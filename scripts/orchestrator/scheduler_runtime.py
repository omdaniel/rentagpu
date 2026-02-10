from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .manifest import task_branch_name
from .models import ModelProfile, RuntimeDirs, TaskState
from .scheduler_detection import timeout_stream_text


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        output = (proc.stdout or "") + (proc.stderr or "")
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{output}"
        )
    return proc


def git_root(path: Path) -> Path:
    proc = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=path, check=True)
    return Path(proc.stdout.strip()).resolve()


def ensure_dirs(
    repo_root: Path,
    runtime_dir_arg: str,
    worktree_root_arg: Optional[str],
) -> RuntimeDirs:
    runtime_root = (repo_root / runtime_dir_arg).resolve()
    worktrees = (
        Path(worktree_root_arg).resolve()
        if worktree_root_arg
        else (runtime_root / "worktrees")
    )
    prompts = runtime_root / "prompts"
    logs = runtime_root / "logs"
    runtime_root.mkdir(parents=True, exist_ok=True)
    worktrees.mkdir(parents=True, exist_ok=True)
    prompts.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return RuntimeDirs(
        root=runtime_root,
        prompts=prompts,
        logs=logs,
        state_file=runtime_root / "state.json",
        events_file=runtime_root / "events.jsonl",
        worktrees_root=worktrees,
    )


def ensure_worktree(
    *,
    repo_root: Path,
    dirs: RuntimeDirs,
    task: TaskState,
    base_ref: str,
) -> Path:
    task_id = task.spec.task_id
    branch = task.runtime.branch_name or task_branch_name(task.spec)
    task.runtime.branch_name = branch
    worktree = task.runtime.worktree_path or (dirs.worktrees_root / task_id.lower())
    task.runtime.worktree_path = worktree

    if worktree.exists():
        valid = run_cmd(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=worktree,
            check=False,
        )
        if valid.returncode != 0:
            raise RuntimeError(
                f"Worktree path exists but is not a git repository: {worktree}"
            )
        return worktree

    branch_exists = (
        run_cmd(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            cwd=repo_root,
            check=False,
        ).returncode
        == 0
    )
    if branch_exists:
        cmd = ["git", "worktree", "add", str(worktree), branch]
    else:
        cmd = ["git", "worktree", "add", "-b", branch, str(worktree), base_ref]
    run_cmd(cmd, cwd=repo_root, check=True)
    return worktree


def build_prompt(task: TaskState, profile: ModelProfile) -> str:
    spec = task.spec
    runtime = task.runtime
    allowed = sorted(spec.allowed_files)
    validations = spec.validation_commands
    escalation_note = ""
    if runtime.attempts > 1 and runtime.last_error:
        escalation_note = (
            "\nPrevious attempt failed.\n"
            f"- failure_kind: {runtime.last_failure_kind}\n"
            f"- summary: {runtime.last_error}\n"
            "Address this directly before making new changes.\n"
        )

    allowed_block = "\n".join(f"- `{path}`" for path in allowed) or "- (none parsed)"
    validation_block = "\n".join(f"- `{cmd}`" for cmd in validations) or "- (none parsed)"
    deps = ", ".join(spec.depends_on) if spec.depends_on else "none"

    return (
        f"You are executing packet {spec.task_id}.\n"
        f"Packet path: {spec.packet_path}\n"
        f"Dependencies already satisfied: {deps}\n"
        f"Target model profile: model={profile.model}, reasoning={profile.reasoning}\n"
        f"{escalation_note}\n"
        "Instructions:\n"
        f"1. Read and execute: `{spec.packet_path}`.\n"
        "2. Edit only the allowed files below.\n"
        "3. Keep changes minimal and aligned with packet objective.\n"
        "4. Run validation commands before exiting.\n"
        "5. If blocked, explain the blocker with exact failing command/output.\n"
        "\nAllowed files:\n"
        f"{allowed_block}\n"
        "\nValidation commands:\n"
        f"{validation_block}\n"
        "\nRequired return format:\n"
        "[TASK] WBxx\n"
        "[STATE] completed|blocked\n"
        "[FILES] ...\n"
        "[VALIDATION] ran: ...\n"
        "[EVIDENCE] key output lines + skipped step reason\n"
        "[BLOCKERS] none|...\n"
    )


def format_template(template: str, values: dict[str, str]) -> str:
    expanded = dict(values)
    for key, value in values.items():
        expanded[f"{key}_q"] = shlex.quote(value)
    try:
        return template.format_map(expanded)
    except KeyError as exc:
        missing = str(exc)
        raise ValueError(
            f"Missing placeholder in worker command template: {missing}. "
            "Allowed placeholders: task_id, model, reasoning, worktree, "
            "prompt_file, log_file, packet_path plus *_q variants."
        ) from exc


def read_tail(path: Path, max_lines: int = 120) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def changed_files_in_worktree(worktree: Path) -> list[str]:
    files: set[str] = set()
    for cmd in (
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        proc = run_cmd(cmd, cwd=worktree, check=False)
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line:
                files.add(line)
    return sorted(files)


def within_allowed_files(changed_files: Iterable[str], allowed_files: set[str]) -> bool:
    if not allowed_files:
        return True
    return all(path in allowed_files for path in changed_files)


def run_validation_commands(
    *,
    task: TaskState,
    timeout_seconds: int,
) -> tuple[bool, str]:
    runtime = task.runtime
    worktree = runtime.worktree_path
    assert worktree is not None
    if not task.spec.validation_commands:
        return True, "No validation commands parsed from packet."

    failures: list[str] = []
    for idx, command in enumerate(task.spec.validation_commands, start=1):
        try:
            proc = subprocess.run(
                command,
                cwd=worktree,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode != 0:
                failures.append(
                    f"[{idx}] `{command}` failed ({proc.returncode})\n"
                    f"{output.strip()}"
                )
                break
        except subprocess.TimeoutExpired as exc:
            partial = timeout_stream_text(exc.stdout) + timeout_stream_text(exc.stderr)
            failures.append(
                f"[{idx}] `{command}` timed out after {timeout_seconds}s\n"
                f"{partial.strip()}"
            )
            break

    if failures:
        return False, "\n\n".join(failures)
    return True, "All packet validation commands passed."


def default_worker_template() -> Optional[str]:
    template = os.environ.get("CODEX_WORKER_COMMAND_TEMPLATE")
    if template:
        return template
    return (
        "cat {prompt_file_q} | "
        "codex exec -m {model_q} "
        "-c model_reasoning_effort={reasoning_q} "
        "--cd {worktree_q} --skip-git-repo-check -"
    )


def terminate_process(proc: subprocess.Popen[str], timeout_seconds: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_seconds)
