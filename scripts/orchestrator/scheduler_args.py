from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from .models import ModelProfile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live orchestrator for packet tasks with model escalation."
    )
    parser.add_argument(
        "--manifest",
        default="docs/executor_packets/wave_b/manifest.json",
        help="Path to task manifest JSON.",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root (git top-level for worktree commands).",
    )
    parser.add_argument(
        "--runtime-dir",
        default="tmp/live_orchestrator",
        help="Directory for prompts, logs, and state outputs.",
    )
    parser.add_argument(
        "--worktree-root",
        default=None,
        help="Worktree root directory (default: <runtime-dir>/worktrees).",
    )
    parser.add_argument(
        "--base-ref",
        default="HEAD",
        help="Base ref for new worktree branches.",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=3,
        help="Max concurrent running workers.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=3.0,
        help="Seconds between scheduler iterations.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=6,
        help="Max launch attempts per task before blocking.",
    )
    parser.add_argument(
        "--quota-cooldown-seconds",
        type=int,
        default=900,
        help=(
            "Global cooldown window after a quota/rate-limit failure before "
            "new worker launches are allowed."
        ),
    )
    parser.add_argument(
        "--quota-max-failures-per-task",
        type=int,
        default=3,
        help="Block task after this many quota/rate-limit failures.",
    )
    parser.add_argument(
        "--quota-fail-fast",
        action="store_true",
        help=(
            "On first quota/rate-limit failure, block all pending tasks "
            "immediately instead of waiting for retries."
        ),
    )
    parser.add_argument(
        "--escalate-after-compile",
        type=int,
        default=2,
        help="Escalate after this many compile failures at current profile.",
    )
    parser.add_argument(
        "--escalate-after-runtime",
        type=int,
        default=2,
        help="Escalate after this many runtime failures at current profile.",
    )
    parser.add_argument(
        "--executor-profiles",
        default=(
            "gpt-5.3-codex:low;"
            "gpt-5.3-codex:medium;"
            "gpt-5.3-codex:high;"
            "gpt-5.3-codex:xhigh;"
            "gpt-5.1-codex-max:high"
        ),
        help=(
            "Escalation ladder as semicolon-separated model:reasoning entries, "
            "e.g. 'gpt-5.3-codex:low;gpt-5.3-codex:medium;gpt-5.3-codex:high'. "
            "Reasoning values: none|minimal|low|medium|high|xhigh "
            "(alias 'extrahigh' is accepted and mapped to xhigh)."
        ),
    )
    parser.add_argument(
        "--worker-command-template",
        default=None,
        help=(
            "Command template used to launch one worker. Supports placeholders "
            "{task_id},{model},{reasoning},{worktree},{prompt_file},{log_file} "
            "and *_q quoted variants."
        ),
    )
    parser.add_argument(
        "--command-timeout-seconds",
        type=int,
        default=1800,
        help="Timeout per validation command execution.",
    )
    parser.add_argument(
        "--validation-executor",
        choices=["dual", "orchestrator"],
        default="dual",
        help=(
            "Validation execution strategy. 'dual' keeps worker-side validation "
            "instructions plus orchestrator-side validation (default). "
            "'orchestrator' tells workers to skip validation and lets the "
            "orchestrator run validation commands exclusively."
        ),
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=int,
        default=0,
        help=(
            "Optional hard timeout for a worker process. 0 disables worker timeout "
            "(default)."
        ),
    )
    parser.add_argument(
        "--allow-empty-allowed-files",
        action="store_true",
        help=(
            "Allow packets with no parsed 'Allowed Files' entries. By default, "
            "empty allowed-file sets are rejected to fail closed."
        ),
    )
    parser.add_argument(
        "--probe-models",
        action="store_true",
        help=(
            "Probe unique models in --executor-profiles via local codex CLI and "
            "drop unsupported models before orchestration starts."
        ),
    )
    parser.add_argument(
        "--probe-model-timeout-seconds",
        type=int,
        default=60,
        help="Timeout per model probe request in seconds.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help=(
            "Ignore existing runtime state and start fresh. By default, the "
            "orchestrator resumes from runtime state when available."
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print a compact report from the current state file and exit.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help=(
            "Optional path override for state.json. Defaults to "
            "<runtime-dir>/state.json."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan and schedule tasks without launching workers or running validation.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.max_parallel < 1:
        raise ValueError("--max-parallel must be at least 1.")
    if args.max_attempts < 1:
        raise ValueError("--max-attempts must be at least 1.")
    if args.quota_cooldown_seconds < 1:
        raise ValueError("--quota-cooldown-seconds must be at least 1.")
    if args.quota_max_failures_per_task < 1:
        raise ValueError("--quota-max-failures-per-task must be at least 1.")
    if args.escalate_after_compile < 1:
        raise ValueError("--escalate-after-compile must be at least 1.")
    if args.escalate_after_runtime < 1:
        raise ValueError("--escalate-after-runtime must be at least 1.")
    if args.command_timeout_seconds < 1:
        raise ValueError("--command-timeout-seconds must be at least 1.")
    if args.worker_timeout_seconds < 0:
        raise ValueError("--worker-timeout-seconds must be >= 0.")
    if args.poll_interval < 0:
        raise ValueError("--poll-interval must be >= 0.")
    if args.probe_model_timeout_seconds < 1:
        raise ValueError("--probe-model-timeout-seconds must be at least 1.")


def parse_profiles(spec: str) -> list[ModelProfile]:
    def normalize_reasoning_effort(value: str) -> str:
        raw = value.strip().lower()
        alias_map = {
            "extrahigh": "xhigh",
            "extra_high": "xhigh",
            "extra-high": "xhigh",
        }
        normalized = alias_map.get(raw, raw)
        valid = {"none", "minimal", "low", "medium", "high", "xhigh"}
        if normalized not in valid:
            raise ValueError(
                f"Invalid reasoning effort '{value}'. "
                "Expected one of: none, minimal, low, medium, high, xhigh."
            )
        return normalized

    profiles: list[ModelProfile] = []
    for token in spec.split(";"):
        token = token.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(
                f"Invalid profile entry '{token}'. Expected '<model>:<reasoning>'."
            )
        model, reasoning = token.split(":", 1)
        profiles.append(
            ModelProfile(
                model=model.strip(),
                reasoning=normalize_reasoning_effort(reasoning),
            )
        )
    if not profiles:
        raise ValueError("No executor profiles parsed.")
    return profiles


def resolve_state_path(
    *,
    repo_root: Optional[Path],
    repo_root_arg: str,
    runtime_dir_arg: str,
    state_file_arg: Optional[str],
) -> Path:
    if state_file_arg:
        return Path(state_file_arg).expanduser().resolve()

    runtime_dir = Path(runtime_dir_arg)
    if runtime_dir.is_absolute():
        base = runtime_dir
    elif repo_root is not None:
        base = (repo_root / runtime_dir).resolve()
    else:
        base = (Path(repo_root_arg).resolve() / runtime_dir).resolve()
    return base / "state.json"
