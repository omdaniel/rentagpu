from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from scripts.orchestrator.models import ModelProfile, QuotaRuntime, TaskSpec, TaskState
from scripts.orchestrator.scheduler_policy import activate_quota_cooldown, retry_or_block_task


class StubEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object]]] = []

    def emit(self, event_type: str, message: str, **extra: object) -> None:
        self.rows.append((event_type, message, extra))


def make_task(task_id: str, *, status: str = "pending") -> TaskState:
    task = TaskState(
        spec=TaskSpec(
            task_id=task_id,
            packet_path=pathlib.Path(f"docs/{task_id}.md"),
            backlog_path=None,
            depends_on=[],
            can_run_in_parallel_with=[],
            allowed_files=set(),
            validation_commands=[],
        )
    )
    task.runtime.status = status
    return task


class LiveOrchestratorQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = StubEvents()
        self.quota_runtime = QuotaRuntime()
        self.profiles = [ModelProfile(model="gpt-5.3-codex", reasoning="low")]

    def test_activate_quota_cooldown_sets_state_and_emits_event(self) -> None:
        with mock.patch("scripts.orchestrator.scheduler_policy.time.time", return_value=1000.0):
            cooldown_until = activate_quota_cooldown(
                quota_runtime=self.quota_runtime,
                cooldown_seconds=900,
                error_summary="429 Too Many Requests",
                events=self.events,  # type: ignore[arg-type]
                task_id="W101",
            )

        self.assertEqual(cooldown_until, 1900.0)
        self.assertEqual(self.quota_runtime.cooldown_until, 1900.0)
        self.assertEqual(self.quota_runtime.last_detected_at, 1000.0)
        self.assertEqual(self.quota_runtime.total_failures, 1)
        self.assertEqual(self.events.rows[0][0], "quota_cooldown")

    def test_activate_quota_cooldown_never_shortens_existing_window(self) -> None:
        self.quota_runtime.cooldown_until = 2500.0
        with mock.patch("scripts.orchestrator.scheduler_policy.time.time", return_value=1000.0):
            cooldown_until = activate_quota_cooldown(
                quota_runtime=self.quota_runtime,
                cooldown_seconds=300,
                error_summary="rate_limit",
                events=self.events,  # type: ignore[arg-type]
                task_id="W101",
            )

        self.assertEqual(cooldown_until, 2500.0)
        self.assertEqual(self.quota_runtime.cooldown_until, 2500.0)

    def test_retry_quota_schedules_pending_task_with_next_eligible(self) -> None:
        task = make_task("W101", status="running")
        task.runtime.attempts = 1
        tasks = {"W101": task}

        with mock.patch("scripts.orchestrator.scheduler_policy.time.time", return_value=1000.0):
            retry_or_block_task(
                tasks=tasks,
                task=task,
                kind="quota",
                error_summary="429 too many requests",
                profiles=self.profiles,
                quota_runtime=self.quota_runtime,
                quota_cooldown_seconds=900,
                quota_max_failures_per_task=3,
                quota_fail_fast=False,
                max_attempts=6,
                escalate_after_compile=2,
                escalate_after_runtime=2,
                events=self.events,  # type: ignore[arg-type]
            )

        self.assertEqual(task.runtime.status, "pending")
        self.assertEqual(task.runtime.next_eligible_at, 1900.0)
        self.assertEqual(task.runtime.quota_failures_total, 1)
        self.assertIn("task_retry", [row[0] for row in self.events.rows])

    def test_retry_quota_blocks_task_when_failure_threshold_reached(self) -> None:
        task = make_task("W101", status="running")
        task.runtime.quota_failures_total = 2
        task.runtime.attempts = 1
        tasks = {"W101": task}

        with mock.patch("scripts.orchestrator.scheduler_policy.time.time", return_value=1000.0):
            retry_or_block_task(
                tasks=tasks,
                task=task,
                kind="quota",
                error_summary="quota exceeded",
                profiles=self.profiles,
                quota_runtime=self.quota_runtime,
                quota_cooldown_seconds=900,
                quota_max_failures_per_task=3,
                quota_fail_fast=False,
                max_attempts=6,
                escalate_after_compile=2,
                escalate_after_runtime=2,
                events=self.events,  # type: ignore[arg-type]
            )

        self.assertEqual(task.runtime.status, "blocked")
        self.assertIn("threshold reached", task.runtime.block_reason or "")

    def test_retry_quota_fail_fast_blocks_current_and_pending_tasks(self) -> None:
        current = make_task("W101", status="running")
        pending = make_task("W102", status="pending")
        tasks = {"W101": current, "W102": pending}

        with mock.patch("scripts.orchestrator.scheduler_policy.time.time", return_value=1000.0):
            retry_or_block_task(
                tasks=tasks,
                task=current,
                kind="quota",
                error_summary="rate limit reached",
                profiles=self.profiles,
                quota_runtime=self.quota_runtime,
                quota_cooldown_seconds=900,
                quota_max_failures_per_task=3,
                quota_fail_fast=True,
                max_attempts=6,
                escalate_after_compile=2,
                escalate_after_runtime=2,
                events=self.events,  # type: ignore[arg-type]
            )

        self.assertEqual(current.runtime.status, "blocked")
        self.assertEqual(pending.runtime.status, "blocked")
        self.assertIn("quota_fail_fast", [row[0] for row in self.events.rows])


if __name__ == "__main__":
    unittest.main()
