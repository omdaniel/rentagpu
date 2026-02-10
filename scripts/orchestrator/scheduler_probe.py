from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .models import ModelProfile
from .scheduler_detection import detect_quota_or_rate_limit, is_model_unsupported

if TYPE_CHECKING:
    from .scheduler_state import EventSink


def probe_model_support(
    *,
    repo_root: Path,
    model: str,
    timeout_seconds: int,
) -> tuple[bool, str]:
    cmd = [
        "codex",
        "exec",
        "-m",
        model,
        "-c",
        "model_reasoning_effort=low",
        "--cd",
        str(repo_root),
        "--skip-git-repo-check",
        "--json",
        "Reply with OK",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return True, f"probe timed out after {timeout_seconds}s; treating as supported"

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and '"turn.completed"' in output:
        return True, "supported"
    if is_model_unsupported(output):
        return False, "unsupported by current account"
    quota_reason = detect_quota_or_rate_limit(output)
    if quota_reason:
        return True, f"probe hit {quota_reason}; treating model as supported"
    # Keep probe failures conservative: treat unknown probe failures as supported,
    # and let runtime retries/escalations handle transient issues.
    return True, "probe inconclusive; treating as supported"


def filter_profiles_by_model_probe(
    *,
    repo_root: Path,
    profiles: list[ModelProfile],
    timeout_seconds: int,
    events: EventSink,
) -> list[ModelProfile]:
    model_status: dict[str, bool] = {}
    for profile in profiles:
        if profile.model in model_status:
            continue
        supported, reason = probe_model_support(
            repo_root=repo_root,
            model=profile.model,
            timeout_seconds=timeout_seconds,
        )
        model_status[profile.model] = supported
        event_name = "model_probe_ok" if supported else "model_probe_drop"
        events.emit(
            event_name,
            f"model probe {profile.model}: {reason}",
            model=profile.model,
            supported=supported,
            reason=reason,
        )

    filtered = [p for p in profiles if model_status.get(p.model, True)]
    if not filtered:
        raise RuntimeError(
            "All models were removed by --probe-models. "
            "Adjust --executor-profiles or authentication."
        )
    return filtered
