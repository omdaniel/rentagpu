from __future__ import annotations

import dataclasses
import time

from .models import ModelProfile, QuotaRuntime, TaskState
from .scheduler_report import compact_text
from .scheduler_state import EventSink, ts_iso


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
