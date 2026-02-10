#!/usr/bin/env python3
"""Compatibility entrypoint for the modular live orchestrator."""

from __future__ import annotations

import importlib
import sys


def _import_with_fallback(primary: str, fallback: str):
    try:
        return importlib.import_module(primary)
    except ModuleNotFoundError:
        # Fallback for copied target repositories where this file lives under scripts/
        # and the package is available as scripts/orchestrator/.
        return importlib.import_module(fallback)


_cli = _import_with_fallback("scripts.orchestrator.cli", "orchestrator.cli")
_manifest = _import_with_fallback("scripts.orchestrator.manifest", "orchestrator.manifest")
_models = _import_with_fallback("scripts.orchestrator.models", "orchestrator.models")
_scheduler = _import_with_fallback("scripts.orchestrator.scheduler", "orchestrator.scheduler")


main = _cli.main
parse_args = _cli.parse_args
validate_args = _cli.validate_args

load_manifest = _manifest.load_manifest
parse_allowed_files = _manifest.parse_allowed_files
parse_validation_commands = _manifest.parse_validation_commands
task_branch_name = _manifest.task_branch_name

ModelProfile = _models.ModelProfile
TaskSpec = _models.TaskSpec
TaskRuntime = _models.TaskRuntime
TaskState = _models.TaskState
RuntimeDirs = _models.RuntimeDirs
QuotaRuntime = _models.QuotaRuntime

classify_failure = _scheduler.classify_failure
default_worker_template = _scheduler.default_worker_template
detect_quota_or_rate_limit = _scheduler.detect_quota_or_rate_limit
filter_profiles_by_model_probe = _scheduler.filter_profiles_by_model_probe
format_template = _scheduler.format_template
parse_profiles = _scheduler.parse_profiles
run_validation_commands = _scheduler.run_validation_commands
within_allowed_files = _scheduler.within_allowed_files


__all__ = [
    "ModelProfile",
    "TaskSpec",
    "TaskRuntime",
    "TaskState",
    "RuntimeDirs",
    "QuotaRuntime",
    "classify_failure",
    "default_worker_template",
    "detect_quota_or_rate_limit",
    "filter_profiles_by_model_probe",
    "format_template",
    "load_manifest",
    "main",
    "parse_allowed_files",
    "parse_args",
    "parse_profiles",
    "parse_validation_commands",
    "run_validation_commands",
    "task_branch_name",
    "validate_args",
    "within_allowed_files",
]


if __name__ == "__main__":
    sys.exit(main())
