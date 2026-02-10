from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest


class BootstrapOrchestratorWavePyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = pathlib.Path(__file__).resolve().parents[1]
        cls.bootstrap = cls.repo_root / "scripts" / "bootstrap_orchestrator_wave.py"
        cls.orchestrator_source = cls.repo_root / "live_orchestrator.py"

    def run_bootstrap(
        self,
        *,
        repo_root: pathlib.Path,
        args: list[str],
        include_source: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            "python3",
            str(self.bootstrap),
            "--repo-root",
            str(repo_root),
        ]
        if include_source:
            cmd.extend(["--orchestrator-source", str(self.orchestrator_source)])
        cmd.extend(args)
        return subprocess.run(
            cmd,
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dry_run_writes_no_repo_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap_py_dry_") as tmp:
            target = pathlib.Path(tmp) / "target"
            target.mkdir(parents=True, exist_ok=True)

            proc = self.run_bootstrap(
                repo_root=target,
                args=["--dry-run", "--wave", "wave_dry", "--task-count", "1"],
            )

            self.assertEqual(proc.returncode, 0, msg=(proc.stdout or "") + "\n" + (proc.stderr or ""))
            self.assertIn("[dry-run] write", proc.stdout)
            self.assertIn("bootstrap completed", proc.stdout)
            self.assertFalse((target / "docs").exists())
            self.assertFalse((target / "scripts").exists())
            self.assertFalse((target / "AGENTS.md").exists())

    def test_non_dry_run_creates_expected_files_for_two_tasks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap_py_files_") as tmp:
            target = pathlib.Path(tmp) / "target"
            target.mkdir(parents=True, exist_ok=True)

            proc = self.run_bootstrap(
                repo_root=target,
                args=[
                    "--wave",
                    "wave_x",
                    "--task-count",
                    "2",
                    "--start-id",
                    "201",
                    "--id-prefix",
                    "T",
                ],
            )
            self.assertEqual(proc.returncode, 0, msg=(proc.stdout or "") + "\n" + (proc.stderr or ""))

            files = {
                str(path.relative_to(target))
                for path in target.rglob("*")
                if path.is_file()
            }
            expected = {
                "AGENTS.md",
                "docs/refactor_plan.md",
                "docs/backlog/201-task-1.md",
                "docs/backlog/202-task-2.md",
                "docs/executor_packets/wave_x/ATOMIC_DECOMPOSITION_GUIDE.md",
                "docs/executor_packets/wave_x/PLANNING_SESSION_PROMPT.md",
                "docs/executor_packets/wave_x/README.md",
                "docs/executor_packets/wave_x/T201_task_1.md",
                "docs/executor_packets/wave_x/T202_task_2.md",
                "docs/executor_packets/wave_x/manifest.json",
                "docs/executor_packets/wave_x/orchestrator_state.md",
                "scripts/live_orchestrator.py",
                "scripts/orchestrator_gate.sh",
            }
            self.assertEqual(files, expected)

    def test_manifest_dependencies_and_parallel_hints_match_rules(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap_py_manifest_") as tmp:
            target = pathlib.Path(tmp) / "target"
            target.mkdir(parents=True, exist_ok=True)

            proc = self.run_bootstrap(
                repo_root=target,
                args=["--wave", "wave_m", "--task-count", "4", "--start-id", "101"],
            )
            self.assertEqual(proc.returncode, 0, msg=(proc.stdout or "") + "\n" + (proc.stderr or ""))

            manifest_path = target / "docs" / "executor_packets" / "wave_m" / "manifest.json"
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["wave"], "wave_m")
            self.assertRegex(payload["generated_at"], r"^\d{4}-\d{2}-\d{2}$")
            tasks = payload["tasks"]
            self.assertEqual(len(tasks), 4)

            self.assertEqual(tasks[0]["depends_on"], [])
            self.assertEqual(tasks[1]["depends_on"], [])
            self.assertEqual(tasks[2]["depends_on"], ["W101", "W102"])
            self.assertEqual(tasks[3]["depends_on"], ["W103"])

            self.assertEqual(tasks[0]["can_run_in_parallel_with"], ["W102"])
            self.assertEqual(tasks[1]["can_run_in_parallel_with"], ["W101"])
            self.assertEqual(tasks[2]["can_run_in_parallel_with"], [])
            self.assertEqual(tasks[3]["can_run_in_parallel_with"], [])

    def test_overwrite_flag_controls_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap_py_overwrite_") as tmp:
            target = pathlib.Path(tmp) / "target"
            target.mkdir(parents=True, exist_ok=True)

            base_args = ["--wave", "wave_o", "--task-count", "1"]
            first = self.run_bootstrap(repo_root=target, args=base_args)
            self.assertEqual(first.returncode, 0)

            packet_path = target / "docs" / "executor_packets" / "wave_o" / "W101_task_1.md"
            packet_path.write_text("CUSTOM\n", encoding="utf-8")

            second = self.run_bootstrap(repo_root=target, args=base_args)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(packet_path.read_text(encoding="utf-8"), "CUSTOM\n")

            third = self.run_bootstrap(repo_root=target, args=base_args + ["--overwrite"])
            self.assertEqual(third.returncode, 0)
            content = packet_path.read_text(encoding="utf-8")
            self.assertIn("# Packet W101: Task 1", content)
            self.assertNotIn("CUSTOM", content)

    def test_agents_contract_append_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap_py_agents_idem_") as tmp:
            target = pathlib.Path(tmp) / "target"
            target.mkdir(parents=True, exist_ok=True)

            args = ["--wave", "wave_a", "--task-count", "1"]
            first = self.run_bootstrap(repo_root=target, args=args)
            self.assertEqual(first.returncode, 0)
            second = self.run_bootstrap(repo_root=target, args=args)
            self.assertEqual(second.returncode, 0)

            agents = (target / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(agents.count("## Orchestrator/Executor Contract"), 1)
            self.assertEqual(agents.count("## Orchestrator Planning Contract"), 1)

    def test_agents_context_file_and_text_are_appended(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap_py_agents_ctx_") as tmp:
            target = pathlib.Path(tmp) / "target"
            target.mkdir(parents=True, exist_ok=True)

            context_file = pathlib.Path(tmp) / "context.md"
            context_file.write_text("Project context line 1\nProject context line 2\n", encoding="utf-8")

            proc = self.run_bootstrap(
                repo_root=target,
                args=[
                    "--wave",
                    "wave_ctx",
                    "--task-count",
                    "1",
                    "--agents-context-file",
                    str(context_file),
                    "--agents-context-text",
                    "Inline context tail",
                ],
            )
            self.assertEqual(proc.returncode, 0)

            agents = (target / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("## Project Orchestration Context", agents)
            self.assertIn("Project context line 1", agents)
            self.assertIn("Project context line 2", agents)
            self.assertIn("Inline context tail", agents)
            self.assertLess(agents.index("Project context line 1"), agents.index("Inline context tail"))

    def test_invalid_invocations_return_code_2(self) -> None:
        missing_value = subprocess.run(
            ["python3", str(self.bootstrap), "--wave"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(missing_value.returncode, 2)
        self.assertIn("expected one argument", missing_value.stderr)

        with tempfile.TemporaryDirectory(prefix="bootstrap_py_invalid_wave_") as tmp:
            target = pathlib.Path(tmp) / "target"
            target.mkdir(parents=True, exist_ok=True)
            invalid_wave = self.run_bootstrap(
                repo_root=target,
                args=["--wave", "bad/wave", "--task-count", "1"],
            )

        self.assertEqual(invalid_wave.returncode, 2)
        self.assertIn("--wave contains invalid characters", invalid_wave.stderr)

    def test_missing_orchestrator_source_errors_when_target_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap_py_missing_src_") as tmp:
            target = pathlib.Path(tmp) / "target"
            target.mkdir(parents=True, exist_ok=True)

            proc = self.run_bootstrap(
                repo_root=target,
                args=["--wave", "wave_src", "--task-count", "1"],
                include_source=False,
            )

            self.assertEqual(proc.returncode, 2)
            self.assertIn("--orchestrator-source is required", proc.stderr)


if __name__ == "__main__":
    unittest.main()
