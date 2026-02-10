from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest


class InjectTemplateSchedulerModulesTests(unittest.TestCase):
    def test_injector_copies_scheduler_submodules(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        injector = repo_root / "scripts" / "inject_orchestration_template.sh"

        with tempfile.TemporaryDirectory(prefix="rentagpu_inject_target_") as tmp:
            target_repo = pathlib.Path(tmp) / "target"
            target_repo.mkdir(parents=True, exist_ok=True)

            proc = subprocess.run(
                [
                    "bash",
                    str(injector),
                    "--target-repo",
                    str(target_repo),
                    "--task-count",
                    "1",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                proc.returncode,
                0,
                msg=(proc.stdout or "") + "\n" + (proc.stderr or ""),
            )

            orchestrator_dir = target_repo / "scripts" / "orchestrator"
            expected = {
                "scheduler.py",
                "scheduler_args.py",
                "scheduler_report.py",
                "scheduler_detection.py",
                "scheduler_probe.py",
                "scheduler_runtime.py",
                "scheduler_state.py",
                "scheduler_policy.py",
                "scheduler_worker.py",
                "scheduler_engine.py",
            }

            present = {path.name for path in orchestrator_dir.glob("scheduler*.py")}
            self.assertTrue(expected.issubset(present), msg=f"missing: {expected - present}")


if __name__ == "__main__":
    unittest.main()
