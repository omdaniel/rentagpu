from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


def compact_text(value: Optional[str], max_chars: int = 220) -> str:
    if not value:
        return ""
    collapsed = " ".join(value.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 3] + "..."


def render_state_report(state_path: Path) -> str:
    if not state_path.exists():
        return f"state file not found: {state_path}"

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    summary = payload.get("summary", {})
    orchestrator = payload.get("orchestrator", {})
    if not isinstance(orchestrator, dict):
        orchestrator = {}
    updated = payload.get("updated_at", "unknown")
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []

    blocked_rows: list[str] = []
    errored_rows: list[str] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id", "unknown"))
        status = str(item.get("status", "unknown"))
        block_reason = compact_text(item.get("block_reason"))
        last_error = compact_text(item.get("last_error"))
        attempts = item.get("attempts", 0)

        if status == "blocked":
            reason = block_reason or last_error or "(no reason recorded)"
            blocked_rows.append(f"- {task_id} (attempts={attempts}): {reason}")
        elif last_error:
            errored_rows.append(
                f"- {task_id} [{status}] (attempts={attempts}): {last_error}"
            )

    lines: list[str] = []
    lines.append(f"state: {state_path}")
    lines.append(f"updated_at: {updated}")
    lines.append(
        "summary: "
        f"pending={summary.get('pending', 0)} "
        f"running={summary.get('running', 0)} "
        f"completed={summary.get('completed', 0)} "
        f"blocked={summary.get('blocked', 0)}"
    )
    raw_cooldown_until = orchestrator.get("quota_cooldown_until")
    raw_quota_total = orchestrator.get("quota_failures_total", 0)
    cooldown_remaining = 0
    if isinstance(raw_cooldown_until, (int, float)) and raw_cooldown_until > 0:
        cooldown_remaining = max(0, int(raw_cooldown_until - time.time()))
    quota_total = int(raw_quota_total) if isinstance(raw_quota_total, (int, float)) else 0
    lines.append(
        "quota: "
        f"failures_total={quota_total} "
        f"cooldown_remaining_seconds={cooldown_remaining}"
    )
    lines.append("")
    lines.append("blocked tasks:")
    if blocked_rows:
        lines.extend(blocked_rows)
    else:
        lines.append("- none")
    lines.append("")
    lines.append("latest task errors:")
    if errored_rows:
        lines.extend(errored_rows[:20])
        if len(errored_rows) > 20:
            lines.append(f"- ... {len(errored_rows) - 20} more")
    else:
        lines.append("- none")
    return "\n".join(lines)
