#!/usr/bin/env python3
"""Modal runtime for remote CUDA/Warp command execution."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

import modal

CONFIG_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "gpu_backend.toml"
DEFAULT_CONFIG: dict[str, Any] = {
    "modal": {
        "app_name": "rentagpu-executor",
        "gpu": "L4",
        "python_version": "3.12",
        "image": "nvidia/cuda:12.4.1-devel-ubuntu22.04",
        "scaledown_window": 600,
        "min_containers": 0,
        "hot_scaledown_window": 1200,
        "hot_min_containers": 1,
        "default_timeout_seconds": 1800,
    },
    "artifacts": {
        "s3_bucket": "",
        "s3_prefix": "gpu-runs",
        "s3_region": "us-east-1",
        "s3_endpoint_url": "",
    },
}

_REMOTE_INVOCATIONS = 0


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    with CONFIG_PATH.open("rb") as fh:
        parsed = tomllib.load(fh)
    merged: dict[str, Any] = dict(DEFAULT_CONFIG)
    for key, value in parsed.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            section = dict(merged[key])
            section.update(value)
            merged[key] = section
        else:
            merged[key] = value
    return merged


def _tail(text: str, max_lines: int = 120) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _safe_extract_tar(archive_path: pathlib.Path, destination: pathlib.Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:gz") as tf:
        dest_root = destination.resolve()
        for member in tf.getmembers():
            target = (destination / member.name).resolve()
            if os.path.commonpath([str(dest_root), str(target)]) != str(dest_root):
                raise ValueError(f"tar archive contains unsafe path: {member.name}")
        tf.extractall(destination)


def _download_s3_uri(uri: str, destination: pathlib.Path, cfg: dict[str, Any]) -> None:
    # boto3 import is deferred so this module can still be imported without it.
    import boto3

    if not uri.startswith("s3://"):
        raise ValueError(f"unsupported URI scheme for workspace archive: {uri}")
    path = uri[len("s3://") :]
    if "/" not in path:
        raise ValueError(f"invalid s3 uri: {uri}")
    bucket, key = path.split("/", 1)
    client = boto3.client(
        "s3",
        region_name=str(cfg.get("s3_region") or "us-east-1"),
        endpoint_url=str(cfg.get("s3_endpoint_url") or "") or None,
    )
    client.download_file(bucket, key, str(destination))


def _upload_file_to_s3(
    *,
    local_path: pathlib.Path,
    bucket: str,
    key: str,
    cfg: dict[str, Any],
) -> str:
    import boto3

    client = boto3.client(
        "s3",
        region_name=str(cfg.get("s3_region") or "us-east-1"),
        endpoint_url=str(cfg.get("s3_endpoint_url") or "") or None,
    )
    client.upload_file(str(local_path), bucket, key)
    return f"s3://{bucket}/{key}"


def _materialize_workspace_archive(
    payload: dict[str, Any],
    staging_dir: pathlib.Path,
    cfg: dict[str, Any],
) -> pathlib.Path:
    archive_path = staging_dir / "workspace.tar.gz"
    if payload.get("workspace_tar_b64"):
        archive_path.write_bytes(base64.b64decode(str(payload["workspace_tar_b64"])))
        return archive_path

    workspace_tar = str(payload.get("workspace_tar") or "")
    if workspace_tar.startswith("s3://"):
        _download_s3_uri(workspace_tar, archive_path, cfg)
        return archive_path

    if workspace_tar:
        src = pathlib.Path(workspace_tar)
        if not src.exists():
            raise FileNotFoundError(f"workspace archive path not found: {workspace_tar}")
        shutil.copy2(src, archive_path)
        return archive_path

    raise ValueError("payload is missing workspace tar archive details")


def _execute_payload(payload: dict[str, Any], execution_mode: str) -> dict[str, Any]:
    global _REMOTE_INVOCATIONS

    config = _load_config()
    artifacts_cfg = config.get("artifacts", {})
    run_id = str(payload.get("run_id") or f"run_{uuid.uuid4().hex}")
    submitted_at_ms = int(payload.get("submitted_at_epoch_ms") or 0)
    handler_start = time.time()
    startup_latency_ms = (
        max(0, int((handler_start * 1000) - submitted_at_ms)) if submitted_at_ms else 0
    )

    cold_start = _REMOTE_INVOCATIONS == 0
    _REMOTE_INVOCATIONS += 1

    with tempfile.TemporaryDirectory(prefix="rentagpu_modal_") as tmp:
        tmp_dir = pathlib.Path(tmp)
        archive_path = _materialize_workspace_archive(payload, tmp_dir, artifacts_cfg)
        workspace_dir = tmp_dir / "workspace"
        _safe_extract_tar(archive_path, workspace_dir)

        command = str(payload["command"])
        timeout_seconds = int(payload.get("timeout_seconds") or 1200)
        env = os.environ.copy()
        env["RENTAGPU_EXECUTION_MODE"] = execution_mode
        env["RENTAGPU_RUN_ID"] = run_id
        for key, value in dict(payload.get("env") or {}).items():
            env[str(key)] = str(value)

        started = time.time()
        try:
            proc = subprocess.run(
                command,
                cwd=workspace_dir,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = _stream_text(exc.stdout)
            stderr = _stream_text(exc.stderr) + f"\ncommand timed out after {timeout_seconds}s"
            exit_code = 124
            timed_out = True
        finished = time.time()
        duration_ms = max(0, int((finished - started) * 1000))

        artifact_prefix = str(payload.get("artifact_prefix") or "gpu-runs")
        artifact_root = tmp_dir / "artifacts"
        artifact_root.mkdir(parents=True, exist_ok=True)

        stdout_path = artifact_root / "stdout.log"
        stderr_path = artifact_root / "stderr.log"
        metadata_path = artifact_root / "metadata.json"
        workspace_after_path = artifact_root / "workspace_after.tar.gz"

        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")

        with tarfile.open(workspace_after_path, mode="w:gz") as tf:
            tf.add(workspace_dir, arcname="workspace")

        metadata: dict[str, Any] = {
            "run_id": run_id,
            "command": command,
            "execution_mode": execution_mode,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "cold_start": cold_start,
            "gpu_type": str(payload.get("gpu") or config.get("modal", {}).get("gpu", "L4")),
            "duration_ms": duration_ms,
            "submitted_at_epoch_ms": submitted_at_ms,
            "startup_latency_ms": startup_latency_ms,
            "started_at_epoch_ms": int(started * 1000),
            "finished_at_epoch_ms": int(finished * 1000),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        artifact_uri = ""
        bucket = str(artifacts_cfg.get("s3_bucket") or "")
        if bucket:
            base = artifact_prefix.strip("/")
            key_prefix = f"{base}/{run_id}" if base else run_id
            _upload_file_to_s3(
                local_path=stdout_path,
                bucket=bucket,
                key=f"{key_prefix}/stdout.log",
                cfg=artifacts_cfg,
            )
            _upload_file_to_s3(
                local_path=stderr_path,
                bucket=bucket,
                key=f"{key_prefix}/stderr.log",
                cfg=artifacts_cfg,
            )
            _upload_file_to_s3(
                local_path=metadata_path,
                bucket=bucket,
                key=f"{key_prefix}/metadata.json",
                cfg=artifacts_cfg,
            )
            _upload_file_to_s3(
                local_path=workspace_after_path,
                bucket=bucket,
                key=f"{key_prefix}/workspace_after.tar.gz",
                cfg=artifacts_cfg,
            )
            artifact_uri = f"s3://{bucket}/{key_prefix}/"
        else:
            artifact_uri = f"unpersisted://{run_id}"

        return {
            "run_id": run_id,
            "exit_code": exit_code,
            "cold_start": cold_start,
            "gpu_type": metadata["gpu_type"],
            "duration_ms": duration_ms,
            "artifact_uri": artifact_uri,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
            "startup_latency_ms": startup_latency_ms,
            "queue_time_ms": startup_latency_ms,
            "attempt_count_in_container": _REMOTE_INVOCATIONS,
            "cold_start_samples_ms": [startup_latency_ms] if cold_start else [],
        }


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


CONFIG = _load_config()
MODAL_CONFIG = CONFIG.get("modal", {})
APP_NAME = str(MODAL_CONFIG.get("app_name") or "rentagpu-executor")
GPU_TYPE = str(MODAL_CONFIG.get("gpu") or "L4")
DEFAULT_TIMEOUT_SECONDS = _to_int(MODAL_CONFIG.get("default_timeout_seconds"), 1800)
HYBRID_SCALEDOWN = _to_int(MODAL_CONFIG.get("scaledown_window"), 600)
HYBRID_MIN_CONTAINERS = _to_int(MODAL_CONFIG.get("min_containers"), 0)
HOT_SCALEDOWN = _to_int(MODAL_CONFIG.get("hot_scaledown_window"), 1200)
HOT_MIN_CONTAINERS = _to_int(MODAL_CONFIG.get("hot_min_containers"), 1)
BASE_IMAGE = str(MODAL_CONFIG.get("image") or "nvidia/cuda:12.4.1-devel-ubuntu22.04")
PYTHON_VERSION = str(MODAL_CONFIG.get("python_version") or "3.12")

image = (
    modal.Image.from_registry(BASE_IMAGE, add_python=PYTHON_VERSION)
    .apt_install("build-essential", "git", "python3-dev")
    .pip_install("warp-lang", "boto3")
)

cache_volume = modal.Volume.from_name("rentagpu-cache", create_if_missing=True)
app = modal.App(APP_NAME)


@app.function(
    image=image,
    gpu=GPU_TYPE,
    timeout=DEFAULT_TIMEOUT_SECONDS,
    min_containers=HYBRID_MIN_CONTAINERS,
    scaledown_window=HYBRID_SCALEDOWN,
    volumes={"/cache": cache_volume},
)
def run_gpu_job_hybrid(payload: dict[str, Any]) -> dict[str, Any]:
    return _execute_payload(payload, execution_mode="hybrid")


@app.function(
    image=image,
    gpu=GPU_TYPE,
    timeout=DEFAULT_TIMEOUT_SECONDS,
    min_containers=HOT_MIN_CONTAINERS,
    scaledown_window=HOT_SCALEDOWN,
    volumes={"/cache": cache_volume},
)
def run_gpu_job_hot(payload: dict[str, Any]) -> dict[str, Any]:
    return _execute_payload(payload, execution_mode="hot")


@app.local_entrypoint()
def submit(payload_file: str, execution_mode: str = "hybrid") -> None:
    payload_path = pathlib.Path(payload_file)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    mode = execution_mode.strip().lower()
    if mode not in {"hybrid", "hot"}:
        raise ValueError("execution_mode must be 'hybrid' or 'hot'")
    fn = run_gpu_job_hot if mode == "hot" else run_gpu_job_hybrid
    result = fn.remote(payload)
    # Emit machine-readable output for the local bridge.
    print(json.dumps(result, sort_keys=True))
