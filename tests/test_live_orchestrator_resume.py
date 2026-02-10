from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scripts.orchestrator.models import ModelProfile, QuotaRuntime, TaskSpec, TaskState
from scripts.orchestrator.scheduler_state import restore_runtime_state


class StubEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object]]] = []

    def emit(self, event_type: str, message: str, **extra: object) -> None:
        self.rows.append((event_type, message, extra))


def make_task(task_id: str) -> TaskState:
    return TaskState(
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


class LiveOrchestratorResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = [
            ModelProfile(model="gpt-5.3-codex", reasoning="low"),
            ModelProfile(model="gpt-5.3-codex", reasoning="high"),
        ]

    def test_missing_state_file_emits_resume_skip(self) -> None:
        tasks = {"W101": make_task("W101")}
        quota = QuotaRuntime()
        events = StubEvents()

        with tempfile.TemporaryDirectory(prefix="resume_missing_") as tmp:
            missing = pathlib.Path(tmp) / "state.json"
            restore_runtime_state(
                state_path=missing,
                tasks=tasks,
                profiles=self.profiles,
                quota_runtime=quota,
                events=events,  # type: ignore[arg-type]
            )

        self.assertEqual(tasks["W101"].runtime.status, "pending")
        self.assertEqual(events.rows[0][0], "resume_skip")

    def test_running_status_is_converted_to_pending_with_note(self) -> None:
        tasks = {"W101": make_task("W101")}
        quota = QuotaRuntime()
        events = StubEvents()

        payload = {
            "orchestrator": {},
            "tasks": [
                {
                    "id": "W101",
                    "status": "running",
                    "attempts": 2,
                }
            ],
        }

        with tempfile.TemporaryDirectory(prefix="resume_running_") as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            restore_runtime_state(
                state_path=state_path,
                tasks=tasks,
                profiles=self.profiles,
                quota_runtime=quota,
                events=events,  # type: ignore[arg-type]
            )

        runtime = tasks["W101"].runtime
        self.assertEqual(runtime.status, "pending")
        self.assertEqual(runtime.last_failure_kind, "infra")
        self.assertIn("stale 'running' state", runtime.last_error or "")

    def test_profile_object_mapping_is_preferred_over_profile_index(self) -> None:
        tasks = {"W101": make_task("W101")}
        quota = QuotaRuntime()
        events = StubEvents()

        payload = {
            "orchestrator": {},
            "tasks": [
                {
                    "id": "W101",
                    "status": "pending",
                    "profile_index": 0,
                    "profile": {"model": "gpt-5.3-codex", "reasoning": "high"},
                }
            ],
        }

        with tempfile.TemporaryDirectory(prefix="resume_profile_map_") as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            restore_runtime_state(
                state_path=state_path,
                tasks=tasks,
                profiles=self.profiles,
                quota_runtime=quota,
                events=events,  # type: ignore[arg-type]
            )

        self.assertEqual(tasks["W101"].runtime.profile_index, 1)

    def test_out_of_range_profile_index_is_clamped(self) -> None:
        tasks = {"W101": make_task("W101")}
        quota = QuotaRuntime()
        events = StubEvents()

        payload = {
            "orchestrator": {},
            "tasks": [
                {
                    "id": "W101",
                    "status": "pending",
                    "profile_index": 99,
                }
            ],
        }

        with tempfile.TemporaryDirectory(prefix="resume_profile_clamp_") as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            restore_runtime_state(
                state_path=state_path,
                tasks=tasks,
                profiles=self.profiles,
                quota_runtime=quota,
                events=events,  # type: ignore[arg-type]
            )

        self.assertEqual(tasks["W101"].runtime.profile_index, 1)

    def test_unknown_task_ids_are_ignored(self) -> None:
        tasks = {"W101": make_task("W101"), "W102": make_task("W102")}
        quota = QuotaRuntime()
        events = StubEvents()

        payload = {
            "orchestrator": {},
            "tasks": [
                {
                    "id": "W999",
                    "status": "blocked",
                    "profile_index": 1,
                },
                {
                    "id": "W101",
                    "status": "completed",
                    "profile_index": 0,
                },
            ],
        }

        with tempfile.TemporaryDirectory(prefix="resume_unknown_ids_") as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            restore_runtime_state(
                state_path=state_path,
                tasks=tasks,
                profiles=self.profiles,
                quota_runtime=quota,
                events=events,  # type: ignore[arg-type]
            )

        self.assertEqual(tasks["W101"].runtime.status, "completed")
        self.assertEqual(tasks["W102"].runtime.status, "pending")

    def test_quota_runtime_fields_are_restored(self) -> None:
        tasks = {"W101": make_task("W101")}
        quota = QuotaRuntime()
        events = StubEvents()

        payload = {
            "orchestrator": {
                "quota_cooldown_until": 1234.5,
                "quota_last_detected_at": 1000.0,
                "quota_failures_total": 7,
            },
            "tasks": [
                {
                    "id": "W101",
                    "status": "pending",
                    "profile_index": 0,
                }
            ],
        }

        with tempfile.TemporaryDirectory(prefix="resume_quota_") as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            restore_runtime_state(
                state_path=state_path,
                tasks=tasks,
                profiles=self.profiles,
                quota_runtime=quota,
                events=events,  # type: ignore[arg-type]
            )

        self.assertEqual(quota.cooldown_until, 1234.5)
        self.assertEqual(quota.last_detected_at, 1000.0)
        self.assertEqual(quota.total_failures, 7)

    def test_invalid_tasks_payload_raises_value_error(self) -> None:
        tasks = {"W101": make_task("W101")}
        quota = QuotaRuntime()
        events = StubEvents()

        payload = {
            "orchestrator": {},
            "tasks": {"id": "W101"},
        }

        with tempfile.TemporaryDirectory(prefix="resume_invalid_tasks_") as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                restore_runtime_state(
                    state_path=state_path,
                    tasks=tasks,
                    profiles=self.profiles,
                    quota_runtime=quota,
                    events=events,  # type: ignore[arg-type]
                )


if __name__ == "__main__":
    unittest.main()
