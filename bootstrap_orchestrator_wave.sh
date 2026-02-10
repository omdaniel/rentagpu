#!/usr/bin/env bash
set -euo pipefail

# Bootstraps packet + manifest scaffolding for live_orchestrator.py.
# This script is intended to run before refactor planning starts, so the
# repository has a concrete contract for atomic async task decomposition.

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap_orchestrator_wave.sh [options]

Options:
  --repo-root PATH        Repository root. Defaults to git top-level or cwd.
  --wave NAME             Wave directory name under docs/executor_packets/.
                          Default: wave_1
  --id-prefix PREFIX      Task ID prefix. Default: W
  --start-id N            First numeric task id. Default: 101
  --task-count N          Number of task skeletons to generate. Default: 3
  --orchestrator-source   Optional path to source live_orchestrator.py to copy.
                          If omitted, uses embedded payload in this script.
  --agents-context-file   Optional markdown/text file appended to AGENTS.md
                          under project orchestration context.
  --agents-context-text   Optional one-line context text appended to AGENTS.md.
  --overwrite             Overwrite existing generated files.
  --dry-run               Print intended actions without writing files.
  -h, --help              Show this help message.

Example:
  scripts/bootstrap_orchestrator_wave.sh \
    --wave wave_1 --id-prefix W --start-id 101 --task-count 3
EOF
}

log() {
  printf '[bootstrap] %s\n' "$*"
}

die() {
  printf '[bootstrap] error: %s\n' "$*" >&2
  exit 2
}

require_arg_value() {
  local flag="$1"
  local value="${2:-}"
  if [[ -z "$value" || "$value" == -* ]]; then
    die "missing value for $flag"
  fi
}

require_int() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name must be an integer (got '$value')."
}

file_contains_line() {
  local line="$1"
  local path="$2"
  if command -v rg >/dev/null 2>&1; then
    rg -q "^${line}\$" "$path"
  else
    grep -q "^${line}\$" "$path"
  fi
}

mkdirp() {
  local dir="$1"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "[dry-run] mkdir -p $dir"
    return 0
  fi
  mkdir -p "$dir"
}

write_file() {
  local path="$1"
  local mode="${2:-0644}"
  local should_write=1
  if [[ -f "$path" && "$OVERWRITE" -eq 0 ]]; then
    should_write=0
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ "$should_write" -eq 1 ]]; then
      log "[dry-run] write $path"
    else
      log "[dry-run] skip existing $path"
    fi
    cat >/dev/null
    return 0
  fi

  if [[ "$should_write" -eq 0 ]]; then
    log "skip existing $path (use --overwrite to replace)"
    cat >/dev/null
    return 0
  fi

  mkdir -p "$(dirname "$path")"
  cat >"$path"
  chmod "$mode" "$path"
  log "wrote $path"
}

append_agents_contract() {
  local agents_path="$1"
  local executor_marker='## Orchestrator/Executor Contract'
  local planning_marker='## Orchestrator Planning Contract'
  local context_marker='## Project Orchestration Context'

  if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ ! -f "$agents_path" ]]; then
      log "[dry-run] create $agents_path"
    fi
    if [[ -f "$agents_path" ]] && file_contains_line "$executor_marker" "$agents_path"; then
      log "[dry-run] skip existing AGENTS executor contract in $agents_path"
    else
      log "[dry-run] append AGENTS executor contract to $agents_path"
    fi
    if [[ -f "$agents_path" ]] && file_contains_line "$planning_marker" "$agents_path"; then
      log "[dry-run] skip existing AGENTS planning contract in $agents_path"
    else
      log "[dry-run] append AGENTS planning contract to $agents_path"
    fi
    if [[ -n "${AGENTS_CONTEXT_CONTENT:-}" ]]; then
      if [[ -f "$agents_path" ]] && file_contains_line "$context_marker" "$agents_path"; then
        log "[dry-run] skip existing AGENTS project context in $agents_path"
      else
        log "[dry-run] append AGENTS project context to $agents_path"
      fi
    fi
    return 0
  fi

  if [[ ! -f "$agents_path" ]]; then
    cat >"$agents_path" <<'EOF'
# AGENTS
EOF
  fi

  if ! file_contains_line "$executor_marker" "$agents_path"; then
    cat >>"$agents_path" <<'EOF'

## Orchestrator/Executor Contract

When executing packetized refactor tasks:

- Read the assigned packet file under `docs/executor_packets/<wave>/`.
- Edit only files listed under that packet's `## Allowed Files`.
- Run all commands listed under that packet's `## Validation Commands`.
- If task cannot be completed within allowed files, return `blocked` and explain exact blocker.
- Do not commit generated/transient artifacts (`tmp/`, build caches, etc).
- Return status in this format:

```text
[TASK] <ID>
[STATE] completed|blocked
[FILES] <comma-separated edited files>
[VALIDATION] ran: <commands>
[EVIDENCE] <key output lines and skipped-step reasons>
[BLOCKERS] none|<details>
```
EOF
    log "appended AGENTS executor contract to $agents_path"
  else
    log "AGENTS executor contract already present in $agents_path"
  fi

  if ! file_contains_line "$planning_marker" "$agents_path"; then
    cat >>"$agents_path" <<'EOF'

## Orchestrator Planning Contract

When planning a new wave:

- Build packets under `docs/executor_packets/<wave>/` with one objective per packet.
- Encode hard ordering in manifest `depends_on` and use `can_run_in_parallel_with` only as advisory.
- Keep packet `Allowed Files` disjoint for tasks that should run in parallel.
- Include packet-local deterministic `Validation Commands`.
- Mark packets `blocked` when dependencies are blocked or scope must expand.
- Update `orchestrator_state.md` with packet status and blocker reasons.
EOF
    log "appended AGENTS planning contract to $agents_path"
  else
    log "AGENTS planning contract already present in $agents_path"
  fi

  if [[ -n "${AGENTS_CONTEXT_CONTENT:-}" ]]; then
    if ! file_contains_line "$context_marker" "$agents_path"; then
      {
        printf '\n%s\n\n' "$context_marker"
        printf '%s\n' "$AGENTS_CONTEXT_CONTENT"
      } >>"$agents_path"
      log "appended AGENTS project context to $agents_path"
    else
      log "AGENTS project context already present in $agents_path"
    fi
  fi
}

base64_decode_stream() {
  if printf 'QQ==\n' | base64 -D >/dev/null 2>&1; then
    base64 -D
  elif printf 'QQ==\n' | base64 -d >/dev/null 2>&1; then
    base64 -d
  elif printf 'QQ==\n' | base64 --decode >/dev/null 2>&1; then
    base64 --decode
  else
    die "unable to find a usable base64 decoder (tried -D, -d, --decode)."
  fi
}

write_embedded_orchestrator() {
  local target_path="$1"
  local tmp_path
  local has_payload=0

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "[dry-run] write embedded live_orchestrator.py -> $target_path"
    return 0
  fi

  tmp_path="$(mktemp "${TMPDIR:-/tmp}/live_orchestrator.XXXXXX")"
  if awk '
    $0=="__LIVE_ORCHESTRATOR_PAYLOAD_BEGIN__" { capture=1; next }
    $0=="__LIVE_ORCHESTRATOR_PAYLOAD_END__" { capture=0; exit }
    capture { print }
  ' "$SCRIPT_PATH" | base64_decode_stream | gzip -dc >"$tmp_path"; then
    has_payload=1
  fi

  if [[ "$has_payload" -ne 1 || ! -s "$tmp_path" ]]; then
    rm -f "$tmp_path"
    die "embedded live_orchestrator.py payload decode failed."
  fi

  mkdir -p "$(dirname "$target_path")"
  chmod 0755 "$tmp_path"
  mv "$tmp_path" "$target_path"
  log "installed $target_path from embedded payload"
}

install_orchestrator() {
  local target_path="$1"
  local source_path="$ORCHESTRATOR_SOURCE"

  if [[ -f "$target_path" && "$OVERWRITE" -eq 0 ]]; then
    log "skip existing $target_path (use --overwrite to replace)"
    return 0
  fi

  if [[ -n "$source_path" ]]; then
    [[ -f "$source_path" ]] || die "cannot install scripts/live_orchestrator.py; source not found at '$source_path'."
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "[dry-run] copy $source_path -> $target_path"
      return 0
    fi
    mkdir -p "$(dirname "$target_path")"
    cp "$source_path" "$target_path"
    chmod 0755 "$target_path"
    log "installed $target_path from $source_path"
    return 0
  fi

  write_embedded_orchestrator "$target_path"
}

json_array_from_list() {
  local out="["
  local first=1
  local item
  for item in "$@"; do
    if [[ "$first" -eq 0 ]]; then
      out+=", "
    fi
    first=0
    out+="\"$item\""
  done
  out+="]"
  printf '%s' "$out"
}

build_task_dependencies() {
  local idx="$1"

  if (( TASK_COUNT <= 2 )); then
    json_array_from_list
    return 0
  fi

  if (( idx < 2 )); then
    json_array_from_list
  elif (( idx == 2 )); then
    json_array_from_list "${TASK_IDS[0]}" "${TASK_IDS[1]}"
  else
    json_array_from_list "${TASK_IDS[idx-1]}"
  fi
}

build_parallel_hint() {
  local idx="$1"
  if (( TASK_COUNT < 2 )); then
    json_array_from_list
    return 0
  fi

  if (( idx == 0 )); then
    json_array_from_list "${TASK_IDS[1]}"
  elif (( idx == 1 )); then
    json_array_from_list "${TASK_IDS[0]}"
  else
    json_array_from_list
  fi
}

REPO_ROOT=""
WAVE="wave_1"
ID_PREFIX="W"
START_ID=101
TASK_COUNT=3
ORCHESTRATOR_SOURCE=""
AGENTS_CONTEXT_FILE=""
AGENTS_CONTEXT_TEXT=""
AGENTS_CONTEXT_CONTENT=""
OVERWRITE=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      require_arg_value "$1" "${2:-}"
      REPO_ROOT="${2:-}"
      shift 2
      ;;
    --wave)
      require_arg_value "$1" "${2:-}"
      WAVE="${2:-}"
      shift 2
      ;;
    --id-prefix)
      require_arg_value "$1" "${2:-}"
      ID_PREFIX="${2:-}"
      shift 2
      ;;
    --start-id)
      require_arg_value "$1" "${2:-}"
      START_ID="${2:-}"
      shift 2
      ;;
    --task-count)
      require_arg_value "$1" "${2:-}"
      TASK_COUNT="${2:-}"
      shift 2
      ;;
    --orchestrator-source)
      require_arg_value "$1" "${2:-}"
      ORCHESTRATOR_SOURCE="${2:-}"
      shift 2
      ;;
    --agents-context-file)
      require_arg_value "$1" "${2:-}"
      AGENTS_CONTEXT_FILE="${2:-}"
      shift 2
      ;;
    --agents-context-text)
      require_arg_value "$1" "${2:-}"
      AGENTS_CONTEXT_TEXT="${2:-}"
      shift 2
      ;;
    --overwrite)
      OVERWRITE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

if [[ -z "$REPO_ROOT" ]]; then
  if REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    :
  else
    REPO_ROOT="$(pwd)"
  fi
fi

require_int "--start-id" "$START_ID"
require_int "--task-count" "$TASK_COUNT"
(( TASK_COUNT >= 1 )) || die "--task-count must be >= 1."
[[ "$WAVE" =~ ^[A-Za-z0-9._-]+$ ]] || die "--wave contains invalid characters."
[[ "$ID_PREFIX" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]] || die "--id-prefix must start with a letter."

[[ -d "$REPO_ROOT" ]] || die "--repo-root does not exist: $REPO_ROOT"
REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"

if [[ -n "$AGENTS_CONTEXT_FILE" ]]; then
  [[ -f "$AGENTS_CONTEXT_FILE" ]] || die "--agents-context-file not found: $AGENTS_CONTEXT_FILE"
  AGENTS_CONTEXT_CONTENT="$(cat "$AGENTS_CONTEXT_FILE")"
fi
if [[ -n "$AGENTS_CONTEXT_TEXT" ]]; then
  if [[ -n "$AGENTS_CONTEXT_CONTENT" ]]; then
    AGENTS_CONTEXT_CONTENT="${AGENTS_CONTEXT_CONTENT}"$'\n'"$AGENTS_CONTEXT_TEXT"
  else
    AGENTS_CONTEXT_CONTENT="$AGENTS_CONTEXT_TEXT"
  fi
fi

BACKLOG_DIR="$REPO_ROOT/docs/backlog"
PACKETS_DIR="$REPO_ROOT/docs/executor_packets/$WAVE"
SCRIPTS_DIR="$REPO_ROOT/scripts"
TMP_DIR="$REPO_ROOT/tmp"

mkdirp "$BACKLOG_DIR"
mkdirp "$PACKETS_DIR"
mkdirp "$SCRIPTS_DIR"
mkdirp "$TMP_DIR"

install_orchestrator "$REPO_ROOT/scripts/live_orchestrator.py"

declare -a TASK_IDS TASK_NUMS BACKLOG_RELS PACKET_RELS
for ((i=0; i<TASK_COUNT; i++)); do
  num=$((START_ID + i))
  id="${ID_PREFIX}${num}"
  TASK_IDS+=("$id")
  TASK_NUMS+=("$num")
  BACKLOG_RELS+=("docs/backlog/${num}-task-$((i+1)).md")
  PACKET_RELS+=("docs/executor_packets/${WAVE}/${id}_task_$((i+1)).md")
done

today="$(date -u +%Y-%m-%d)"

# refactor plan template
write_file "$REPO_ROOT/docs/refactor_plan.md" <<EOF
# Refactor Plan ($WAVE)

This document defines the planning scaffold for packetized refactors executed by \`scripts/live_orchestrator.py\`.

## Goals

- Break large refactors into dependency-safe packets.
- Enable parallel execution for independent tasks.
- Enforce strict file-scope and validation gates.

## Non-Goals

- Unscoped architecture redesign.
- Opportunistic cleanup unrelated to packet objectives.
- CI/release redesign unless explicitly packetized.

## Wave Boundaries

- Current wave: \`$WAVE\`
- Add future waves under \`docs/executor_packets/<wave>/\` with separate manifests.

## Orchestration Contract

- Machine-readable queue: \`docs/executor_packets/$WAVE/manifest.json\`
- Packet specs: one markdown packet per task in \`docs/executor_packets/$WAVE/\`
- Human ledger: \`docs/executor_packets/$WAVE/orchestrator_state.md\`
- Integration gate: \`scripts/orchestrator_gate.sh\`
EOF

# atomic decomposition guide
write_file "$PACKETS_DIR/ATOMIC_DECOMPOSITION_GUIDE.md" <<EOF
# Atomic Task Decomposition Guide ($WAVE)

Use this rubric before editing packet files and manifest dependencies.

## Atomic Packet Rules

- One packet should target one coherent objective.
- Prefer 1-4 owned files per packet.
- Avoid overlapping \`Allowed Files\` across packets that may run in parallel.
- Keep packet validation commands packet-local and deterministic.

## Parallelism Rules

- Packets with disjoint \`Allowed Files\` and no semantic dependency can run in parallel.
- Encode ordering in \`depends_on\`.
- \`can_run_in_parallel_with\` is advisory; \`depends_on\` is authoritative.

## Dependency Rules

- A packet should depend only on tasks whose outputs it directly requires.
- Keep dependency chains shallow where possible.
- Use a final integration packet to collect cross-cutting updates.

## Packet Completion Rules

- Executor must return \`completed\` or \`blocked\`.
- Block when scope must expand beyond \`Allowed Files\`.
- Include exact failing validation command output on block.
EOF

# planning prompt scaffold
write_file "$PACKETS_DIR/PLANNING_SESSION_PROMPT.md" <<EOF
# Planning Session Prompt Template ($WAVE)

Use this in a fresh planning session so decomposition aligns with the orchestrator.

## Required Inputs

- \`docs/refactor_plan.md\`
- \`docs/executor_packets/$WAVE/ATOMIC_DECOMPOSITION_GUIDE.md\`
- Existing architecture notes / backlog docs in this repository.

## Planner Instructions

1. Decompose work into packet tasks that can be executed asynchronously where safe.
2. For each packet, define:
   - objective and success criteria
   - exact \`Allowed Files\` (prefer disjoint paths for parallel packets)
   - deterministic \`Validation Commands\`
3. Explicitly identify:
   - packets that are ready immediately
   - packets blocked by dependencies
4. Encode ordering in \`docs/executor_packets/$WAVE/manifest.json\`:
   - use \`depends_on\` for hard dependencies
   - use \`can_run_in_parallel_with\` only as advisory hints
5. Produce blocker notes for any packet that cannot be scoped without expanding allowed files.

## Required Planning Output

- Updated packet markdown files under \`docs/executor_packets/$WAVE/\`
- Updated \`docs/executor_packets/$WAVE/manifest.json\`
- Updated \`docs/executor_packets/$WAVE/orchestrator_state.md\` status table
EOF

# backlog skeletons
for ((i=0; i<TASK_COUNT; i++)); do
  id="${TASK_IDS[$i]}"
  num="${TASK_NUMS[$i]}"
  backlog_path="$REPO_ROOT/${BACKLOG_RELS[$i]}"
  write_file "$backlog_path" <<EOF
# ${num} - Task $((i+1))

## Summary

Describe objective for packet $id.

## Success Criteria

- Define observable behavior outcomes.
- Define required validation signals.

## Out of Scope

- List explicit exclusions for this packet.
EOF
done

# packet skeletons
for ((i=0; i<TASK_COUNT; i++)); do
  id="${TASK_IDS[$i]}"
  packet_path="$REPO_ROOT/${PACKET_RELS[$i]}"
  backlog_rel="${BACKLOG_RELS[$i]}"
  dep_json="$(build_task_dependencies "$i")"
  dep_text="None."
  if [[ "$dep_json" != "[]" ]]; then
    dep_text="$(printf '%s' "$dep_json" | tr -d '[]\"')"
  fi

  write_file "$packet_path" <<EOF
# Packet $id: Task $((i+1))

Backlog source: \`$backlog_rel\`

## Objective

Describe what this packet changes and why.

## Dependencies

- $dep_text

## Allowed Files

- \`<replace-with-allowed-file-1>\`
- \`<replace-with-allowed-file-2>\`

## Forbidden Files

- \`infra/\`
- \`.github/\`

## Required Outcomes

- Define expected behavior/results.

## Implementation Checklist

1. Step 1.
2. Step 2.

## Validation Commands

\`\`\`bash
python3 scripts/gpu_exec.py \\
  --backend modal \\
  --task-id $id \\
  --attempt \${GPU_ATTEMPT:-1} \\
  --command \"echo 'TODO: replace remote GPU validation command for $id'\"
\`\`\`

## Required Evidence in Handoff

- Key output lines for validation commands.
- Any skipped steps and reasons.

## Definition of Done

- Validation commands pass.
- Scope gate passes (allowed files only).

## Fast Executor Prompt (Copy/Paste)

\`\`\`text
Execute packet $id from ${PACKET_RELS[$i]}.
Respect allowed files exactly.
Return status in the required format with validation evidence.
\`\`\`
EOF
done

# manifest
manifest_path="$PACKETS_DIR/manifest.json"
if [[ "$DRY_RUN" -eq 1 ]]; then
  if [[ -f "$manifest_path" && "$OVERWRITE" -eq 0 ]]; then
    log "[dry-run] skip existing $manifest_path"
  else
    log "[dry-run] write $manifest_path"
  fi
else
  if [[ -f "$manifest_path" && "$OVERWRITE" -eq 0 ]]; then
    log "skip existing $manifest_path (use --overwrite to replace)"
  else
    {
      echo "{"
      echo "  \"wave\": \"${WAVE}\","
      echo "  \"generated_at\": \"${today}\","
      echo "  \"tasks\": ["
      for ((i=0; i<TASK_COUNT; i++)); do
        id="${TASK_IDS[$i]}"
        backlog_rel="${BACKLOG_RELS[$i]}"
        packet_rel="${PACKET_RELS[$i]}"
        deps="$(build_task_dependencies "$i")"
        parallel="$(build_parallel_hint "$i")"
        comma=","
        if (( i == TASK_COUNT - 1 )); then
          comma=""
        fi
        cat <<EOF
    {
      "id": "${id}",
      "backlog": "${backlog_rel}",
      "packet": "${packet_rel}",
      "depends_on": ${deps},
      "can_run_in_parallel_with": ${parallel}
    }${comma}
EOF
      done
      echo "  ]"
      echo "}"
    } >"$manifest_path"
    log "wrote $manifest_path"
  fi
fi

# wave README
write_file "$PACKETS_DIR/README.md" <<EOF
# ${WAVE} Executor Packets (Template)

This folder contains packetized tasks for orchestrated refactor execution.

## Execution Contract

- Executor edits are limited to packet \`Allowed Files\`.
- If required changes exceed packet scope, return \`blocked\` with precise blocker details.
- Do not add generated/transient artifacts (\`tmp/\`, build caches, etc.) to tracked files.
- Every packet return must include command-level validation evidence.

## Recommended Dispatch Order

1. Run ready packets with no dependencies in parallel.
2. Promote dependent packets once prerequisites are completed.
3. Run integration gate after all packets complete.

## Required Executor Return Format

\`\`\`text
[TASK] <ID>
[STATE] completed|blocked
[FILES] ...
[VALIDATION] ran: ...
[EVIDENCE] key output lines + skipped step reason
[BLOCKERS] none|...
\`\`\`

## Integration Gate

Run from repository root:

\`\`\`bash
bash scripts/orchestrator_gate.sh
\`\`\`

## Quota-Aware Orchestration

If Codex API usage limits are hit, use quota controls:

\`\`\`bash
python3 -B scripts/live_orchestrator.py \\
  --manifest docs/executor_packets/$WAVE/manifest.json \\
  --quota-cooldown-seconds 900 \\
  --quota-max-failures-per-task 3
\`\`\`
EOF

# orchestrator ledger template
write_file "$PACKETS_DIR/orchestrator_state.md" <<EOF
# ${WAVE} Orchestrator State

## Status Legend

- \`ready\`
- \`running\`
- \`completed\`
- \`blocked\`
- \`rejected\`

## Packet Status
EOF

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "[dry-run] append packet statuses to $PACKETS_DIR/orchestrator_state.md"
else
  if [[ "$OVERWRITE" -eq 1 || ! -s "$PACKETS_DIR/orchestrator_state.md" || "$(tail -n 1 "$PACKETS_DIR/orchestrator_state.md")" == "## Packet Status" ]]; then
    for ((i=0; i<TASK_COUNT; i++)); do
      id="${TASK_IDS[$i]}"
      status="blocked"
      if (( TASK_COUNT == 1 )); then
        status="ready"
      elif (( i < 2 )); then
        status="ready"
      fi
      printf -- "- \`%s\`: %s\n" "$id" "$status" >>"$PACKETS_DIR/orchestrator_state.md"
    done
    cat >>"$PACKETS_DIR/orchestrator_state.md" <<'EOF'

## Notes

- Add acceptance/rejection rationale per packet.
- Record validation evidence and blocker details.
EOF
    log "updated $PACKETS_DIR/orchestrator_state.md"
  fi
fi

# gate script (create if missing unless overwrite)
gate_path="$SCRIPTS_DIR/orchestrator_gate.sh"
write_file "$gate_path" 0755 <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

# Baseline integration gate for orchestrated refactor waves.
# Replace or extend this command set to match your repository stack.

ran_any=0

if command -v cargo >/dev/null 2>&1 && [[ -f Cargo.toml ]]; then
  ran_any=1
  cargo check --workspace
fi

if command -v swift >/dev/null 2>&1 && [[ -f Package.swift ]]; then
  ran_any=1
  swift build -c debug
fi

if command -v python3 >/dev/null 2>&1 && [[ -d tests ]]; then
  ran_any=1
  python3 -m pytest
fi

if command -v npm >/dev/null 2>&1 && [[ -f package.json ]]; then
  ran_any=1
  npm test --if-present
fi

if [[ "$ran_any" -eq 0 ]]; then
  echo "No gate commands executed. Customize scripts/orchestrator_gate.sh for this repo." >&2
  exit 2
fi
EOF

append_agents_contract "$REPO_ROOT/AGENTS.md"

log "bootstrap completed for wave '$WAVE' with $TASK_COUNT task(s)."
log "next steps:"
log "  1) Start planning from: docs/executor_packets/$WAVE/PLANNING_SESSION_PROMPT.md"
log "  2) Fill each packet's Allowed Files and Validation Commands."
log "  3) Refine depends_on in $PACKETS_DIR/manifest.json."
log "  4) Run: python3 -B scripts/live_orchestrator.py --manifest docs/executor_packets/$WAVE/manifest.json --dry-run --no-resume --max-parallel 3"
log "  5) Inspect: python3 -B scripts/live_orchestrator.py --report --manifest docs/executor_packets/$WAVE/manifest.json"
log "  6) For quota limits, add: --quota-cooldown-seconds 900 --quota-max-failures-per-task 3 [--quota-fail-fast]"

exit 0

: <<'__LIVE_ORCHESTRATOR_EMBEDDED_PAYLOAD__'
__LIVE_ORCHESTRATOR_PAYLOAD_BEGIN__
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
__LIVE_ORCHESTRATOR_PAYLOAD_END__
__LIVE_ORCHESTRATOR_EMBEDDED_PAYLOAD__
