from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ModelProfile:
    model: str
    reasoning: str


@dataclass
class TaskSpec:
    task_id: str
    packet_path: Path
    backlog_path: Optional[Path]
    depends_on: list[str]
    can_run_in_parallel_with: list[str]
    allowed_files: set[str]
    validation_commands: list[str]


@dataclass
class TaskRuntime:
    status: str = "pending"  # pending|running|completed|blocked
    attempts: int = 0
    profile_index: int = 0
    compile_failures_total: int = 0
    runtime_failures_total: int = 0
    quota_failures_total: int = 0
    other_failures_total: int = 0
    compile_failures_level: int = 0
    runtime_failures_level: int = 0
    last_failure_kind: Optional[str] = None
    last_error: Optional[str] = None
    block_reason: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    worktree_path: Optional[Path] = None
    branch_name: Optional[str] = None
    prompt_file: Optional[Path] = None
    log_file: Optional[Path] = None
    next_eligible_at: Optional[float] = None
    last_changed_files: list[str] = field(default_factory=list)


@dataclass
class TaskState:
    spec: TaskSpec
    runtime: TaskRuntime = field(default_factory=TaskRuntime)


@dataclass
class RuntimeDirs:
    root: Path
    prompts: Path
    logs: Path
    state_file: Path
    events_file: Path
    worktrees_root: Path


@dataclass
class QuotaRuntime:
    cooldown_until: float = 0.0
    last_detected_at: Optional[float] = None
    total_failures: int = 0
