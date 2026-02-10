from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from scripts.orchestrator.models import ModelProfile, QuotaRuntime, TaskSpec, TaskState
from scripts.orchestrator.scheduler_runtime import changed_files_in_worktree
from scripts.orchestrator.scheduler_worker import handle_finished_worker


class StubEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object]]] = []

    def emit(self, event_type: str, message: str, **extra: object) -> None:
        self.rows.append((event_type, message, extra))


def make_task(task_id: str, allowed_files: set[str]) -> TaskState:
    task = TaskState(
        spec=TaskSpec(
            task_id=task_id,
            packet_path=pathlib.Path(f"docs/{task_id}.md"),
            backlog_path=None,
            depends_on=[],
            can_run_in_parallel_with=[],
            allowed_files=allowed_files,
            validation_commands=["echo ok"],
        )
    )
    task.runtime.status = "running"
    task.runtime.attempts = 1
    return task


class LiveOrchestratorScopeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = [ModelProfile(model="gpt-5.3-codex", reasoning="low")]
        self.quota_runtime = QuotaRuntime()

    def test_handle_finished_worker_blocks_when_worktree_missing(self) -> None:
        task = make_task("W101", {"src/a.py"})
        tasks = {"W101": task}
        events = StubEvents()

        handle_finished_worker(
            tasks=tasks,
            task=task,
            returncode=0,
            profiles=self.profiles,
            quota_runtime=self.quota_runtime,
            quota_cooldown_seconds=900,
            quota_max_failures_per_task=3,
            quota_fail_fast=False,
            max_attempts=6,
            escalate_after_compile=2,
            escalate_after_runtime=2,
            validation_timeout_seconds=10,
            events=events,  # type: ignore[arg-type]
        )

        self.assertEqual(task.runtime.status, "blocked")
        self.assertIn("missing worktree path", task.runtime.block_reason or "")

    def test_scope_gate_failure_blocks_and_records_changed_files(self) -> None:
        task = make_task("W101", {"src/a.py"})
        with tempfile.TemporaryDirectory(prefix="scope_gate_") as tmp:
            task.runtime.worktree_path = pathlib.Path(tmp)
            tasks = {"W101": task}
            events = StubEvents()

            with mock.patch(
                "scripts.orchestrator.scheduler_worker.changed_files_in_worktree",
                return_value=["src/a.py", "src/disallowed.py"],
            ):
                handle_finished_worker(
                    tasks=tasks,
                    task=task,
                    returncode=0,
                    profiles=self.profiles,
                    quota_runtime=self.quota_runtime,
                    quota_cooldown_seconds=900,
                    quota_max_failures_per_task=3,
                    quota_fail_fast=False,
                    max_attempts=6,
                    escalate_after_compile=2,
                    escalate_after_runtime=2,
                    validation_timeout_seconds=10,
                    events=events,  # type: ignore[arg-type]
                )

        self.assertEqual(task.runtime.last_changed_files, ["src/a.py", "src/disallowed.py"])
        self.assertEqual(task.runtime.status, "blocked")
        self.assertIn("scope gate failed", task.runtime.block_reason or "")
        self.assertIn("src/disallowed.py", task.runtime.block_reason or "")

    def test_validation_failure_routes_to_retry_path(self) -> None:
        task = make_task("W101", {"src/a.py"})
        with tempfile.TemporaryDirectory(prefix="scope_validation_") as tmp:
            task.runtime.worktree_path = pathlib.Path(tmp)
            tasks = {"W101": task}
            events = StubEvents()

            with (
                mock.patch(
                    "scripts.orchestrator.scheduler_worker.changed_files_in_worktree",
                    return_value=["src/a.py"],
                ),
                mock.patch(
                    "scripts.orchestrator.scheduler_worker.run_validation_commands",
                    return_value=(False, "failed to compile"),
                ),
                mock.patch("scripts.orchestrator.scheduler_worker.retry_or_block_task") as retry_mock,
            ):
                handle_finished_worker(
                    tasks=tasks,
                    task=task,
                    returncode=0,
                    profiles=self.profiles,
                    quota_runtime=self.quota_runtime,
                    quota_cooldown_seconds=900,
                    quota_max_failures_per_task=3,
                    quota_fail_fast=False,
                    max_attempts=6,
                    escalate_after_compile=2,
                    escalate_after_runtime=2,
                    validation_timeout_seconds=10,
                    events=events,  # type: ignore[arg-type]
                )

        retry_mock.assert_called_once()
        self.assertNotEqual(task.runtime.status, "completed")

    def test_changed_files_in_worktree_unions_and_deduplicates(self) -> None:
        responses = [
            mock.Mock(returncode=0, stdout="a.py\nb.py\n"),
            mock.Mock(returncode=0, stdout="b.py\nc.py\n"),
            mock.Mock(returncode=0, stdout="c.py\nd.py\n"),
        ]

        with mock.patch("scripts.orchestrator.scheduler_runtime.run_cmd", side_effect=responses):
            files = changed_files_in_worktree(pathlib.Path("/tmp/unused"))

        self.assertEqual(files, ["a.py", "b.py", "c.py", "d.py"])


if __name__ == "__main__":
    unittest.main()
