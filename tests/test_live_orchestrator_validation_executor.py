from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import live_orchestrator
from scripts.orchestrator.models import ModelProfile, RuntimeDirs, TaskSpec, TaskState
from scripts.orchestrator.scheduler_args import parse_args
from scripts.orchestrator.scheduler_runtime import build_prompt
from scripts.orchestrator.scheduler_worker import launch_task


class StubEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object]]] = []

    def emit(self, event_type: str, message: str, **extra: object) -> None:
        self.rows.append((event_type, message, extra))


def make_task(task_id: str = "W101") -> TaskState:
    return TaskState(
        spec=TaskSpec(
            task_id=task_id,
            packet_path=pathlib.Path(f"docs/{task_id}.md"),
            backlog_path=None,
            depends_on=[],
            can_run_in_parallel_with=[],
            allowed_files={"src/a.py"},
            validation_commands=["echo ok"],
        )
    )


class LiveOrchestratorValidationExecutorTests(unittest.TestCase):
    def test_parse_args_validation_executor_default_and_override(self) -> None:
        with mock.patch("sys.argv", ["live_orchestrator.py"]):
            args = parse_args()
        self.assertEqual(args.validation_executor, "dual")

        with mock.patch(
            "sys.argv",
            ["live_orchestrator.py", "--validation-executor", "orchestrator"],
        ):
            args = parse_args()
        self.assertEqual(args.validation_executor, "orchestrator")

    def test_build_prompt_validation_executor_modes(self) -> None:
        task = make_task("W101")
        profile = ModelProfile(model="gpt-5.3-codex", reasoning="low")

        dual_prompt = build_prompt(task, profile, validation_executor="dual")
        self.assertIn("Run validation commands before exiting.", dual_prompt)

        orch_prompt = build_prompt(task, profile, validation_executor="orchestrator")
        self.assertIn("Do not run validation commands yourself", orch_prompt)
        self.assertIn("orchestrator will run", orch_prompt)

    def test_launch_task_passes_validation_executor_to_prompt_builder(self) -> None:
        task = make_task("W101")
        profiles = [ModelProfile(model="gpt-5.3-codex", reasoning="low")]
        events = StubEvents()

        with tempfile.TemporaryDirectory(prefix="launch_validation_executor_") as tmp:
            tmp_path = pathlib.Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir(parents=True, exist_ok=True)

            dirs = RuntimeDirs(
                root=tmp_path,
                prompts=tmp_path / "prompts",
                logs=tmp_path / "logs",
                state_file=tmp_path / "state.json",
                events_file=tmp_path / "events.jsonl",
                worktrees_root=tmp_path / "worktrees",
            )
            dirs.prompts.mkdir(parents=True, exist_ok=True)
            dirs.logs.mkdir(parents=True, exist_ok=True)
            dirs.worktrees_root.mkdir(parents=True, exist_ok=True)

            fake_proc = mock.Mock()
            fake_proc.pid = 12345

            with (
                mock.patch(
                    "scripts.orchestrator.scheduler_worker.ensure_worktree",
                    return_value=worktree,
                ),
                mock.patch(
                    "scripts.orchestrator.scheduler_worker.build_prompt",
                    return_value="prompt",
                ) as build_prompt_mock,
                mock.patch(
                    "scripts.orchestrator.scheduler_worker.format_template",
                    return_value="echo launched",
                ),
                mock.patch(
                    "scripts.orchestrator.scheduler_worker.subprocess.Popen",
                    return_value=fake_proc,
                ),
            ):
                proc = launch_task(
                    repo_root=tmp_path,
                    dirs=dirs,
                    task=task,
                    profiles=profiles,
                    worker_template="unused",
                    validation_executor="orchestrator",
                    base_ref="HEAD",
                    dry_run=False,
                    events=events,  # type: ignore[arg-type]
                )

        self.assertIs(proc, fake_proc)
        build_prompt_mock.assert_called_once()
        _, kwargs = build_prompt_mock.call_args
        self.assertEqual(kwargs["validation_executor"], "orchestrator")

    def test_main_start_event_includes_validation_executor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="validation_executor_event_") as tmp:
            repo_root = pathlib.Path(tmp)
            subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)

            wave_dir = repo_root / "docs" / "executor_packets" / "wave_b"
            wave_dir.mkdir(parents=True, exist_ok=True)

            packet_path = wave_dir / "W101_packet.md"
            packet_path.write_text(
                "\n".join(
                    [
                        "# Packet W101",
                        "## Allowed Files",
                        "- `src/example.py`",
                        "## Validation Commands",
                        "```bash",
                        "echo ok",
                        "```",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            manifest_path = wave_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "W101",
                                "packet": "docs/executor_packets/wave_b/W101_packet.md",
                                "depends_on": [],
                                "can_run_in_parallel_with": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            argv = [
                "live_orchestrator.py",
                "--repo-root",
                str(repo_root),
                "--manifest",
                "docs/executor_packets/wave_b/manifest.json",
                "--runtime-dir",
                "tmp/live_orchestrator_it",
                "--dry-run",
                "--poll-interval",
                "0",
                "--validation-executor",
                "orchestrator",
            ]
            with mock.patch("sys.argv", argv):
                rc = live_orchestrator.main()

            self.assertEqual(rc, 0)
            events_path = repo_root / "tmp" / "live_orchestrator_it" / "events.jsonl"
            self.assertTrue(events_path.exists())

            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            start_event = next((evt for evt in events if evt.get("event") == "start"), None)
            self.assertIsNotNone(start_event)
            assert start_event is not None
            self.assertEqual(start_event.get("validation_executor"), "orchestrator")
            self.assertIn("validation_executor=orchestrator", str(start_event.get("message") or ""))


if __name__ == "__main__":
    unittest.main()
