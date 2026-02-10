# Local-First GPU Orchestration (Codex + Modal)

This repository uses a local-first control plane:

- Code generation and orchestration run locally via `codex exec` and `live_orchestrator.py`.
- CUDA/Warp validation commands run remotely on rented NVIDIA GPUs through Modal.
- Artifact persistence is S3-compatible storage plus local JSONL run telemetry.

Codex Cloud is intentionally out of scope for this workflow.

## Template Usage

This repo is intended to be injected into another project. Use:

```bash
scripts/inject_orchestration_template.sh \
  --target-repo /path/to/target/repo \
  --wave wave_1 \
  --task-count 3 \
  --agents-context-file /path/to/context.md
```

The injector bootstraps packet scaffolding and appends the orchestration contracts/context into the target repo `AGENTS.md`.
It also installs `scripts/bootstrap_orchestrator_wave.py` and `scripts/templates/bootstrap_orchestrator/`
into the target repo for future wave generation.

## Components

- Local scheduler: `live_orchestrator.py`
- Local bridge CLI: `scripts/gpu_exec.py`
- Remote runtime module: `scripts/gpu_modal_app.py`
- Runtime config: `config/gpu_backend.toml`
- Run telemetry: `tmp/live_orchestrator/gpu_runs.jsonl`
- Warm policy state: `tmp/live_orchestrator/gpu_policy_state.json`

## Prerequisites

1. Python 3.12+
2. Codex CLI authenticated locally.
3. Modal CLI authenticated (`modal setup`).
4. Modal Python package available in local environment.
5. For persistent artifacts: S3-compatible credentials and bucket.

## Config

Edit `config/gpu_backend.toml`.

- `[backend]`: primary backend selector (`modal`).
- `[modal]`: GPU/image/runtime defaults.
- `[policy]`: hybrid/hot promotion and demotion thresholds.
- `[artifacts]`: S3 bucket/prefix and local spool directory.
- `[timeouts]`: command and modal submit timeouts.

Important defaults:

- GPU: `L4`
- Hybrid scaledown window: `600s`
- Hot scaledown window: `1200s`
- Promote to hot mode when either:
  - 4 attempts in 15 minutes
  - median cold start latency over recent cold starts exceeds 45s
- Demote to hybrid after 30 minutes idle

## Packet Validation Command Pattern

Use this command style in packet `## Validation Commands`:

```bash
python3 scripts/gpu_exec.py \
  --backend modal \
  --task-id WB101 \
  --attempt ${GPU_ATTEMPT:-1} \
  --command "python3 -m pytest tests/test_gpu_smoke.py -q"
```

`gpu_exec.py` packages the current worktree snapshot, submits to Modal, collects status, emits local telemetry, and returns the remote command exit code.

## Artifacts

If `artifacts.s3_bucket` is set, each run uploads:

- `stdout.log`
- `stderr.log`
- `metadata.json`
- `workspace_after.tar.gz`

Artifact URI format:

```text
s3://<bucket>/<s3_prefix>/<task_id>/attempt-<nn>/<timestamp>/<run_id>/
```

If no bucket is configured, runtime reports `unpersisted://<run_id>`.

Note: when no S3 bucket is configured, workspace snapshots are sent inline; large worktrees can exceed inline payload limits. Configure `artifacts.s3_bucket` for larger snapshots.

## Operational Notes

- Keep `live_orchestrator.py` unchanged for scheduler/retry/escalation.
- Put remote execution only inside packet validation commands.
- Preserve classifier-friendly failures by ensuring remote stdout/stderr tails are surfaced.
- Use `--hot-mode on|off|auto` only when policy override is needed.

## Local NVIDIA Benchmarking

When running validation commands on a local NVIDIA workstation, you can keep worker throughput high
while serializing local GPU validation to avoid benchmark contention:

```bash
python3 -B scripts/live_orchestrator.py \
  --manifest docs/executor_packets/<wave>/manifest.json \
  --max-parallel 4 \
  --validation-executor orchestrator
```

How this works:

- `--max-parallel N` keeps multiple code-generation workers active.
- `--validation-executor orchestrator` tells workers not to run validation commands directly.
- The orchestrator runs packet validation commands after worker completion, one task at a time.

## Troubleshooting

- `unsupported backend`: pass `--backend modal`.
- `modal submission failed`: verify `modal setup`, network, and entrypoint path.
- `workspace packaging found no files`: ensure worktree has tracked/untracked files.
- `unpersisted://` artifacts: set `artifacts.s3_bucket` in config and provide credentials.
