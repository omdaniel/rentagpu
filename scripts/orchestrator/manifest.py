from __future__ import annotations

import json
import re
from pathlib import Path

from .models import TaskSpec, TaskState


def parse_section(lines: list[str], title: str) -> list[str]:
    header = f"## {title}"
    in_section = False
    section: list[str] = []
    for line in lines:
        if not in_section:
            if line.strip() == header:
                in_section = True
            continue
        if line.startswith("## "):
            break
        section.append(line.rstrip("\n"))
    return section


def normalize_allowed_file(path_token: str) -> str:
    value = path_token.strip()
    if " (" in value:
        value = value.split(" (", 1)[0].strip()
    return value


def parse_allowed_files(lines: list[str]) -> set[str]:
    section = parse_section(lines, "Allowed Files")
    allowed: set[str] = set()
    for line in section:
        match = re.match(r"^\s*-\s+`([^`]+)`", line)
        if not match:
            continue
        allowed.add(normalize_allowed_file(match.group(1)))
    return allowed


def parse_validation_commands(lines: list[str]) -> list[str]:
    section = parse_section(lines, "Validation Commands")
    block_lines: list[str] = []
    in_code = False
    for line in section:
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                continue
            break
        if in_code:
            block_lines.append(line)

    commands: list[str] = []
    current = ""
    for line in block_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if current:
            current = f"{current} {stripped}"
        else:
            current = stripped
        if stripped.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        commands.append(current)
        current = ""
    if current:
        commands.append(current)
    return commands


def load_manifest(
    repo_root: Path,
    manifest_path: Path,
    *,
    allow_empty_allowed_files: bool,
) -> dict[str, TaskState]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    task_entries = payload.get("tasks")
    if not isinstance(task_entries, list):
        raise ValueError(f"Invalid manifest format: {manifest_path}")

    tasks: dict[str, TaskState] = {}
    for entry in task_entries:
        task_id = str(entry["id"])
        if task_id in tasks:
            raise ValueError(f"Duplicate task id in manifest: {task_id}")
        packet_rel = Path(entry["packet"])
        packet_path = (repo_root / packet_rel).resolve()
        if not packet_path.exists():
            raise FileNotFoundError(f"Packet path not found for {task_id}: {packet_path}")
        packet_lines = packet_path.read_text(encoding="utf-8").splitlines()
        allowed = parse_allowed_files(packet_lines)
        if not allowed and not allow_empty_allowed_files:
            raise ValueError(
                f"Packet {task_id} has no parsed 'Allowed Files'. "
                "Failing closed; pass --allow-empty-allowed-files to override."
            )
        validations = parse_validation_commands(packet_lines)
        spec = TaskSpec(
            task_id=task_id,
            packet_path=packet_path,
            backlog_path=(repo_root / Path(entry["backlog"])).resolve()
            if entry.get("backlog")
            else None,
            depends_on=list(entry.get("depends_on", [])),
            can_run_in_parallel_with=list(entry.get("can_run_in_parallel_with", [])),
            allowed_files=allowed,
            validation_commands=validations,
        )
        tasks[task_id] = TaskState(spec=spec)

    for task in tasks.values():
        missing = [dep for dep in task.spec.depends_on if dep not in tasks]
        if missing:
            raise ValueError(
                f"Task {task.spec.task_id} depends on unknown tasks: {', '.join(missing)}"
            )
    return tasks


def slugify(value: str) -> str:
    lowered = value.lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = lowered.strip("-")
    return lowered or "task"


def task_branch_name(task: TaskSpec) -> str:
    suffix = slugify(task.packet_path.stem)
    return f"codex/{task.task_id.lower()}-{suffix}"
