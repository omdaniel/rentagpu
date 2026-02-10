# rentagpu (orchestration template)

This repository is a reusable orchestration template, not an implementation repo for CUDA/Warp kernels.

Use it to inject a planning/execution control layer into another project that has GPU-heavy simulation work.

## Template Injection

Run from this template repository:

```bash
scripts/inject_orchestration_template.sh \
  --target-repo /path/to/target/repo \
  --wave wave_1 \
  --id-prefix W \
  --start-id 101 \
  --task-count 5 \
  --agents-context-file /path/to/context.md
```

What this does in the target repo:

- creates/updates packet scaffolding under `docs/executor_packets/<wave>/`
- installs `scripts/live_orchestrator.py`
- appends orchestration contracts to `AGENTS.md` (or creates `AGENTS.md`)
- appends project-specific context to `AGENTS.md` when provided
- generates a planning prompt scaffold at
  `docs/executor_packets/<wave>/PLANNING_SESSION_PROMPT.md`

## Key Files in This Template

- `bootstrap_orchestrator_wave.sh`: base packet/manifest bootstrapper.
- `scripts/inject_orchestration_template.sh`: one-command injector for target repos.
- `live_orchestrator.py`: local orchestration loop with dependency-aware scheduling.
- `scripts/gpu_exec.py`: optional remote GPU validation bridge.
- `scripts/gpu_modal_app.py`: optional Modal runtime handler.
