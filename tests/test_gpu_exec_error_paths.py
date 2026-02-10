from __future__ import annotations

import argparse
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import scripts.gpu_exec as gpu_exec


class GpuExecErrorPathTests(unittest.TestCase):
    def test_main_returns_64_for_unsupported_backend(self) -> None:
        argv = [
            "gpu_exec.py",
            "--backend",
            "unknown",
            "--task-id",
            "W101",
            "--attempt",
            "1",
            "--command",
            "echo ok",
        ]
        with mock.patch("sys.argv", argv):
            rc = gpu_exec.main()
        self.assertEqual(rc, 64)

    def test_main_returns_70_on_workspace_packaging_error(self) -> None:
        argv = [
            "gpu_exec.py",
            "--backend",
            "modal",
            "--task-id",
            "W101",
            "--attempt",
            "1",
            "--command",
            "echo ok",
        ]
        with (
            mock.patch("sys.argv", argv),
            mock.patch("scripts.gpu_exec._git_root", side_effect=RuntimeError("boom")),
        ):
            rc = gpu_exec.main()
        self.assertEqual(rc, 70)

    def test_main_returns_70_on_backend_timeout(self) -> None:
        argv = [
            "gpu_exec.py",
            "--backend",
            "modal",
            "--task-id",
            "W101",
            "--attempt",
            "1",
            "--command",
            "echo ok",
        ]
        with tempfile.TemporaryDirectory(prefix="gpu_exec_timeout_") as tmp:
            repo_root = pathlib.Path(tmp)
            temp_dir = pathlib.Path(tmp) / "work"
            temp_dir.mkdir(parents=True, exist_ok=True)
            archive_path = temp_dir / "workspace.tar.gz"
            archive_path.write_bytes(b"small")

            with (
                mock.patch("sys.argv", argv),
                mock.patch("scripts.gpu_exec._git_root", return_value=repo_root),
                mock.patch("scripts.gpu_exec._collect_workspace_files", return_value=[pathlib.Path("x")]),
                mock.patch("scripts.gpu_exec._create_workspace_archive", return_value=archive_path),
                mock.patch(
                    "scripts.gpu_exec._run_modal_backend",
                    side_effect=subprocess.TimeoutExpired(
                        cmd="modal run",
                        timeout=10,
                        output="stdout",
                        stderr="stderr",
                    ),
                ),
            ):
                rc = gpu_exec.main()

        self.assertEqual(rc, 70)

    def test_main_returns_70_on_backend_runtime_error(self) -> None:
        argv = [
            "gpu_exec.py",
            "--backend",
            "modal",
            "--task-id",
            "W101",
            "--attempt",
            "1",
            "--command",
            "echo ok",
        ]
        with tempfile.TemporaryDirectory(prefix="gpu_exec_runtime_") as tmp:
            repo_root = pathlib.Path(tmp)
            temp_dir = pathlib.Path(tmp) / "work"
            temp_dir.mkdir(parents=True, exist_ok=True)
            archive_path = temp_dir / "workspace.tar.gz"
            archive_path.write_bytes(b"small")

            with (
                mock.patch("sys.argv", argv),
                mock.patch("scripts.gpu_exec._git_root", return_value=repo_root),
                mock.patch("scripts.gpu_exec._collect_workspace_files", return_value=[pathlib.Path("x")]),
                mock.patch("scripts.gpu_exec._create_workspace_archive", return_value=archive_path),
                mock.patch(
                    "scripts.gpu_exec._run_modal_backend",
                    side_effect=RuntimeError(
                        "modal submission failed",
                        {"returncode": 1, "stdout": "oops", "stderr": "bad"},
                    ),
                ),
            ):
                rc = gpu_exec.main()

        self.assertEqual(rc, 70)

    def test_run_modal_backend_rejects_oversized_inline_archive_without_s3(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gpu_exec_oversize_") as tmp:
            repo_root = pathlib.Path(tmp)
            archive = repo_root / "workspace.tar.gz"
            archive.write_bytes(b"x" * (gpu_exec.MAX_INLINE_WORKSPACE_BYTES + 1))
            args = argparse.Namespace(
                timeout_seconds=None,
                artifact_prefix=None,
                task_id="W101",
                attempt=1,
                command="echo ok",
                gpu=None,
            )
            config = {
                "modal": {
                    "entrypoint": "scripts/gpu_modal_app.py::submit",
                    "submit_timeout_seconds": 10,
                    "gpu": "L4",
                },
                "artifacts": {"s3_bucket": "", "s3_prefix": "gpu-runs"},
                "timeouts": {"default_command_timeout_seconds": 1200},
            }

            with self.assertRaises(RuntimeError) as ctx:
                gpu_exec._run_modal_backend(
                    repo_root=repo_root,
                    args=args,
                    config=config,
                    mode="hybrid",
                    workspace_archive=archive,
                )

        self.assertIn("too large for inline payload transport", str(ctx.exception))

    def test_run_modal_backend_surfaces_modal_nonzero_return(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gpu_exec_nonzero_") as tmp:
            repo_root = pathlib.Path(tmp)
            archive = repo_root / "workspace.tar.gz"
            archive.write_bytes(b"small")
            args = argparse.Namespace(
                timeout_seconds=None,
                artifact_prefix=None,
                task_id="W101",
                attempt=1,
                command="echo ok",
                gpu=None,
            )
            config = {
                "modal": {
                    "entrypoint": "scripts/gpu_modal_app.py::submit",
                    "submit_timeout_seconds": 10,
                    "gpu": "L4",
                },
                "artifacts": {"s3_bucket": "", "s3_prefix": "gpu-runs"},
                "timeouts": {"default_command_timeout_seconds": 1200},
            }

            proc = mock.Mock(returncode=1, stdout="modal stdout", stderr="modal stderr")
            with (
                mock.patch(
                    "scripts.gpu_exec._run_modal_backend_sdk",
                    side_effect=gpu_exec._ModalSdkUnavailableError("sdk unavailable"),
                ),
                mock.patch("scripts.gpu_exec.subprocess.run", return_value=proc),
                self.assertRaises(RuntimeError) as ctx,
            ):
                gpu_exec._run_modal_backend(
                    repo_root=repo_root,
                    args=args,
                    config=config,
                    mode="hybrid",
                    workspace_archive=archive,
                )

        exc = ctx.exception
        self.assertEqual(exc.args[0], "modal submission failed")
        details = exc.args[1]
        self.assertEqual(details["returncode"], 1)
        self.assertIn("modal stdout", details["stdout"])
        self.assertIn("modal stderr", details["stderr"])
        self.assertIn("sdk_fallback", details)

    def test_run_modal_backend_prefers_sdk_when_available(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gpu_exec_sdk_ok_") as tmp:
            repo_root = pathlib.Path(tmp)
            archive = repo_root / "workspace.tar.gz"
            archive.write_bytes(b"small")
            args = argparse.Namespace(
                timeout_seconds=None,
                artifact_prefix=None,
                task_id="W101",
                attempt=1,
                command="echo ok",
                gpu=None,
            )
            config = {
                "modal": {
                    "entrypoint": "scripts/gpu_modal_app.py::submit",
                    "submit_timeout_seconds": 10,
                    "gpu": "L4",
                },
                "artifacts": {"s3_bucket": "", "s3_prefix": "gpu-runs"},
                "timeouts": {"default_command_timeout_seconds": 1200},
            }

            with (
                mock.patch(
                    "scripts.gpu_exec._run_modal_backend_sdk",
                    return_value=(
                        {"run_id": "sdk-123", "exit_code": 0},
                        "[gpu_exec] modal submit path=sdk",
                    ),
                ) as sdk_mock,
                mock.patch("scripts.gpu_exec._run_modal_backend_cli") as cli_mock,
            ):
                result, backend_stdout, backend_stderr = gpu_exec._run_modal_backend(
                    repo_root=repo_root,
                    args=args,
                    config=config,
                    mode="hybrid",
                    workspace_archive=archive,
                )

        self.assertEqual(result["run_id"], "sdk-123")
        self.assertIn("path=sdk", backend_stdout)
        self.assertEqual(backend_stderr, "")
        sdk_mock.assert_called_once()
        cli_mock.assert_not_called()

    def test_run_modal_backend_falls_back_to_cli_when_sdk_unavailable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gpu_exec_sdk_fallback_") as tmp:
            repo_root = pathlib.Path(tmp)
            archive = repo_root / "workspace.tar.gz"
            archive.write_bytes(b"small")
            args = argparse.Namespace(
                timeout_seconds=None,
                artifact_prefix=None,
                task_id="W101",
                attempt=1,
                command="echo ok",
                gpu=None,
            )
            config = {
                "modal": {
                    "entrypoint": "scripts/gpu_modal_app.py::submit",
                    "submit_timeout_seconds": 10,
                    "gpu": "L4",
                },
                "artifacts": {"s3_bucket": "", "s3_prefix": "gpu-runs"},
                "timeouts": {"default_command_timeout_seconds": 1200},
            }

            proc = mock.Mock(
                returncode=0,
                stdout='{"run_id":"cli-456","exit_code":0}',
                stderr="",
            )
            with (
                mock.patch(
                    "scripts.gpu_exec._run_modal_backend_sdk",
                    side_effect=gpu_exec._ModalSdkUnavailableError("no sdk fn"),
                ),
                mock.patch("scripts.gpu_exec.subprocess.run", return_value=proc) as run_mock,
            ):
                result, backend_stdout, backend_stderr = gpu_exec._run_modal_backend(
                    repo_root=repo_root,
                    args=args,
                    config=config,
                    mode="hybrid",
                    workspace_archive=archive,
                )

        self.assertEqual(result["run_id"], "cli-456")
        self.assertIn("falling back to CLI", backend_stdout)
        self.assertEqual(backend_stderr, "")
        run_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
