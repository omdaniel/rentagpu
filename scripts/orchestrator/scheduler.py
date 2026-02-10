#!/usr/bin/env python3
"""Compatibility facade for the modular live orchestrator scheduler."""

from __future__ import annotations

import sys

from .scheduler_args import parse_args, parse_profiles, resolve_state_path, validate_args
from .scheduler_detection import (
    classify_failure,
    detect_quota_or_rate_limit,
    is_model_unsupported,
    timeout_stream_text,
)
from .scheduler_engine import main
from .scheduler_policy import (
    activate_quota_cooldown,
    all_done,
    block_all_pending_tasks_for_quota,
    mark_task_blocked,
    maybe_escalate,
    propagate_dependency_blocks,
    retry_or_block_task,
    task_ready,
)
from .scheduler_probe import filter_profiles_by_model_probe, probe_model_support
from .scheduler_report import compact_text, render_state_report
from .scheduler_runtime import (
    build_prompt,
    changed_files_in_worktree,
    default_worker_template,
    ensure_dirs,
    ensure_worktree,
    format_template,
    git_root,
    read_tail,
    run_cmd,
    run_validation_commands,
    terminate_process,
    within_allowed_files,
)
from .scheduler_state import (
    EventSink,
    now_iso,
    restore_runtime_state,
    safe_error_text,
    ts_iso,
    write_state,
)
from .scheduler_worker import handle_finished_worker, launch_task

__all__ = [
    "EventSink",
    "activate_quota_cooldown",
    "all_done",
    "block_all_pending_tasks_for_quota",
    "build_prompt",
    "changed_files_in_worktree",
    "classify_failure",
    "compact_text",
    "default_worker_template",
    "detect_quota_or_rate_limit",
    "ensure_dirs",
    "ensure_worktree",
    "filter_profiles_by_model_probe",
    "format_template",
    "git_root",
    "handle_finished_worker",
    "is_model_unsupported",
    "launch_task",
    "main",
    "mark_task_blocked",
    "maybe_escalate",
    "now_iso",
    "parse_args",
    "parse_profiles",
    "probe_model_support",
    "propagate_dependency_blocks",
    "read_tail",
    "render_state_report",
    "resolve_state_path",
    "restore_runtime_state",
    "retry_or_block_task",
    "run_cmd",
    "run_validation_commands",
    "safe_error_text",
    "task_ready",
    "terminate_process",
    "timeout_stream_text",
    "ts_iso",
    "validate_args",
    "within_allowed_files",
    "write_state",
]


if __name__ == "__main__":
    sys.exit(main())
