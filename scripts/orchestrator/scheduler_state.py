from __future__ import annotations

import dataclasses
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .models import ModelProfile, QuotaRuntime, TaskState


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


def restore_runtime_state(
    *,
    state_path: Path,
    tasks: dict[str, TaskState],
    profiles: list[ModelProfile],
    quota_runtime: QuotaRuntime,
    events: EventSink,
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
