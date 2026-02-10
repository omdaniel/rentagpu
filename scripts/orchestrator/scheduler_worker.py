from __future__ import annotations

import dataclasses
import subprocess
import time
from pathlib import Path
from typing import Optional

from .models import ModelProfile, QuotaRuntime, RuntimeDirs, TaskState
from .scheduler_detection import classify_failure
from .scheduler_policy import mark_task_blocked, retry_or_block_task
from .scheduler_runtime import (
    build_prompt,
    changed_files_in_worktree,
    ensure_worktree,
    format_template,
    read_tail,
    run_validation_commands,
    within_allowed_files,
)
from .scheduler_state import EventSink


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
