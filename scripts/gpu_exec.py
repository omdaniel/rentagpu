#!/usr/bin/env python3
"""Local bridge for remote GPU execution backends."""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_CONFIG_PATH = pathlib.Path("config/gpu_backend.toml")
DEFAULT_RUNS_JSONL = pathlib.Path("tmp/live_orchestrator/gpu_runs.jsonl")
DEFAULT_POLICY_STATE = pathlib.Path("tmp/live_orchestrator/gpu_policy_state.json")
MAX_INLINE_WORKSPACE_BYTES = 8 * 1024 * 1024


@dataclass
class PolicyDecision:
    mode: str
    reason: str


def _tail(text: str, max_lines: int = 120) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _load_config(path: pathlib.Path) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "backend": {"name": "modal"},
        "modal": {
            "entrypoint": "scripts/gpu_modal_app.py::submit",
            "gpu": "L4",
            "submit_timeout_seconds": 7200,
        },
        "policy": {
            "promote_attempts": 4,
            "promote_window_seconds": 900,
            "promote_cold_start_median_seconds": 45,
            "demote_idle_seconds": 1800,
            "history_limit": 100,
        },
        "artifacts": {
            "s3_prefix": "gpu-runs",
        },
        "timeouts": {
            "default_command_timeout_seconds": 1200,
            "modal_submit_timeout_seconds": 7200,
        },
    }

    if not path.exists():
        return defaults

    with path.open("rb") as fh:
        payload = tomllib.load(fh)
    merged = dict(defaults)
    for key, value in payload.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            section = dict(merged[key])
            section.update(value)
            merged[key] = section
        else:
            merged[key] = value
    return merged


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _git_root(path: pathlib.Path) -> pathlib.Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "not a git repository")
    return pathlib.Path(proc.stdout.strip()).resolve()


def _collect_workspace_files(worktree: pathlib.Path) -> list[pathlib.Path]:
    proc = subprocess.run(
        ["git", "ls-files", "-c", "-m", "-o", "--exclude-standard", "-z"],
        cwd=worktree,
        capture_output=True,
        text=False,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("failed to collect workspace files with git ls-files")
    raw = proc.stdout.decode("utf-8", errors="replace")
    entries = [chunk for chunk in raw.split("\x00") if chunk]
    files: list[pathlib.Path] = []
    for token in entries:
        rel = pathlib.Path(token)
        abs_path = (worktree / rel).resolve()
        if abs_path.is_file():
            files.append(rel)
    return sorted(files)


def _create_workspace_archive(worktree: pathlib.Path, files: list[pathlib.Path]) -> pathlib.Path:
    if not files:
        raise RuntimeError("workspace packaging found no tracked/modified/untracked files")
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="rentagpu_workspace_"))
    archive_path = temp_dir / "workspace.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as tf:
        for rel_path in files:
            tf.add(worktree / rel_path, arcname=str(rel_path))
    return archive_path


def _load_policy_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"mode": "hybrid", "last_activity_epoch": 0, "history": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"mode": "hybrid", "last_activity_epoch": 0, "history": []}


def _save_policy_state(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _median_cold_start_seconds(history: list[dict[str, Any]]) -> float:
    samples: list[float] = []
    for row in history[-5:]:
        if not row.get("cold_start"):
            continue
        latency_ms = row.get("startup_latency_ms")
        if isinstance(latency_ms, (int, float)):
            samples.append(float(latency_ms) / 1000.0)
    if not samples:
        return 0.0
    return float(statistics.median(samples))


def _decide_execution_mode(
    *,
    state: dict[str, Any],
    policy: dict[str, Any],
    override: str,
    now_ts: float,
) -> PolicyDecision:
    if override == "on":
        return PolicyDecision(mode="hot", reason="forced_by_flag")
    if override == "off":
        return PolicyDecision(mode="hybrid", reason="forced_by_flag")

    prior_mode = str(state.get("mode") or "hybrid")
    idle_seconds = max(0.0, now_ts - float(state.get("last_activity_epoch") or 0.0))
    demote_idle_seconds = _to_int(policy.get("demote_idle_seconds"), 1800)
    if prior_mode == "hot" and idle_seconds >= demote_idle_seconds:
        prior_mode = "hybrid"

    history = list(state.get("history") or [])
    promote_window_seconds = _to_int(policy.get("promote_window_seconds"), 900)
    promote_attempts = _to_int(policy.get("promote_attempts"), 4)
    promote_cold_start = float(policy.get("promote_cold_start_median_seconds") or 45)

    recent_attempts = [
        row
        for row in history
        if isinstance(row, dict)
        and isinstance(row.get("finished_at_epoch"), (int, float))
        and (now_ts - float(row["finished_at_epoch"])) <= promote_window_seconds
    ]
    if len(recent_attempts) >= promote_attempts:
        return PolicyDecision(mode="hot", reason="promoted_by_attempt_burst")

    median_cold = _median_cold_start_seconds(history)
    if median_cold > promote_cold_start:
        return PolicyDecision(mode="hot", reason="promoted_by_cold_start_latency")

    if prior_mode == "hot":
        return PolicyDecision(mode="hot", reason="retain_hot_non_idle")
    return PolicyDecision(mode="hybrid", reason="default_hybrid")


def _append_jsonl(path: pathlib.Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _parse_result_json(stdout: str) -> dict[str, Any]:
    candidates = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in reversed(candidates):
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "run_id" in payload:
            return payload
    raise ValueError("unable to parse JSON result from modal output")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run command on remote rented GPU backend.")
    parser.add_argument("--backend", required=True, help="Execution backend (supported: modal).")
    parser.add_argument("--task-id", required=True, help="Task identifier for artifact names.")
    parser.add_argument("--attempt", required=True, type=int, help="Attempt number.")
    parser.add_argument("--command", required=True, help="Command to execute remotely.")
    parser.add_argument("--gpu", default=None, help="Optional GPU override (e.g. L4, A10G, A100).")
    parser.add_argument(
        "--timeout-seconds",
        default=None,
        type=int,
        help="Remote command timeout override in seconds.",
    )
    parser.add_argument(
        "--artifact-prefix",
        default=None,
        help="Optional artifact prefix. Defaults to config/artifacts/task+attempt prefix.",
    )
    parser.add_argument(
        "--hot-mode",
        choices=["auto", "on", "off"],
        default="auto",
        help="Execution mode override. auto applies hybrid/hot policy.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to gpu backend config TOML.",
    )
    return parser


def _stderr_error(error_type: str, message: str, details: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "error_type": error_type,
        "message": message,
    }
    if details:
        payload["details"] = details
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def _run_modal_backend(
    *,
    repo_root: pathlib.Path,
    args: argparse.Namespace,
    config: dict[str, Any],
    mode: str,
    workspace_archive: pathlib.Path,
) -> tuple[dict[str, Any], str, str]:
    modal_cfg = config.get("modal", {})
    artifacts_cfg = config.get("artifacts", {})
    timeouts_cfg = config.get("timeouts", {})

    default_timeout = _to_int(timeouts_cfg.get("default_command_timeout_seconds"), 1200)
    timeout_seconds = args.timeout_seconds if args.timeout_seconds else default_timeout

    base_prefix = str(artifacts_cfg.get("s3_prefix") or "gpu-runs").strip("/")
    default_artifact_prefix = (
        f"{base_prefix}/{args.task_id}/attempt-{int(args.attempt):02d}/{int(time.time())}"
    )
    artifact_prefix = args.artifact_prefix or default_artifact_prefix
    workspace_blob: dict[str, Any] = {}
    archive_size = workspace_archive.stat().st_size
    artifact_bucket = str(artifacts_cfg.get("s3_bucket") or "")
    if artifact_bucket:
        try:
            import boto3
        except Exception as exc:
            raise RuntimeError(
                "workspace staging failed: boto3 is required when artifacts.s3_bucket is set",
                {"error": str(exc)},
            ) from exc

        workspace_key = f"{artifact_prefix.strip('/')}/workspace.tar.gz"
        client = boto3.client(
            "s3",
            region_name=str(artifacts_cfg.get("s3_region") or "us-east-1"),
            endpoint_url=str(artifacts_cfg.get("s3_endpoint_url") or "") or None,
        )
        client.upload_file(str(workspace_archive), artifact_bucket, workspace_key)
        workspace_blob["workspace_tar"] = f"s3://{artifact_bucket}/{workspace_key}"
    else:
        if archive_size > MAX_INLINE_WORKSPACE_BYTES:
            raise RuntimeError(
                (
                    "workspace archive is too large for inline payload transport; "
                    "set artifacts.s3_bucket in config to enable S3 staging."
                ),
                {"workspace_archive_bytes": archive_size},
            )
        workspace_blob["workspace_tar_b64"] = base64.b64encode(workspace_archive.read_bytes()).decode(
            "ascii"
        )
        workspace_blob["workspace_tar"] = str(workspace_archive)

    payload: dict[str, Any] = {
        "run_id": f"{args.task_id.lower()}-a{int(args.attempt):02d}-{uuid.uuid4().hex[:10]}",
        **workspace_blob,
        "command": args.command,
        "env": {
            "RENTAGPU_TASK_ID": args.task_id,
            "RENTAGPU_ATTEMPT": str(args.attempt),
        },
        "timeout_seconds": timeout_seconds,
        "artifact_prefix": artifact_prefix,
        "gpu": args.gpu or str(modal_cfg.get("gpu") or "L4"),
        "submitted_at_epoch_ms": int(time.time() * 1000),
    }

    payload_dir = repo_root / "tmp" / "live_orchestrator" / "modal_payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_file = payload_dir / f"{payload['run_id']}.json"
    payload_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    entrypoint = str(modal_cfg.get("entrypoint") or "scripts/gpu_modal_app.py::submit")
    submit_timeout = _to_int(
        modal_cfg.get("submit_timeout_seconds")
        or timeouts_cfg.get("modal_submit_timeout_seconds"),
        7200,
    )
    cmd = [
        "modal",
        "run",
        entrypoint,
        "--payload-file",
        str(payload_file),
        "--execution-mode",
        mode,
    ]
    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=submit_timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "modal submission failed",
            {"returncode": proc.returncode, "stdout": _tail(proc.stdout), "stderr": _tail(proc.stderr)},
        )

    result = _parse_result_json(proc.stdout)
    return result, proc.stdout, proc.stderr


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    started_at = time.time()
    config_path = pathlib.Path(args.config)
    config = _load_config(config_path)

    backend = args.backend.strip().lower()
    if backend != "modal":
        _stderr_error(
            "unsupported_backend",
            f"unsupported backend '{args.backend}'; supported backends: modal",
        )
        return 64

    cwd = pathlib.Path.cwd()
    try:
        repo_root = _git_root(cwd)
        worktree = cwd
        files = _collect_workspace_files(worktree)
        workspace_archive = _create_workspace_archive(worktree, files)
    except Exception as exc:
        _stderr_error("workspace_packaging_error", str(exc))
        return 70

    policy_state_path = repo_root / DEFAULT_POLICY_STATE
    runs_jsonl_path = repo_root / DEFAULT_RUNS_JSONL
    policy_state = _load_policy_state(policy_state_path)
    decision = _decide_execution_mode(
        state=policy_state,
        policy=dict(config.get("policy") or {}),
        override=args.hot_mode,
        now_ts=started_at,
    )

    try:
        result, backend_stdout, backend_stderr = _run_modal_backend(
            repo_root=repo_root,
            args=args,
            config=config,
            mode=decision.mode,
            workspace_archive=workspace_archive,
        )
    except subprocess.TimeoutExpired as exc:
        _stderr_error(
            "backend_timeout",
            f"modal submit timed out after {exc.timeout}s",
            {"stdout": _tail(exc.stdout or ""), "stderr": _tail(exc.stderr or "")},
        )
        return 70
    except RuntimeError as exc:
        if len(exc.args) > 1 and isinstance(exc.args[1], dict):
            details = exc.args[1]
        else:
            details = None
        _stderr_error("backend_submission_error", str(exc.args[0]), details)
        return 70
    except Exception as exc:
        _stderr_error("backend_execution_error", str(exc))
        return 70
    finally:
        shutil_ok = True
        try:
            if workspace_archive.exists():
                workspace_archive.unlink()
            if workspace_archive.parent.exists():
                workspace_archive.parent.rmdir()
        except Exception:
            shutil_ok = False
        if not shutil_ok:
            print("[gpu_exec] warning: failed to fully clean temp workspace archive", file=sys.stderr)

    finished_at = time.time()
    exit_code = _to_int(result.get("exit_code"), 70)
    startup_latency_ms = _to_int(result.get("startup_latency_ms"), 0)
    queue_time_ms = _to_int(result.get("queue_time_ms"), startup_latency_ms)
    duration_ms = _to_int(result.get("duration_ms"), int((finished_at - started_at) * 1000))

    history = list(policy_state.get("history") or [])
    history.append(
        {
            "task_id": args.task_id,
            "attempt": int(args.attempt),
            "finished_at_epoch": finished_at,
            "mode": decision.mode,
            "cold_start": bool(result.get("cold_start")),
            "startup_latency_ms": startup_latency_ms,
            "duration_ms": duration_ms,
            "exit_code": exit_code,
        }
    )
    history_limit = _to_int(config.get("policy", {}).get("history_limit"), 100)
    if len(history) > history_limit:
        history = history[-history_limit:]

    policy_state["mode"] = decision.mode
    policy_state["last_activity_epoch"] = finished_at
    policy_state["history"] = history
    _save_policy_state(policy_state_path, policy_state)

    run_row = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished_at)),
        "backend": backend,
        "task_id": args.task_id,
        "attempt": int(args.attempt),
        "decision_mode": decision.mode,
        "decision_reason": decision.reason,
        "run_id": result.get("run_id"),
        "gpu_type": result.get("gpu_type"),
        "cold_start": bool(result.get("cold_start")),
        "queue_time_ms": queue_time_ms,
        "startup_latency_ms": startup_latency_ms,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "artifact_uri": result.get("artifact_uri"),
    }
    _append_jsonl(runs_jsonl_path, run_row)

    print(
        "[gpu_exec] "
        f"backend={backend} mode={decision.mode} reason={decision.reason} "
        f"run_id={result.get('run_id')} gpu={result.get('gpu_type')} "
        f"cold_start={bool(result.get('cold_start'))} "
        f"startup_latency_ms={startup_latency_ms} duration_ms={duration_ms} "
        f"exit_code={exit_code} artifacts={result.get('artifact_uri')}"
    )
    stdout_tail = str(result.get("stdout_tail") or "").strip()
    stderr_tail = str(result.get("stderr_tail") or "").strip()
    if stdout_tail:
        print("[gpu_exec][remote_stdout_tail]")
        print(stdout_tail)
    if stderr_tail:
        print("[gpu_exec][remote_stderr_tail]", file=sys.stderr)
        print(stderr_tail, file=sys.stderr)
    if backend_stdout.strip():
        print("[gpu_exec][modal_stdout_tail]")
        print(_tail(backend_stdout))
    if backend_stderr.strip():
        print("[gpu_exec][modal_stderr_tail]", file=sys.stderr)
        print(_tail(backend_stderr), file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
