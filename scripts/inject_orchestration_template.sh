#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/inject_orchestration_template.sh [options]

Inject this orchestration template into a target repository.

Options:
  --target-repo PATH      Target repository root (required).
  --wave NAME             Wave directory name. Default: wave_1
  --id-prefix PREFIX      Task ID prefix. Default: W
  --start-id N            First numeric task id. Default: 101
  --task-count N          Number of packet skeletons. Default: 3
  --agents-context-file   Optional markdown/text file appended to target AGENTS.md.
  --agents-context-text   Optional one-line context text appended to target AGENTS.md.
  --overwrite             Overwrite template files already in target repo.
  --dry-run               Print actions without writing files.
  -h, --help              Show this help message.
EOF
}

log() {
  printf '[inject] %s\n' "$*"
}

die() {
  printf '[inject] error: %s\n' "$*" >&2
  exit 2
}

require_arg_value() {
  local flag="$1"
  local value="${2:-}"
  if [[ -z "$value" || "$value" == -* ]]; then
    die "missing value for $flag"
  fi
}

copy_file() {
  local src="$1"
  local dest="$2"
  local mode="${3:-0644}"

  if [[ -f "$dest" && "$OVERWRITE" -eq 0 ]]; then
    log "skip existing $dest (use --overwrite to replace)"
    return 0
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "[dry-run] copy $src -> $dest"
    return 0
  fi

  mkdir -p "$(dirname "$dest")"
  cp "$src" "$dest"
  chmod "$mode" "$dest"
  log "copied $src -> $dest"
}

TARGET_REPO=""
WAVE="wave_1"
ID_PREFIX="W"
START_ID=101
TASK_COUNT=3
AGENTS_CONTEXT_FILE=""
AGENTS_CONTEXT_TEXT=""
OVERWRITE=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-repo)
      require_arg_value "$1" "${2:-}"
      TARGET_REPO="${2:-}"
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

[[ -n "$TARGET_REPO" ]] || die "--target-repo is required"
[[ -d "$TARGET_REPO" ]] || die "--target-repo does not exist: $TARGET_REPO"
if [[ -n "$AGENTS_CONTEXT_FILE" ]]; then
  [[ -f "$AGENTS_CONTEXT_FILE" ]] || die "--agents-context-file not found: $AGENTS_CONTEXT_FILE"
fi

TARGET_REPO="$(cd "$TARGET_REPO" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BOOTSTRAP_SOURCE="$TEMPLATE_ROOT/bootstrap_orchestrator_wave.sh"

[[ -f "$BOOTSTRAP_SOURCE" ]] || die "bootstrap script missing at $BOOTSTRAP_SOURCE"
[[ -f "$TEMPLATE_ROOT/live_orchestrator.py" ]] || die "live_orchestrator.py missing in template root"

copy_file "$TEMPLATE_ROOT/bootstrap_orchestrator_wave.sh" "$TARGET_REPO/scripts/bootstrap_orchestrator_wave.sh" 0755
copy_file "$TEMPLATE_ROOT/live_orchestrator.py" "$TARGET_REPO/scripts/live_orchestrator.py" 0755
copy_file "$TEMPLATE_ROOT/scripts/gpu_exec.py" "$TARGET_REPO/scripts/gpu_exec.py" 0755
copy_file "$TEMPLATE_ROOT/scripts/gpu_modal_app.py" "$TARGET_REPO/scripts/gpu_modal_app.py" 0755
copy_file "$TEMPLATE_ROOT/config/gpu_backend.toml" "$TARGET_REPO/config/gpu_backend.toml" 0644
copy_file "$TEMPLATE_ROOT/docs/gpu_orchestration.md" "$TARGET_REPO/docs/gpu_orchestration.md" 0644

cmd=(
  bash "$BOOTSTRAP_SOURCE"
  --repo-root "$TARGET_REPO"
  --wave "$WAVE"
  --id-prefix "$ID_PREFIX"
  --start-id "$START_ID"
  --task-count "$TASK_COUNT"
  --orchestrator-source "$TEMPLATE_ROOT/live_orchestrator.py"
)
if [[ -n "$AGENTS_CONTEXT_FILE" ]]; then
  cmd+=(--agents-context-file "$AGENTS_CONTEXT_FILE")
fi
if [[ -n "$AGENTS_CONTEXT_TEXT" ]]; then
  cmd+=(--agents-context-text "$AGENTS_CONTEXT_TEXT")
fi
if [[ "$OVERWRITE" -eq 1 ]]; then
  cmd+=(--overwrite)
fi
if [[ "$DRY_RUN" -eq 1 ]]; then
  cmd+=(--dry-run)
fi

log "running bootstrap against target repository"
"${cmd[@]}"

log "template injection complete"
