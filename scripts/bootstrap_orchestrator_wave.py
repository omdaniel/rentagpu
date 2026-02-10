#!/usr/bin/env python3
"""Bootstrap packet + manifest scaffolding for live_orchestrator.py."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

TEMPLATE_TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
PROJECT_CONTEXT_MARKER = "## Project Orchestration Context"
EXECUTOR_MARKER = "## Orchestrator/Executor Contract"
PLANNING_MARKER = "## Orchestrator Planning Contract"


class BootstrapError(RuntimeError):
    """Raised when bootstrap validation or rendering fails."""


@dataclass(frozen=True)
class BootstrapPaths:
    repo_root: Path
    backlog_dir: Path
    packets_dir: Path
    scripts_dir: Path
    tmp_dir: Path
    orchestrator_target: Path


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def templates_dir() -> Path:
    return script_dir() / "templates" / "bootstrap_orchestrator"


def log(message: str) -> None:
    print(f"[bootstrap] {message}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap orchestrator wave scaffolding.")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root. Defaults to git top-level or cwd.",
    )
    parser.add_argument(
        "--wave",
        default="wave_1",
        help="Wave directory name under docs/executor_packets/. Default: wave_1",
    )
    parser.add_argument(
        "--id-prefix",
        default="W",
        help="Task ID prefix. Default: W",
    )
    parser.add_argument(
        "--start-id",
        default=101,
        type=int,
        help="First numeric task id. Default: 101",
    )
    parser.add_argument(
        "--task-count",
        default=3,
        type=int,
        help="Number of task skeletons to generate. Default: 3",
    )
    parser.add_argument(
        "--orchestrator-source",
        default=None,
        help="Path to source scripts/live_orchestrator.py to copy.",
    )
    parser.add_argument(
        "--agents-context-file",
        default=None,
        help="Optional markdown/text file appended under project orchestration context.",
    )
    parser.add_argument(
        "--agents-context-text",
        default=None,
        help="Optional one-line context text appended under project orchestration context.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing generated files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended actions without writing files.",
    )
    return parser.parse_args(argv)


def detect_repo_root(repo_root_arg: str | None) -> Path:
    if repo_root_arg:
        return Path(repo_root_arg).expanduser().resolve()

    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return Path(proc.stdout.strip()).resolve()
    return Path.cwd().resolve()


def validate_args(args: argparse.Namespace, repo_root: Path) -> None:
    if args.task_count < 1:
        raise BootstrapError("--task-count must be >= 1.")
    if args.start_id < 0:
        raise BootstrapError("--start-id must be >= 0.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.wave):
        raise BootstrapError("--wave contains invalid characters.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.id_prefix):
        raise BootstrapError("--id-prefix must start with a letter.")
    if not repo_root.is_dir():
        raise BootstrapError(f"--repo-root does not exist: {repo_root}")

    if args.agents_context_file:
        context_file = Path(args.agents_context_file).expanduser().resolve()
        if not context_file.is_file():
            raise BootstrapError(f"--agents-context-file not found: {context_file}")

    if args.orchestrator_source:
        source = Path(args.orchestrator_source).expanduser().resolve()
        if not source.is_file():
            raise BootstrapError(
                "cannot install scripts/live_orchestrator.py; "
                f"source not found at '{source}'."
            )


def load_template(name: str) -> str:
    path = templates_dir() / name
    if not path.is_file():
        raise BootstrapError(f"template not found: {path}")
    return path.read_text(encoding="utf-8")


def render_template(name: str, values: dict[str, str]) -> str:
    template = load_template(name)
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.add(key)
            return match.group(0)
        return values[key]

    rendered = TEMPLATE_TOKEN_RE.sub(replace, template)
    if missing:
        missing_tokens = ", ".join(sorted(missing))
        raise BootstrapError(f"template {name} missing values for: {missing_tokens}")

    unresolved = sorted(set(TEMPLATE_TOKEN_RE.findall(rendered)))
    if unresolved:
        unresolved_tokens = ", ".join(unresolved)
        raise BootstrapError(f"template {name} has unresolved placeholders: {unresolved_tokens}")
    return rendered


def file_contains_line(path: Path, target_line: str) -> bool:
    if not path.is_file():
        return False
    return any(line == target_line for line in path.read_text(encoding="utf-8").splitlines())


def mkdirp(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        log(f"[dry-run] mkdir -p {path}")
        return
    path.mkdir(parents=True, exist_ok=True)


def write_file(
    path: Path,
    content: str,
    *,
    overwrite: bool,
    dry_run: bool,
    mode: int = 0o644,
) -> None:
    should_write = True
    if path.exists() and not overwrite:
        should_write = False

    if dry_run:
        if should_write:
            log(f"[dry-run] write {path}")
        else:
            log(f"[dry-run] skip existing {path}")
        return

    if not should_write:
        log(f"skip existing {path} (use --overwrite to replace)")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)
    log(f"wrote {path}")


def load_agents_context(args: argparse.Namespace) -> str:
    content_parts: list[str] = []
    if args.agents_context_file:
        context_file = Path(args.agents_context_file).expanduser().resolve()
        content_parts.append(context_file.read_text(encoding="utf-8"))
    if args.agents_context_text:
        content_parts.append(args.agents_context_text)
    return "\n".join(part for part in content_parts if part)


def install_orchestrator(
    *,
    target_path: Path,
    source_path: Path | None,
    overwrite: bool,
    dry_run: bool,
) -> None:
    if target_path.exists() and not overwrite:
        log(f"skip existing {target_path} (use --overwrite to replace)")
        return

    if source_path is None:
        raise BootstrapError(
            "--orchestrator-source is required when scripts/live_orchestrator.py "
            "does not already exist (embedded fallback removed)."
        )

    if dry_run:
        log(f"[dry-run] copy {source_path} -> {target_path}")
        return

    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
    os.chmod(target_path, 0o755)
    log(f"installed {target_path} from {source_path}")


def task_dependencies(index: int, task_ids: list[str]) -> list[str]:
    if len(task_ids) <= 2:
        return []
    if index < 2:
        return []
    if index == 2:
        return [task_ids[0], task_ids[1]]
    return [task_ids[index - 1]]


def task_parallel_hint(index: int, task_ids: list[str]) -> list[str]:
    if len(task_ids) < 2:
        return []
    if index == 0:
        return [task_ids[1]]
    if index == 1:
        return [task_ids[0]]
    return []


def append_text(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def append_agents_contract(
    *,
    agents_path: Path,
    agents_context_content: str,
    dry_run: bool,
) -> None:
    executor_contract = load_template("agents_executor_contract.md.tmpl").strip("\n")
    planning_contract = load_template("agents_planning_contract.md.tmpl").strip("\n")

    if dry_run:
        if not agents_path.exists():
            log(f"[dry-run] create {agents_path}")

        if agents_path.is_file() and file_contains_line(agents_path, EXECUTOR_MARKER):
            log(f"[dry-run] skip existing AGENTS executor contract in {agents_path}")
        else:
            log(f"[dry-run] append AGENTS executor contract to {agents_path}")

        if agents_path.is_file() and file_contains_line(agents_path, PLANNING_MARKER):
            log(f"[dry-run] skip existing AGENTS planning contract in {agents_path}")
        else:
            log(f"[dry-run] append AGENTS planning contract to {agents_path}")

        if agents_context_content:
            if agents_path.is_file() and file_contains_line(agents_path, PROJECT_CONTEXT_MARKER):
                log(f"[dry-run] skip existing AGENTS project context in {agents_path}")
            else:
                log(f"[dry-run] append AGENTS project context to {agents_path}")
        return

    if not agents_path.exists():
        agents_path.write_text("# AGENTS\n", encoding="utf-8")

    if not file_contains_line(agents_path, EXECUTOR_MARKER):
        append_text(agents_path, f"\n{executor_contract}\n")
        log(f"appended AGENTS executor contract to {agents_path}")
    else:
        log(f"AGENTS executor contract already present in {agents_path}")

    if not file_contains_line(agents_path, PLANNING_MARKER):
        append_text(agents_path, f"\n{planning_contract}\n")
        log(f"appended AGENTS planning contract to {agents_path}")
    else:
        log(f"AGENTS planning contract already present in {agents_path}")

    if agents_context_content:
        if not file_contains_line(agents_path, PROJECT_CONTEXT_MARKER):
            append_text(agents_path, f"\n{PROJECT_CONTEXT_MARKER}\n\n{agents_context_content}\n")
            log(f"appended AGENTS project context to {agents_path}")
        else:
            log(f"AGENTS project context already present in {agents_path}")


def should_append_packet_statuses(path: Path, *, overwrite: bool) -> bool:
    if overwrite:
        return True
    if not path.exists():
        return False
    if path.stat().st_size == 0:
        return True

    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return True
    return lines[-1] == "## Packet Status"


def packet_status_for_index(index: int, task_count: int) -> str:
    if task_count == 1:
        return "ready"
    if index < 2:
        return "ready"
    return "blocked"


def build_paths(repo_root: Path, wave: str) -> BootstrapPaths:
    backlog_dir = repo_root / "docs" / "backlog"
    packets_dir = repo_root / "docs" / "executor_packets" / wave
    scripts_dir = repo_root / "scripts"
    tmp_dir = repo_root / "tmp"
    orchestrator_target = scripts_dir / "live_orchestrator.py"
    return BootstrapPaths(
        repo_root=repo_root,
        backlog_dir=backlog_dir,
        packets_dir=packets_dir,
        scripts_dir=scripts_dir,
        tmp_dir=tmp_dir,
        orchestrator_target=orchestrator_target,
    )


def run(args: argparse.Namespace) -> int:
    repo_root = detect_repo_root(args.repo_root)
    validate_args(args, repo_root)

    source_path = (
        Path(args.orchestrator_source).expanduser().resolve()
        if args.orchestrator_source
        else None
    )
    paths = build_paths(repo_root, args.wave)

    mkdirp(paths.backlog_dir, dry_run=args.dry_run)
    mkdirp(paths.packets_dir, dry_run=args.dry_run)
    mkdirp(paths.scripts_dir, dry_run=args.dry_run)
    mkdirp(paths.tmp_dir, dry_run=args.dry_run)

    install_orchestrator(
        target_path=paths.orchestrator_target,
        source_path=source_path,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    task_ids: list[str] = []
    task_nums: list[int] = []
    backlog_rels: list[str] = []
    packet_rels: list[str] = []
    for index in range(args.task_count):
        number = args.start_id + index
        task_id = f"{args.id_prefix}{number}"
        task_ids.append(task_id)
        task_nums.append(number)
        backlog_rels.append(f"docs/backlog/{number}-task-{index + 1}.md")
        packet_rels.append(
            f"docs/executor_packets/{args.wave}/{task_id}_task_{index + 1}.md"
        )

    render_common = {
        "WAVE": args.wave,
    }

    write_file(
        paths.repo_root / "docs" / "refactor_plan.md",
        render_template("refactor_plan.md.tmpl", render_common),
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    write_file(
        paths.packets_dir / "ATOMIC_DECOMPOSITION_GUIDE.md",
        render_template("atomic_decomposition_guide.md.tmpl", render_common),
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    write_file(
        paths.packets_dir / "PLANNING_SESSION_PROMPT.md",
        render_template("planning_session_prompt.md.tmpl", render_common),
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    for index, task_id in enumerate(task_ids):
        backlog_rel = backlog_rels[index]
        packet_rel = packet_rels[index]
        dep_ids = task_dependencies(index, task_ids)
        dep_text = "None." if not dep_ids else ", ".join(dep_ids)

        backlog_path = paths.repo_root / backlog_rel
        packet_path = paths.repo_root / packet_rel

        write_file(
            backlog_path,
            render_template(
                "backlog_skeleton.md.tmpl",
                {
                    "TASK_NUM": str(task_nums[index]),
                    "TASK_INDEX": str(index + 1),
                    "TASK_ID": task_id,
                },
            ),
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )

        write_file(
            packet_path,
            render_template(
                "packet_skeleton.md.tmpl",
                {
                    "TASK_ID": task_id,
                    "TASK_INDEX": str(index + 1),
                    "BACKLOG_REL": backlog_rel,
                    "DEPENDENCIES_TEXT": dep_text,
                    "PACKET_REL": packet_rel,
                },
            ),
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )

    manifest_path = paths.packets_dir / "manifest.json"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tasks_payload: list[dict[str, object]] = []
    for index, task_id in enumerate(task_ids):
        tasks_payload.append(
            {
                "id": task_id,
                "backlog": backlog_rels[index],
                "packet": packet_rels[index],
                "depends_on": task_dependencies(index, task_ids),
                "can_run_in_parallel_with": task_parallel_hint(index, task_ids),
            }
        )
    manifest_payload = {
        "wave": args.wave,
        "generated_at": today,
        "tasks": tasks_payload,
    }
    write_file(
        manifest_path,
        json.dumps(manifest_payload, indent=2) + "\n",
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    write_file(
        paths.packets_dir / "README.md",
        render_template("wave_readme.md.tmpl", render_common),
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    orchestrator_state_path = paths.packets_dir / "orchestrator_state.md"
    write_file(
        orchestrator_state_path,
        render_template("orchestrator_state.md.tmpl", render_common),
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        log(f"[dry-run] append packet statuses to {orchestrator_state_path}")
    else:
        if should_append_packet_statuses(orchestrator_state_path, overwrite=args.overwrite):
            with orchestrator_state_path.open("a", encoding="utf-8") as fh:
                for index, task_id in enumerate(task_ids):
                    status = packet_status_for_index(index, args.task_count)
                    fh.write(f"- `{task_id}`: {status}\n")
                fh.write("\n## Notes\n\n")
                fh.write("- Add acceptance/rejection rationale per packet.\n")
                fh.write("- Record validation evidence and blocker details.\n")
            log(f"updated {orchestrator_state_path}")

    write_file(
        paths.scripts_dir / "orchestrator_gate.sh",
        load_template("orchestrator_gate.sh.tmpl"),
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        mode=0o755,
    )

    agents_context_content = load_agents_context(args)
    append_agents_contract(
        agents_path=paths.repo_root / "AGENTS.md",
        agents_context_content=agents_context_content,
        dry_run=args.dry_run,
    )

    log(f"bootstrap completed for wave '{args.wave}' with {args.task_count} task(s).")
    log("next steps:")
    log(
        "  1) Start planning from: "
        f"docs/executor_packets/{args.wave}/PLANNING_SESSION_PROMPT.md"
    )
    log("  2) Fill each packet's Allowed Files and Validation Commands.")
    log(f"  3) Refine depends_on in {paths.packets_dir / 'manifest.json' }.")
    log(
        "  4) Run: python3 -B scripts/live_orchestrator.py "
        f"--manifest docs/executor_packets/{args.wave}/manifest.json "
        "--dry-run --no-resume --max-parallel 3"
    )
    log(
        "  5) Inspect: python3 -B scripts/live_orchestrator.py "
        f"--report --manifest docs/executor_packets/{args.wave}/manifest.json"
    )
    log(
        "  6) For quota limits, add: --quota-cooldown-seconds 900 "
        "--quota-max-failures-per-task 3 [--quota-fail-fast]"
    )

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return run(args)
    except BootstrapError as exc:
        print(f"[bootstrap] error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
