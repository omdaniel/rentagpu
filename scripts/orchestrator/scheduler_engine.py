from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .manifest import load_manifest
from .models import QuotaRuntime
from .scheduler_args import parse_args, parse_profiles, resolve_state_path, validate_args
from .scheduler_policy import (
    all_done,
    mark_task_blocked,
    propagate_dependency_blocks,
    retry_or_block_task,
    task_ready,
)
from .scheduler_probe import filter_profiles_by_model_probe
from .scheduler_report import render_state_report
from .scheduler_runtime import default_worker_template, ensure_dirs, git_root, terminate_process
from .scheduler_state import EventSink, restore_runtime_state, safe_error_text, ts_iso, write_state
from .scheduler_worker import handle_finished_worker, launch_task


def main() -> int:
    args = parse_args()

    if args.report:
        try:
            repo_for_state: Optional[Path]
            if args.state_file:
                repo_for_state = None
            else:
                try:
                    repo_for_state = git_root(Path(args.repo_root).resolve())
                except RuntimeError:
                    repo_for_state = Path(args.repo_root).resolve()
            state_path = resolve_state_path(
                repo_root=repo_for_state,
                repo_root_arg=args.repo_root,
                runtime_dir_arg=args.runtime_dir,
                state_file_arg=args.state_file,
            )
            print(render_state_report(state_path))
            return 0 if state_path.exists() else 1
        except (ValueError, FileNotFoundError, json.JSONDecodeError, RuntimeError, OSError) as exc:
            print(f"report error: {safe_error_text(exc)}", file=sys.stderr)
            return 2

    try:
        validate_args(args)
        repo_root = git_root(Path(args.repo_root).resolve())
        manifest_path = (repo_root / args.manifest).resolve()
        tasks = load_manifest(
            repo_root,
            manifest_path,
            allow_empty_allowed_files=args.allow_empty_allowed_files,
        )
        profiles = parse_profiles(args.executor_profiles)
        dirs = ensure_dirs(repo_root, args.runtime_dir, args.worktree_root)
        if args.state_file:
            dirs.state_file = Path(args.state_file).expanduser().resolve()
            dirs.state_file.parent.mkdir(parents=True, exist_ok=True)
            dirs.events_file = dirs.state_file.parent / "events.jsonl"
    except (ValueError, FileNotFoundError, json.JSONDecodeError, RuntimeError, OSError) as exc:
        print(f"startup error: {safe_error_text(exc)}", file=sys.stderr)
        return 2
    events = EventSink(dirs.events_file)

    quota_runtime = QuotaRuntime()
    quota_wait_announced_until = 0.0

    if args.probe_models:
        try:
            profiles = filter_profiles_by_model_probe(
                repo_root=repo_root,
                profiles=profiles,
                timeout_seconds=args.probe_model_timeout_seconds,
                events=events,
            )
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"model probe error: {safe_error_text(exc)}", file=sys.stderr)
            return 2

    if not args.no_resume:
        try:
            restore_runtime_state(
                state_path=dirs.state_file,
                tasks=tasks,
                profiles=profiles,
                quota_runtime=quota_runtime,
                events=events,
            )
        except (ValueError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            print(f"resume error: {safe_error_text(exc)}", file=sys.stderr)
            return 2
    else:
        events.emit("resume_skip", "--no-resume set; starting with fresh runtime state")
    if quota_runtime.cooldown_until > time.time():
        quota_wait_announced_until = quota_runtime.cooldown_until
        events.emit(
            "quota_wait",
            (
                "resumed with active quota cooldown; new launches paused until "
                f"{ts_iso(quota_runtime.cooldown_until)}."
            ),
            cooldown_until=ts_iso(quota_runtime.cooldown_until),
        )

    worker_template = args.worker_command_template or default_worker_template()
    if not args.dry_run and not worker_template:
        print(
            "Missing worker command template. Provide --worker-command-template "
            "or set CODEX_WORKER_COMMAND_TEMPLATE.",
            file=sys.stderr,
        )
        print(
            "Example template:\n"
            "  cat {prompt_file_q} | codex exec -m {model_q} "
            "-c model_reasoning_effort={reasoning_q} "
            "--cd {worktree_q} --skip-git-repo-check -",
            file=sys.stderr,
        )
        return 2
    worker_template = worker_template or ""

    running: dict[str, subprocess.Popen[str]] = {}
    events.emit(
        "start",
        (
            f"Live orchestrator started with {len(tasks)} tasks, "
            f"max_parallel={args.max_parallel}, dry_run={args.dry_run}."
        ),
        manifest=str(manifest_path),
    )

    try:
        while True:
            finished_ids: list[str] = []
            for task_id, proc in list(running.items()):
                task = tasks[task_id]
                runtime = task.runtime
                if (
                    args.worker_timeout_seconds > 0
                    and runtime.started_at is not None
                    and (time.time() - runtime.started_at) > args.worker_timeout_seconds
                ):
                    terminate_process(proc)
                    finished_ids.append(task_id)
                    retry_or_block_task(
                        tasks=tasks,
                        task=task,
                        kind="infra",
                        error_summary=(
                            f"worker timed out after {args.worker_timeout_seconds}s"
                        ),
                        profiles=profiles,
                        quota_runtime=quota_runtime,
                        quota_cooldown_seconds=args.quota_cooldown_seconds,
                        quota_max_failures_per_task=args.quota_max_failures_per_task,
                        quota_fail_fast=args.quota_fail_fast,
                        max_attempts=args.max_attempts,
                        escalate_after_compile=args.escalate_after_compile,
                        escalate_after_runtime=args.escalate_after_runtime,
                        events=events,
                    )
                    continue

                returncode = proc.poll()
                if returncode is None:
                    continue
                finished_ids.append(task_id)
                try:
                    handle_finished_worker(
                        tasks=tasks,
                        task=task,
                        returncode=returncode,
                        profiles=profiles,
                        quota_runtime=quota_runtime,
                        quota_cooldown_seconds=args.quota_cooldown_seconds,
                        quota_max_failures_per_task=args.quota_max_failures_per_task,
                        quota_fail_fast=args.quota_fail_fast,
                        max_attempts=args.max_attempts,
                        escalate_after_compile=args.escalate_after_compile,
                        escalate_after_runtime=args.escalate_after_runtime,
                        validation_timeout_seconds=args.command_timeout_seconds,
                        events=events,
                    )
                except Exception as exc:  # defensive: do not crash the orchestrator loop
                    retry_or_block_task(
                        tasks=tasks,
                        task=task,
                        kind="infra",
                        error_summary=(
                            "orchestrator post-processing failure: "
                            + safe_error_text(exc)
                        ),
                        profiles=profiles,
                        quota_runtime=quota_runtime,
                        quota_cooldown_seconds=args.quota_cooldown_seconds,
                        quota_max_failures_per_task=args.quota_max_failures_per_task,
                        quota_fail_fast=args.quota_fail_fast,
                        max_attempts=args.max_attempts,
                        escalate_after_compile=args.escalate_after_compile,
                        escalate_after_runtime=args.escalate_after_runtime,
                        events=events,
                    )
            for task_id in finished_ids:
                running.pop(task_id, None)

            propagate_dependency_blocks(tasks, events)

            now_ts = time.time()
            if (
                quota_runtime.cooldown_until > 0
                and now_ts >= quota_runtime.cooldown_until
            ):
                events.emit(
                    "quota_resume",
                    "quota cooldown expired; scheduling resumes.",
                )
                quota_runtime.cooldown_until = 0.0
                quota_wait_announced_until = 0.0

            capacity = max(args.max_parallel - len(running), 0)
            if quota_runtime.cooldown_until > now_ts:
                capacity = 0
                if quota_wait_announced_until != quota_runtime.cooldown_until:
                    events.emit(
                        "quota_wait",
                        (
                            "quota cooldown active; suppressing new launches until "
                            f"{ts_iso(quota_runtime.cooldown_until)}."
                        ),
                        cooldown_until=ts_iso(quota_runtime.cooldown_until),
                        remaining_seconds=int(quota_runtime.cooldown_until - now_ts),
                    )
                    quota_wait_announced_until = quota_runtime.cooldown_until

            if capacity > 0:
                ready = [t for t in tasks.values() if task_ready(t, tasks)]
                ready.sort(key=lambda t: t.spec.task_id)
                for task in ready[:capacity]:
                    try:
                        launched_proc = launch_task(
                            repo_root=repo_root,
                            dirs=dirs,
                            task=task,
                            profiles=profiles,
                            worker_template=worker_template,
                            base_ref=args.base_ref,
                            dry_run=args.dry_run,
                            events=events,
                        )
                        if launched_proc is not None:
                            running[task.spec.task_id] = launched_proc
                    except Exception as exc:  # defensive: launch failures become task failures
                        retry_or_block_task(
                            tasks=tasks,
                            task=task,
                            kind="infra",
                            error_summary="worker launch failure: " + safe_error_text(exc),
                            profiles=profiles,
                            quota_runtime=quota_runtime,
                            quota_cooldown_seconds=args.quota_cooldown_seconds,
                            quota_max_failures_per_task=args.quota_max_failures_per_task,
                            quota_fail_fast=args.quota_fail_fast,
                            max_attempts=args.max_attempts,
                            escalate_after_compile=args.escalate_after_compile,
                            escalate_after_runtime=args.escalate_after_runtime,
                            events=events,
                        )

            write_state(
                path=dirs.state_file,
                tasks=tasks,
                profiles=profiles,
                quota_runtime=quota_runtime,
                running=running,
            )

            if all_done(tasks) and not running:
                break

            if not running:
                has_ready = any(task_ready(t, tasks) for t in tasks.values())
                if not has_ready:
                    # Remaining tasks are blocked by dependency state.
                    propagate_dependency_blocks(tasks, events)
                    if all_done(tasks):
                        break

            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        events.emit("interrupt", "KeyboardInterrupt received. Shutting down workers.")
        for task_id, proc in list(running.items()):
            terminate_process(proc)
            mark_task_blocked(
                tasks[task_id],
                "orchestrator interrupted by operator",
                events,
            )
            running.pop(task_id, None)
        write_state(
            path=dirs.state_file,
            tasks=tasks,
            profiles=profiles,
            quota_runtime=quota_runtime,
            running=running,
        )
        return 130

    completed = sum(1 for t in tasks.values() if t.runtime.status == "completed")
    blocked = sum(1 for t in tasks.values() if t.runtime.status == "blocked")
    events.emit(
        "finish",
        f"Orchestration finished: completed={completed}, blocked={blocked}.",
        completed=completed,
        blocked=blocked,
        state_file=str(dirs.state_file),
    )
    return 0 if blocked == 0 else 1
