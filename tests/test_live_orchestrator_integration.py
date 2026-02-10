from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import live_orchestrator


class LiveOrchestratorIntegrationTests(unittest.TestCase):
    def test_main_dry_run_completes_task_and_writes_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rentagpu_orch_it_") as tmp:
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
            ]
            with mock.patch("sys.argv", argv):
                rc = live_orchestrator.main()

            self.assertEqual(rc, 0)
            state_path = repo_root / "tmp" / "live_orchestrator_it" / "state.json"
            self.assertTrue(state_path.exists())

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            tasks = payload.get("tasks", [])
            task_ids = [row.get("id") for row in tasks if isinstance(row, dict)]
            self.assertIn("W101", task_ids)
            row = next(row for row in tasks if row.get("id") == "W101")
            self.assertEqual(row.get("status"), "completed")


if __name__ == "__main__":
    unittest.main()
