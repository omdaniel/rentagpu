#!/usr/bin/env python3
"""Live task orchestrator with worktree workers and escalation policies.

This script coordinates packet-style tasks from a manifest, launches one worker
per ready task in a dedicated git worktree, and continuously re-plans as tasks
complete or fail.

Key behavior:
- Dependency-aware dispatch (`depends_on` from manifest)
- Parallel task execution with bounded concurrency
- Worktree + branch provisioning per task
- Scope gate (changed files must stay within packet allowed files)
- Packet validation command execution after worker exits
- Automatic model/reasoning escalation on repeated compile/runtime failures
- Quota/rate-limit aware retries with global cooldown and optional fail-fast

Typical invocation:
  CODEX_WORKER_COMMAND_TEMPLATE='cat {prompt_file_q} | codex exec -m {model_q} -c model_reasoning_effort={reasoning_q} --cd {worktree_q} --skip-git-repo-check -' \
  python3 scripts/live_orchestrator.py --max-parallel 3
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .manifest import load_manifest, task_branch_name
from .models import (
    ModelProfile,
    QuotaRuntime,
    RuntimeDirs,
    TaskState,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live orchestrator for packet tasks with model escalation."
    )
    parser.add_argument(
        "--manifest",
        default="docs/executor_packets/wave_b/manifest.json",
        help="Path to task manifest JSON.",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root (git top-level for worktree commands).",
    )
    parser.add_argument(
        "--runtime-dir",
        default="tmp/live_orchestrator",
        help="Directory for prompts, logs, and state outputs.",
    )
    parser.add_argument(
        "--worktree-root",
        default=None,
        help="Worktree root directory (default: <runtime-dir>/worktrees).",
    )
    parser.add_argument(
        "--base-ref",
        default="HEAD",
        help="Base ref for new worktree branches.",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=3,
        help="Max concurrent running workers.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=3.0,
        help="Seconds between scheduler iterations.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=6,
        help="Max launch attempts per task before blocking.",
    )
    parser.add_argument(
        "--quota-cooldown-seconds",
        type=int,
        default=900,
        help=(
            "Global cooldown window after a quota/rate-limit failure before "
            "new worker launches are allowed."
        ),
    )
    parser.add_argument(
        "--quota-max-failures-per-task",
        type=int,
        default=3,
        help="Block task after this many quota/rate-limit failures.",
    )
    parser.add_argument(
        "--quota-fail-fast",
        action="store_true",
        help=(
            "On first quota/rate-limit failure, block all pending tasks "
            "immediately instead of waiting for retries."
        ),
    )
    parser.add_argument(
        "--escalate-after-compile",
        type=int,
        default=2,
        help="Escalate after this many compile failures at current profile.",
    )
    parser.add_argument(
        "--escalate-after-runtime",
        type=int,
        default=2,
        help="Escalate after this many runtime failures at current profile.",
    )
    parser.add_argument(
        "--executor-profiles",
        default=(
            "gpt-5.3-codex:low;"
            "gpt-5.3-codex:medium;"
            "gpt-5.3-codex:high;"
            "gpt-5.3-codex:xhigh;"
            "gpt-5.1-codex-max:high"
        ),
        help=(
            "Escalation ladder as semicolon-separated model:reasoning entries, "
            "e.g. 'gpt-5.3-codex:low;gpt-5.3-codex:medium;gpt-5.3-codex:high'. "
            "Reasoning values: none|minimal|low|medium|high|xhigh "
            "(alias 'extrahigh' is accepted and mapped to xhigh)."
        ),
    )
    parser.add_argument(
        "--worker-command-template",
        default=None,
        help=(
            "Command template used to launch one worker. Supports placeholders "
            "{task_id},{model},{reasoning},{worktree},{prompt_file},{log_file} "
            "and *_q quoted variants."
        ),
    )
    parser.add_argument(
        "--command-timeout-seconds",
        type=int,
        default=1800,
        help="Timeout per validation command execution.",
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=int,
        default=0,
        help=(
            "Optional hard timeout for a worker process. 0 disables worker timeout "
            "(default)."
        ),
    )
    parser.add_argument(
        "--allow-empty-allowed-files",
        action="store_true",
        help=(
            "Allow packets with no parsed 'Allowed Files' entries. By default, "
            "empty allowed-file sets are rejected to fail closed."
        ),
    )
    parser.add_argument(
        "--probe-models",
        action="store_true",
        help=(
            "Probe unique models in --executor-profiles via local codex CLI and "
            "drop unsupported models before orchestration starts."
        ),
    )
    parser.add_argument(
        "--probe-model-timeout-seconds",
        type=int,
        default=60,
        help="Timeout per model probe request in seconds.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help=(
            "Ignore existing runtime state and start fresh. By default, the "
            "orchestrator resumes from runtime state when available."
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print a compact report from the current state file and exit.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help=(
            "Optional path override for state.json. Defaults to "
            "<runtime-dir>/state.json."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan and schedule tasks without launching workers or running validation.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.max_parallel < 1:
        raise ValueError("--max-parallel must be at least 1.")
    if args.max_attempts < 1:
        raise ValueError("--max-attempts must be at least 1.")
    if args.quota_cooldown_seconds < 1:
        raise ValueError("--quota-cooldown-seconds must be at least 1.")
    if args.quota_max_failures_per_task < 1:
        raise ValueError("--quota-max-failures-per-task must be at least 1.")
    if args.escalate_after_compile < 1:
        raise ValueError("--escalate-after-compile must be at least 1.")
    if args.escalate_after_runtime < 1:
        raise ValueError("--escalate-after-runtime must be at least 1.")
    if args.command_timeout_seconds < 1:
        raise ValueError("--command-timeout-seconds must be at least 1.")
    if args.worker_timeout_seconds < 0:
        raise ValueError("--worker-timeout-seconds must be >= 0.")
    if args.poll_interval < 0:
        raise ValueError("--poll-interval must be >= 0.")
    if args.probe_model_timeout_seconds < 1:
        raise ValueError("--probe-model-timeout-seconds must be at least 1.")


def parse_profiles(spec: str) -> list[ModelProfile]:
    def normalize_reasoning_effort(value: str) -> str:
        raw = value.strip().lower()
        alias_map = {
            "extrahigh": "xhigh",
            "extra_high": "xhigh",
            "extra-high": "xhigh",
        }
        normalized = alias_map.get(raw, raw)
        valid = {"none", "minimal", "low", "medium", "high", "xhigh"}
        if normalized not in valid:
            raise ValueError(
                f"Invalid reasoning effort '{value}'. "
                "Expected one of: none, minimal, low, medium, high, xhigh."
            )
        return normalized

    profiles: list[ModelProfile] = []
    for token in spec.split(";"):
        token = token.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(
                f"Invalid profile entry '{token}'. Expected '<model>:<reasoning>'."
            )
        model, reasoning = token.split(":", 1)
        profiles.append(
            ModelProfile(
                model=model.strip(),
                reasoning=normalize_reasoning_effort(reasoning),
            )
        )
    if not profiles:
        raise ValueError("No executor profiles parsed.")
    return profiles


def resolve_state_path(
    *,
    repo_root: Optional[Path],
    repo_root_arg: str,
    runtime_dir_arg: str,
    state_file_arg: Optional[str],
) -> Path:
    if state_file_arg:
        return Path(state_file_arg).expanduser().resolve()

    runtime_dir = Path(runtime_dir_arg)
    if runtime_dir.is_absolute():
        base = runtime_dir
    elif repo_root is not None:
        base = (repo_root / runtime_dir).resolve()
    else:
        base = (Path(repo_root_arg).resolve() / runtime_dir).resolve()
    return base / "state.json"


def compact_text(value: Optional[str], max_chars: int = 220) -> str:
    if not value:
        return ""
    collapsed = " ".join(value.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 3] + "..."


def render_state_report(state_path: Path) -> str:
    if not state_path.exists():
        return f"state file not found: {state_path}"

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    summary = payload.get("summary", {})
    orchestrator = payload.get("orchestrator", {})
    if not isinstance(orchestrator, dict):
        orchestrator = {}
    updated = payload.get("updated_at", "unknown")
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []

    blocked_rows: list[str] = []
    errored_rows: list[str] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id", "unknown"))
        status = str(item.get("status", "unknown"))
        block_reason = compact_text(item.get("block_reason"))
        last_error = compact_text(item.get("last_error"))
        attempts = item.get("attempts", 0)

        if status == "blocked":
            reason = block_reason or last_error or "(no reason recorded)"
            blocked_rows.append(f"- {task_id} (attempts={attempts}): {reason}")
        elif last_error:
            errored_rows.append(
                f"- {task_id} [{status}] (attempts={attempts}): {last_error}"
            )

    lines: list[str] = []
    lines.append(f"state: {state_path}")
    lines.append(f"updated_at: {updated}")
    lines.append(
        "summary: "
        f"pending={summary.get('pending', 0)} "
        f"running={summary.get('running', 0)} "
        f"completed={summary.get('completed', 0)} "
        f"blocked={summary.get('blocked', 0)}"
    )
    raw_cooldown_until = orchestrator.get("quota_cooldown_until")
    raw_quota_total = orchestrator.get("quota_failures_total", 0)
    cooldown_remaining = 0
    if isinstance(raw_cooldown_until, (int, float)) and raw_cooldown_until > 0:
        cooldown_remaining = max(0, int(raw_cooldown_until - time.time()))
    quota_total = int(raw_quota_total) if isinstance(raw_quota_total, (int, float)) else 0
    lines.append(
        "quota: "
        f"failures_total={quota_total} "
        f"cooldown_remaining_seconds={cooldown_remaining}"
    )
    lines.append("")
    lines.append("blocked tasks:")
    if blocked_rows:
        lines.extend(blocked_rows)
    else:
        lines.append("- none")
    lines.append("")
    lines.append("latest task errors:")
    if errored_rows:
        lines.extend(errored_rows[:20])
        if len(errored_rows) > 20:
            lines.append(f"- ... {len(errored_rows) - 20} more")
    else:
        lines.append("- none")
    return "\n".join(lines)


def is_model_unsupported(output: str) -> bool:
    lower = output.lower()
    return (
        "is not supported when using codex with a chatgpt account" in lower
        or "model is not supported" in lower
    )


def detect_quota_or_rate_limit(text: str) -> Optional[str]:
    lower = text.lower()
    indicators: list[tuple[str, str]] = [
        ("insufficient_quota", "insufficient_quota"),
        ("quota exceeded", "quota_exceeded"),
        ("exceeded your current quota", "quota_exceeded"),
        ("billing hard limit has been reached", "billing_limit"),
        ("usage limit reached", "usage_limit"),
        ("you have reached your usage limit", "usage_limit"),
        ("you've reached your usage limit", "usage_limit"),
        ("rate limit reached", "rate_limit"),
        ("too many requests", "rate_limit"),
        ("status code 429", "rate_limit"),
        ("429 too many requests", "rate_limit"),
        ("chatgpt account", "account_plan_limit"),
        ("monthly limit reached", "account_plan_limit"),
        ("daily limit reached", "account_plan_limit"),
        ("request was rejected due to rate limiting", "rate_limit"),
    ]
    for needle, kind in indicators:
        if needle in lower:
            return kind
    return None


def probe_model_support(
    *,
    repo_root: Path,
    model: str,
    timeout_seconds: int,
) -> tuple[bool, str]:
    cmd = [
        "codex",
        "exec",
        "-m",
        model,
        "-c",
        "model_reasoning_effort=low",
        "--cd",
        str(repo_root),
        "--skip-git-repo-check",
        "--json",
        "Reply with OK",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return True, f"probe timed out after {timeout_seconds}s; treating as supported"

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and '"turn.completed"' in output:
        return True, "supported"
    if is_model_unsupported(output):
        return False, "unsupported by current account"
    quota_reason = detect_quota_or_rate_limit(output)
    if quota_reason:
        return True, f"probe hit {quota_reason}; treating model as supported"
    # Keep probe failures conservative: treat unknown probe failures as supported,
    # and let runtime retries/escalations handle transient issues.
    return True, "probe inconclusive; treating as supported"


def filter_profiles_by_model_probe(
    *,
    repo_root: Path,
    profiles: list[ModelProfile],
    timeout_seconds: int,
    events: "EventSink",
) -> list[ModelProfile]:
    model_status: dict[str, bool] = {}
    for profile in profiles:
        if profile.model in model_status:
            continue
        supported, reason = probe_model_support(
            repo_root=repo_root,
            model=profile.model,
            timeout_seconds=timeout_seconds,
        )
        model_status[profile.model] = supported
        event_name = "model_probe_ok" if supported else "model_probe_drop"
        events.emit(
            event_name,
            f"model probe {profile.model}: {reason}",
            model=profile.model,
            supported=supported,
            reason=reason,
        )

    filtered = [p for p in profiles if model_status.get(p.model, True)]
    if not filtered:
        raise RuntimeError(
            "All models were removed by --probe-models. "
            "Adjust --executor-profiles or authentication."
        )
    return filtered


def restore_runtime_state(
    *,
    state_path: Path,
    tasks: dict[str, TaskState],
    profiles: list[ModelProfile],
    quota_runtime: QuotaRuntime,
    events: "EventSink",
) -> None:
    if not state_path.exists():
        events.emit("resume_skip", f"no existing state file at {state_path}")
        return

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    orchestrator = payload.get("orchestrator", {})
    if isinstance(orchestrator, dict):
        raw_cooldown_until = orchestrator.get("quota_cooldown_until")
        if isinstance(raw_cooldown_until, (int, float)):
            quota_runtime.cooldown_until = float(raw_cooldown_until)
        raw_last_detected = orchestrator.get("quota_last_detected_at")
        if isinstance(raw_last_detected, (int, float)):
            quota_runtime.last_detected_at = float(raw_last_detected)
        raw_total = orchestrator.get("quota_failures_total")
        if isinstance(raw_total, (int, float)):
            quota_runtime.total_failures = int(raw_total)

    task_entries = payload.get("tasks")
    if not isinstance(task_entries, list):
        raise ValueError(f"Invalid state file format: {state_path}")

    profile_lookup = {
        (p.model, p.reasoning): idx for idx, p in enumerate(profiles)
    }
    restored = 0
    resumed_running = 0
    for entry in task_entries:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("id", ""))
        if task_id not in tasks:
            continue
        runtime = tasks[task_id].runtime

        status = str(entry.get("status", "pending"))
        if status not in {"pending", "running", "completed", "blocked"}:
            status = "pending"

        profile_index = None
        profile_obj = entry.get("profile")
        if isinstance(profile_obj, dict):
            key = (
                str(profile_obj.get("model", "")),
                str(profile_obj.get("reasoning", "")),
            )
            profile_index = profile_lookup.get(key)
        if profile_index is None:
            raw_idx = entry.get("profile_index")
            if isinstance(raw_idx, int):
                profile_index = max(0, min(raw_idx, len(profiles) - 1))
            else:
                profile_index = 0

        runtime.status = status
        runtime.profile_index = profile_index
        runtime.attempts = int(entry.get("attempts", runtime.attempts) or 0)
        runtime.compile_failures_total = int(
            entry.get("compile_failures_total", runtime.compile_failures_total) or 0
        )
        runtime.runtime_failures_total = int(
            entry.get("runtime_failures_total", runtime.runtime_failures_total) or 0
        )
        runtime.quota_failures_total = int(
            entry.get("quota_failures_total", runtime.quota_failures_total) or 0
        )
        runtime.other_failures_total = int(
            entry.get("other_failures_total", runtime.other_failures_total) or 0
        )
        runtime.last_failure_kind = entry.get("last_failure_kind")
        runtime.last_error = entry.get("last_error")
        runtime.block_reason = entry.get("block_reason")
        worktree_path = entry.get("worktree_path")
        if isinstance(worktree_path, str) and worktree_path:
            runtime.worktree_path = Path(worktree_path)
        branch_name = entry.get("branch_name")
        if isinstance(branch_name, str) and branch_name:
            runtime.branch_name = branch_name
        prompt_file = entry.get("prompt_file")
        if isinstance(prompt_file, str) and prompt_file:
            runtime.prompt_file = Path(prompt_file)
        log_file = entry.get("log_file")
        if isinstance(log_file, str) and log_file:
            runtime.log_file = Path(log_file)
        next_eligible = entry.get("next_eligible_at")
        if isinstance(next_eligible, (int, float)):
            runtime.next_eligible_at = float(next_eligible)
        last_changed = entry.get("last_changed_files")
        if isinstance(last_changed, list):
            runtime.last_changed_files = [str(x) for x in last_changed]

        if runtime.status == "running":
            runtime.status = "pending"
            resumed_running += 1
            note = "resumed from stale 'running' state; previous worker is not attached"
            if runtime.last_error:
                runtime.last_error = f"{runtime.last_error}\n{note}"
            else:
                runtime.last_error = note
            runtime.last_failure_kind = runtime.last_failure_kind or "infra"

        restored += 1

    events.emit(
        "resume_loaded",
        (
            f"restored {restored} task runtime entries from state "
            f"(running->pending={resumed_running})."
        ),
        restored=restored,
        running_to_pending=resumed_running,
        state_file=str(state_path),
    )


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


def timeout_stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def classify_failure(text: str) -> str:
    quota_reason = detect_quota_or_rate_limit(text)
    if quota_reason:
        return "quota"

    lower = text.lower()
    compile_markers = [
        "failed to compile",
        "compilation failed",
        "could not compile",
        "cargo check",
        "cargo build",
        "swift build",
        "error[e",
        "no such module",
    ]
    runtime_markers = [
        "thread 'main' panicked",
        "panic",
        "segmentation fault",
        "fatal error",
        "traceback",
        "assertion failed",
        "runtime error",
    ]
    test_markers = [
        "test failed",
        "failures:",
        "assertion",
        "0 passed; 1 failed",
        "failed in",
    ]
    infra_markers = [
        "timed out",
        "timeout",
        "permission denied",
        "network is unreachable",
        "temporary failure",
        "killed",
    ]
    if any(marker in lower for marker in compile_markers):
        return "compile"
    if any(marker in lower for marker in runtime_markers):
        return "runtime"
    if any(marker in lower for marker in test_markers):
        return "test"
    if any(marker in lower for marker in infra_markers):
        return "infra"
    return "unknown"


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


def safe_error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ts_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class EventSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def emit(self, event_type: str, message: str, **extra: Any) -> None:
        payload = {
            "time": now_iso(),
            "event": event_type,
            "message": message,
            **extra,
        }
        line = json.dumps(payload, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(f"[{payload['time']}] {event_type}: {message}")


def write_state(
    *,
    path: Path,
    tasks: dict[str, TaskState],
    profiles: list[ModelProfile],
    quota_runtime: QuotaRuntime,
    running: dict[str, subprocess.Popen[str]],
) -> None:
    payload: dict[str, Any] = {
        "updated_at": now_iso(),
        "profiles": [dataclasses.asdict(p) for p in profiles],
        "orchestrator": {
            "quota_cooldown_until": quota_runtime.cooldown_until,
            "quota_last_detected_at": quota_runtime.last_detected_at,
            "quota_failures_total": quota_runtime.total_failures,
        },
        "summary": {
            "pending": sum(1 for t in tasks.values() if t.runtime.status == "pending"),
            "running": sum(1 for t in tasks.values() if t.runtime.status == "running"),
            "completed": sum(1 for t in tasks.values() if t.runtime.status == "completed"),
            "blocked": sum(1 for t in tasks.values() if t.runtime.status == "blocked"),
        },
        "tasks": [],
    }
    for task_id, task in sorted(tasks.items()):
        runtime = task.runtime
        pid = running[task_id].pid if task_id in running else None
        payload["tasks"].append(
            {
                "id": task_id,
                "status": runtime.status,
                "attempts": runtime.attempts,
                "profile_index": runtime.profile_index,
                "profile": dataclasses.asdict(profiles[runtime.profile_index]),
                "depends_on": task.spec.depends_on,
                "compile_failures_total": runtime.compile_failures_total,
                "runtime_failures_total": runtime.runtime_failures_total,
                "quota_failures_total": runtime.quota_failures_total,
                "other_failures_total": runtime.other_failures_total,
                "last_failure_kind": runtime.last_failure_kind,
                "last_error": runtime.last_error,
                "block_reason": runtime.block_reason,
                "worktree_path": str(runtime.worktree_path) if runtime.worktree_path else None,
                "branch_name": runtime.branch_name,
                "prompt_file": str(runtime.prompt_file) if runtime.prompt_file else None,
                "log_file": str(runtime.log_file) if runtime.log_file else None,
                "next_eligible_at": runtime.next_eligible_at,
                "last_changed_files": runtime.last_changed_files,
                "pid": pid,
            }
        )
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def task_ready(task: TaskState, tasks: dict[str, TaskState]) -> bool:
    if task.runtime.status != "pending":
        return False
    if (
        task.runtime.next_eligible_at is not None
        and time.time() < task.runtime.next_eligible_at
    ):
        return False
    for dep in task.spec.depends_on:
        if tasks[dep].runtime.status != "completed":
            return False
    return True


def all_done(tasks: dict[str, TaskState]) -> bool:
    return all(t.runtime.status in {"completed", "blocked"} for t in tasks.values())


def mark_task_blocked(task: TaskState, reason: str, events: EventSink) -> None:
    runtime = task.runtime
    runtime.status = "blocked"
    runtime.block_reason = reason
    runtime.finished_at = time.time()
    runtime.next_eligible_at = None
    runtime.last_error = reason
    events.emit(
        "task_blocked",
        f"{task.spec.task_id} blocked: {reason}",
        task_id=task.spec.task_id,
        reason=reason,
    )


def activate_quota_cooldown(
    *,
    quota_runtime: QuotaRuntime,
    cooldown_seconds: int,
    error_summary: str,
    events: EventSink,
    task_id: str,
) -> float:
    now_ts = time.time()
    new_until = now_ts + cooldown_seconds
    previous_until = quota_runtime.cooldown_until
    quota_runtime.cooldown_until = max(previous_until, new_until)
    quota_runtime.last_detected_at = now_ts
    quota_runtime.total_failures += 1

    events.emit(
        "quota_cooldown",
        (
            f"{task_id} hit quota/rate-limit; pausing new launches until "
            f"{ts_iso(quota_runtime.cooldown_until)}."
        ),
        task_id=task_id,
        cooldown_seconds=cooldown_seconds,
        cooldown_until=ts_iso(quota_runtime.cooldown_until),
        previous_cooldown_until=ts_iso(previous_until) if previous_until > 0 else None,
        summary=compact_text(error_summary, max_chars=500),
        quota_failures_total=quota_runtime.total_failures,
    )
    return quota_runtime.cooldown_until


def block_all_pending_tasks_for_quota(
    *,
    tasks: dict[str, TaskState],
    reason: str,
    events: EventSink,
) -> int:
    blocked = 0
    for task in tasks.values():
        if task.runtime.status != "pending":
            continue
        mark_task_blocked(task, reason, events)
        blocked += 1
    return blocked


def maybe_escalate(
    *,
    task: TaskState,
    kind: str,
    profiles: list[ModelProfile],
    escalate_after_compile: int,
    escalate_after_runtime: int,
    events: EventSink,
) -> bool:
    runtime = task.runtime
    if kind == "compile":
        runtime.compile_failures_total += 1
        runtime.compile_failures_level += 1
    elif kind == "runtime":
        runtime.runtime_failures_total += 1
        runtime.runtime_failures_level += 1
    else:
        runtime.other_failures_total += 1
        runtime.compile_failures_level = 0
        runtime.runtime_failures_level = 0
        return False

    should_escalate = (
        (kind == "compile" and runtime.compile_failures_level >= escalate_after_compile)
        or (kind == "runtime" and runtime.runtime_failures_level >= escalate_after_runtime)
    )
    if not should_escalate:
        return False
    if runtime.profile_index >= len(profiles) - 1:
        return False

    old_profile = profiles[runtime.profile_index]
    runtime.profile_index += 1
    runtime.compile_failures_level = 0
    runtime.runtime_failures_level = 0
    new_profile = profiles[runtime.profile_index]
    events.emit(
        "escalation",
        (
            f"{task.spec.task_id} escalated from {old_profile.model}/{old_profile.reasoning} "
            f"to {new_profile.model}/{new_profile.reasoning} after repeated {kind} failures."
        ),
        task_id=task.spec.task_id,
        kind=kind,
        from_profile=dataclasses.asdict(old_profile),
        to_profile=dataclasses.asdict(new_profile),
    )
    return True


def launch_task(
    *,
    repo_root: Path,
    dirs: RuntimeDirs,
    task: TaskState,
    profiles: list[ModelProfile],
    worker_template: str,
    base_ref: str,
    dry_run: bool,
    events: EventSink,
) -> Optional[subprocess.Popen[str]]:
    runtime = task.runtime
    profile = profiles[runtime.profile_index]

    runtime.attempts += 1
    runtime.status = "running"
    runtime.started_at = time.time()
    runtime.block_reason = None
    runtime.next_eligible_at = None

    if dry_run:
        runtime.status = "completed"
        runtime.finished_at = time.time()
        events.emit(
            "task_completed",
            f"{task.spec.task_id} completed in dry-run mode.",
            task_id=task.spec.task_id,
            dry_run=True,
        )
        return None

    worktree = ensure_worktree(
        repo_root=repo_root,
        dirs=dirs,
        task=task,
        base_ref=base_ref,
    )

    prompt = build_prompt(task, profile)
    prompt_file = dirs.prompts / f"{task.spec.task_id}_attempt_{runtime.attempts:02d}.txt"
    log_file = dirs.logs / f"{task.spec.task_id}_attempt_{runtime.attempts:02d}.log"
    prompt_file.write_text(prompt, encoding="utf-8")

    runtime.prompt_file = prompt_file
    runtime.log_file = log_file

    command = format_template(
        worker_template,
        {
            "task_id": task.spec.task_id,
            "model": profile.model,
            "reasoning": profile.reasoning,
            "worktree": str(worktree),
            "prompt_file": str(prompt_file),
            "log_file": str(log_file),
            "packet_path": str(task.spec.packet_path),
        },
    )
    with log_file.open("w", encoding="utf-8") as stream:
        stream.write(f"# launch: {command}\n")
        stream.flush()
        proc = subprocess.Popen(
            command,
            cwd=repo_root,
            shell=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
    events.emit(
        "task_launched",
        (
            f"{task.spec.task_id} attempt {runtime.attempts} launched with "
            f"{profile.model}/{profile.reasoning} (pid={proc.pid})."
        ),
        task_id=task.spec.task_id,
        attempt=runtime.attempts,
        pid=proc.pid,
        profile=dataclasses.asdict(profile),
    )
    return proc


def retry_or_block_task(
    *,
    tasks: dict[str, TaskState],
    task: TaskState,
    kind: str,
    error_summary: str,
    profiles: list[ModelProfile],
    quota_runtime: QuotaRuntime,
    quota_cooldown_seconds: int,
    quota_max_failures_per_task: int,
    quota_fail_fast: bool,
    max_attempts: int,
    escalate_after_compile: int,
    escalate_after_runtime: int,
    events: EventSink,
) -> None:
    runtime = task.runtime
    runtime.last_failure_kind = kind
    runtime.last_error = error_summary
    runtime.finished_at = time.time()
    runtime.next_eligible_at = None

    if kind == "quota":
        runtime.quota_failures_total += 1
        runtime.other_failures_total += 1
        runtime.compile_failures_level = 0
        runtime.runtime_failures_level = 0
        cooldown_until = activate_quota_cooldown(
            quota_runtime=quota_runtime,
            cooldown_seconds=quota_cooldown_seconds,
            error_summary=error_summary,
            events=events,
            task_id=task.spec.task_id,
        )

        if quota_fail_fast:
            reason = (
                "quota/rate limit detected and --quota-fail-fast is enabled "
                f"(cooldown_until={ts_iso(cooldown_until)})."
            )
            mark_task_blocked(task, reason, events)
            blocked_count = block_all_pending_tasks_for_quota(
                tasks=tasks,
                reason=reason,
                events=events,
            )
            events.emit(
                "quota_fail_fast",
                (
                    f"{task.spec.task_id} triggered fail-fast quota stop; "
                    f"blocked_pending={blocked_count}."
                ),
                task_id=task.spec.task_id,
                blocked_pending=blocked_count,
                cooldown_until=ts_iso(cooldown_until),
            )
            return

        if runtime.quota_failures_total >= quota_max_failures_per_task:
            mark_task_blocked(
                task,
                (
                    "quota/rate-limit failure threshold reached "
                    f"({runtime.quota_failures_total}/{quota_max_failures_per_task})."
                ),
                events,
            )
            return

        runtime.status = "pending"
        runtime.next_eligible_at = cooldown_until
        events.emit(
            "task_retry",
            (
                f"{task.spec.task_id} scheduled to retry after quota/rate-limit "
                f"(attempt {runtime.attempts}/{max_attempts}, "
                f"quota_failures={runtime.quota_failures_total}/"
                f"{quota_max_failures_per_task}, "
                f"next_eligible_at={ts_iso(cooldown_until)})."
            ),
            task_id=task.spec.task_id,
            attempt=runtime.attempts,
            failure_kind=kind,
            next_eligible_at=ts_iso(cooldown_until),
            cooldown_until=ts_iso(cooldown_until),
            quota_failures_total=runtime.quota_failures_total,
        )
        return

    maybe_escalate(
        task=task,
        kind=kind,
        profiles=profiles,
        escalate_after_compile=escalate_after_compile,
        escalate_after_runtime=escalate_after_runtime,
        events=events,
    )

    if runtime.attempts >= max_attempts:
        mark_task_blocked(
            task,
            (
                f"max attempts reached ({runtime.attempts}). "
                f"Last failure kind={kind}."
            ),
            events,
        )
        return

    runtime.status = "pending"
    runtime.next_eligible_at = None
    events.emit(
        "task_retry",
        (
            f"{task.spec.task_id} scheduled to retry after {kind} failure "
            f"(attempt {runtime.attempts}/{max_attempts})."
        ),
        task_id=task.spec.task_id,
        attempt=runtime.attempts,
        failure_kind=kind,
    )


def propagate_dependency_blocks(tasks: dict[str, TaskState], events: EventSink) -> None:
    changed = True
    while changed:
        changed = False
        for task in tasks.values():
            runtime = task.runtime
            if runtime.status != "pending":
                continue
            blocked_dep = next(
                (dep for dep in task.spec.depends_on if tasks[dep].runtime.status == "blocked"),
                None,
            )
            if blocked_dep is None:
                continue
            mark_task_blocked(
                task,
                f"dependency {blocked_dep} is blocked",
                events,
            )
            changed = True


def handle_finished_worker(
    *,
    tasks: dict[str, TaskState],
    task: TaskState,
    returncode: int,
    profiles: list[ModelProfile],
    quota_runtime: QuotaRuntime,
    quota_cooldown_seconds: int,
    quota_max_failures_per_task: int,
    quota_fail_fast: bool,
    max_attempts: int,
    escalate_after_compile: int,
    escalate_after_runtime: int,
    validation_timeout_seconds: int,
    events: EventSink,
) -> None:
    runtime = task.runtime
    runtime.finished_at = time.time()
    log_excerpt = read_tail(runtime.log_file) if runtime.log_file else ""

    if returncode != 0:
        kind = classify_failure(log_excerpt)
        retry_or_block_task(
            tasks=tasks,
            task=task,
            kind=kind,
            error_summary=(
                f"worker exited with code {returncode}; "
                f"log tail:\n{log_excerpt}"
            ),
            profiles=profiles,
            quota_runtime=quota_runtime,
            quota_cooldown_seconds=quota_cooldown_seconds,
            quota_max_failures_per_task=quota_max_failures_per_task,
            quota_fail_fast=quota_fail_fast,
            max_attempts=max_attempts,
            escalate_after_compile=escalate_after_compile,
            escalate_after_runtime=escalate_after_runtime,
            events=events,
        )
        return

    worktree = runtime.worktree_path
    if worktree is None:
        mark_task_blocked(task, "missing worktree path after worker exit", events)
        return

    changed_files = changed_files_in_worktree(worktree)
    runtime.last_changed_files = changed_files
    if not within_allowed_files(changed_files, task.spec.allowed_files):
        disallowed = [p for p in changed_files if p not in task.spec.allowed_files]
        mark_task_blocked(
            task,
            "scope gate failed (disallowed files): " + ", ".join(disallowed),
            events,
        )
        return

    ok, validation_message = run_validation_commands(
        task=task,
        timeout_seconds=validation_timeout_seconds,
    )
    if not ok:
        kind = classify_failure(validation_message)
        retry_or_block_task(
            tasks=tasks,
            task=task,
            kind=kind,
            error_summary=f"validation failed:\n{validation_message}",
            profiles=profiles,
            quota_runtime=quota_runtime,
            quota_cooldown_seconds=quota_cooldown_seconds,
            quota_max_failures_per_task=quota_max_failures_per_task,
            quota_fail_fast=quota_fail_fast,
            max_attempts=max_attempts,
            escalate_after_compile=escalate_after_compile,
            escalate_after_runtime=escalate_after_runtime,
            events=events,
        )
        return

    runtime.status = "completed"
    runtime.last_failure_kind = None
    runtime.last_error = None
    runtime.compile_failures_level = 0
    runtime.runtime_failures_level = 0
    runtime.next_eligible_at = None
    events.emit(
        "task_completed",
        f"{task.spec.task_id} completed and passed validation.",
        task_id=task.spec.task_id,
        changed_files=changed_files,
    )


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


def main() -> int:
    args = parse_args()

    if args.report:
        try:
            repo_for_state: Optional[Path]
            if args.state_file:
                repo_for_state = None
            else:
                try:
                    repo_for_state = git_root(Path(args.repo_root).resolve())
                except RuntimeError:
                    repo_for_state = Path(args.repo_root).resolve()
            state_path = resolve_state_path(
                repo_root=repo_for_state,
                repo_root_arg=args.repo_root,
                runtime_dir_arg=args.runtime_dir,
                state_file_arg=args.state_file,
            )
            print(render_state_report(state_path))
            return 0 if state_path.exists() else 1
        except (ValueError, FileNotFoundError, json.JSONDecodeError, RuntimeError, OSError) as exc:
            print(f"report error: {safe_error_text(exc)}", file=sys.stderr)
            return 2

    try:
        validate_args(args)
        repo_root = git_root(Path(args.repo_root).resolve())
        manifest_path = (repo_root / args.manifest).resolve()
        tasks = load_manifest(
            repo_root,
            manifest_path,
            allow_empty_allowed_files=args.allow_empty_allowed_files,
        )
        profiles = parse_profiles(args.executor_profiles)
        dirs = ensure_dirs(repo_root, args.runtime_dir, args.worktree_root)
        if args.state_file:
            dirs.state_file = Path(args.state_file).expanduser().resolve()
            dirs.state_file.parent.mkdir(parents=True, exist_ok=True)
            dirs.events_file = dirs.state_file.parent / "events.jsonl"
    except (ValueError, FileNotFoundError, json.JSONDecodeError, RuntimeError, OSError) as exc:
        print(f"startup error: {safe_error_text(exc)}", file=sys.stderr)
        return 2
    events = EventSink(dirs.events_file)

    quota_runtime = QuotaRuntime()
    quota_wait_announced_until = 0.0

    if args.probe_models:
        try:
            profiles = filter_profiles_by_model_probe(
                repo_root=repo_root,
                profiles=profiles,
                timeout_seconds=args.probe_model_timeout_seconds,
                events=events,
            )
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"model probe error: {safe_error_text(exc)}", file=sys.stderr)
            return 2

    if not args.no_resume:
        try:
            restore_runtime_state(
                state_path=dirs.state_file,
                tasks=tasks,
                profiles=profiles,
                quota_runtime=quota_runtime,
                events=events,
            )
        except (ValueError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            print(f"resume error: {safe_error_text(exc)}", file=sys.stderr)
            return 2
    else:
        events.emit("resume_skip", "--no-resume set; starting with fresh runtime state")
    if quota_runtime.cooldown_until > time.time():
        quota_wait_announced_until = quota_runtime.cooldown_until
        events.emit(
            "quota_wait",
            (
                "resumed with active quota cooldown; new launches paused until "
                f"{ts_iso(quota_runtime.cooldown_until)}."
            ),
            cooldown_until=ts_iso(quota_runtime.cooldown_until),
        )

    worker_template = args.worker_command_template or default_worker_template()
    if not args.dry_run and not worker_template:
        print(
            "Missing worker command template. Provide --worker-command-template "
            "or set CODEX_WORKER_COMMAND_TEMPLATE.",
            file=sys.stderr,
        )
        print(
            "Example template:\n"
            "  cat {prompt_file_q} | codex exec -m {model_q} "
            "-c model_reasoning_effort={reasoning_q} "
            "--cd {worktree_q} --skip-git-repo-check -",
            file=sys.stderr,
        )
        return 2
    worker_template = worker_template or ""

    running: dict[str, subprocess.Popen[str]] = {}
    events.emit(
        "start",
        (
            f"Live orchestrator started with {len(tasks)} tasks, "
            f"max_parallel={args.max_parallel}, dry_run={args.dry_run}."
        ),
        manifest=str(manifest_path),
    )

    try:
        while True:
            finished_ids: list[str] = []
            for task_id, proc in list(running.items()):
                task = tasks[task_id]
                runtime = task.runtime
                if (
                    args.worker_timeout_seconds > 0
                    and runtime.started_at is not None
                    and (time.time() - runtime.started_at) > args.worker_timeout_seconds
                ):
                    terminate_process(proc)
                    finished_ids.append(task_id)
                    retry_or_block_task(
                        tasks=tasks,
                        task=task,
                        kind="infra",
                        error_summary=(
                            f"worker timed out after {args.worker_timeout_seconds}s"
                        ),
                        profiles=profiles,
                        quota_runtime=quota_runtime,
                        quota_cooldown_seconds=args.quota_cooldown_seconds,
                        quota_max_failures_per_task=args.quota_max_failures_per_task,
                        quota_fail_fast=args.quota_fail_fast,
                        max_attempts=args.max_attempts,
                        escalate_after_compile=args.escalate_after_compile,
                        escalate_after_runtime=args.escalate_after_runtime,
                        events=events,
                    )
                    continue

                returncode = proc.poll()
                if returncode is None:
                    continue
                finished_ids.append(task_id)
                try:
                    handle_finished_worker(
                        tasks=tasks,
                        task=task,
                        returncode=returncode,
                        profiles=profiles,
                        quota_runtime=quota_runtime,
                        quota_cooldown_seconds=args.quota_cooldown_seconds,
                        quota_max_failures_per_task=args.quota_max_failures_per_task,
                        quota_fail_fast=args.quota_fail_fast,
                        max_attempts=args.max_attempts,
                        escalate_after_compile=args.escalate_after_compile,
                        escalate_after_runtime=args.escalate_after_runtime,
                        validation_timeout_seconds=args.command_timeout_seconds,
                        events=events,
                    )
                except Exception as exc:  # defensive: do not crash the orchestrator loop
                    retry_or_block_task(
                        tasks=tasks,
                        task=task,
                        kind="infra",
                        error_summary=(
                            "orchestrator post-processing failure: "
                            + safe_error_text(exc)
                        ),
                        profiles=profiles,
                        quota_runtime=quota_runtime,
                        quota_cooldown_seconds=args.quota_cooldown_seconds,
                        quota_max_failures_per_task=args.quota_max_failures_per_task,
                        quota_fail_fast=args.quota_fail_fast,
                        max_attempts=args.max_attempts,
                        escalate_after_compile=args.escalate_after_compile,
                        escalate_after_runtime=args.escalate_after_runtime,
                        events=events,
                    )
            for task_id in finished_ids:
                running.pop(task_id, None)

            propagate_dependency_blocks(tasks, events)

            now_ts = time.time()
            if (
                quota_runtime.cooldown_until > 0
                and now_ts >= quota_runtime.cooldown_until
            ):
                events.emit(
                    "quota_resume",
                    "quota cooldown expired; scheduling resumes.",
                )
                quota_runtime.cooldown_until = 0.0
                quota_wait_announced_until = 0.0

            capacity = max(args.max_parallel - len(running), 0)
            if quota_runtime.cooldown_until > now_ts:
                capacity = 0
                if quota_wait_announced_until != quota_runtime.cooldown_until:
                    events.emit(
                        "quota_wait",
                        (
                            "quota cooldown active; suppressing new launches until "
                            f"{ts_iso(quota_runtime.cooldown_until)}."
                        ),
                        cooldown_until=ts_iso(quota_runtime.cooldown_until),
                        remaining_seconds=int(quota_runtime.cooldown_until - now_ts),
                    )
                    quota_wait_announced_until = quota_runtime.cooldown_until

            if capacity > 0:
                ready = [t for t in tasks.values() if task_ready(t, tasks)]
                ready.sort(key=lambda t: t.spec.task_id)
                for task in ready[:capacity]:
                    try:
                        launched_proc = launch_task(
                            repo_root=repo_root,
                            dirs=dirs,
                            task=task,
                            profiles=profiles,
                            worker_template=worker_template,
                            base_ref=args.base_ref,
                            dry_run=args.dry_run,
                            events=events,
                        )
                        if launched_proc is not None:
                            running[task.spec.task_id] = launched_proc
                    except Exception as exc:  # defensive: launch failures become task failures
                        retry_or_block_task(
                            tasks=tasks,
                            task=task,
                            kind="infra",
                            error_summary="worker launch failure: " + safe_error_text(exc),
                            profiles=profiles,
                            quota_runtime=quota_runtime,
                            quota_cooldown_seconds=args.quota_cooldown_seconds,
                            quota_max_failures_per_task=args.quota_max_failures_per_task,
                            quota_fail_fast=args.quota_fail_fast,
                            max_attempts=args.max_attempts,
                            escalate_after_compile=args.escalate_after_compile,
                            escalate_after_runtime=args.escalate_after_runtime,
                            events=events,
                        )

            write_state(
                path=dirs.state_file,
                tasks=tasks,
                profiles=profiles,
                quota_runtime=quota_runtime,
                running=running,
            )

            if all_done(tasks) and not running:
                break

            if not running:
                has_ready = any(task_ready(t, tasks) for t in tasks.values())
                if not has_ready:
                    # Remaining tasks are blocked by dependency state.
                    propagate_dependency_blocks(tasks, events)
                    if all_done(tasks):
                        break

            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        events.emit("interrupt", "KeyboardInterrupt received. Shutting down workers.")
        for task_id, proc in list(running.items()):
            terminate_process(proc)
            mark_task_blocked(
                tasks[task_id],
                "orchestrator interrupted by operator",
                events,
            )
            running.pop(task_id, None)
        write_state(
            path=dirs.state_file,
            tasks=tasks,
            profiles=profiles,
            quota_runtime=quota_runtime,
            running=running,
        )
        return 130

    completed = sum(1 for t in tasks.values() if t.runtime.status == "completed")
    blocked = sum(1 for t in tasks.values() if t.runtime.status == "blocked")
    events.emit(
        "finish",
        f"Orchestration finished: completed={completed}, blocked={blocked}.",
        completed=completed,
        blocked=blocked,
        state_file=str(dirs.state_file),
    )
    return 0 if blocked == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
