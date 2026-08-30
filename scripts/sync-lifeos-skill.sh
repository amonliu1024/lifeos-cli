#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: sync-lifeos-skill.sh --target <cc-switch|smartwork> [--check|--apply]"
}

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(dirname "$script_dir")"
source_dir="$repo_root/skills/lifeos"
target_name=""
mode="check"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      target_name="$2"
      shift 2
      ;;
    --check)
      mode="check"
      shift
      ;;
    --apply)
      mode="apply"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

case "$target_name" in
  cc-switch)
    target_dir="$HOME/.cc-switch/skills/lifeos"
    target_label="cc-switch"
    ;;
  smartwork)
    target_dir="$HOME/.SmartWork/skills/lifeos"
    target_label="SmartWork"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if [ ! -f "$source_dir/SKILL.md" ]; then
  echo "ERROR: missing LifeOS Skill source: $source_dir/SKILL.md" >&2
  exit 1
fi

rsync_args=(
  -a
  --delete
  --delete-excluded
  --exclude=.DS_Store
  --exclude=__pycache__
  --exclude='*.pyc'
)

if [ "$mode" = "check" ]; then
  if [ ! -d "$target_dir" ]; then
    echo "OUTDATED: $target_label target does not exist: $target_dir" >&2
    exit 1
  fi
  changes="$(rsync "${rsync_args[@]}" --dry-run --itemize-changes "$source_dir/" "$target_dir/")"
  if [ -n "$changes" ]; then
    echo "OUTDATED: $target_label target differs from source: $target_dir" >&2
    printf '%s\n' "$changes"
    exit 1
  fi
  echo "UP TO DATE: $target_label LifeOS Skill"
  exit 0
fi

mkdir -p "$target_dir"
rsync "${rsync_args[@]}" "$source_dir/" "$target_dir/"
changes="$(rsync "${rsync_args[@]}" --dry-run --itemize-changes "$source_dir/" "$target_dir/")"
if [ -n "$changes" ]; then
  echo "ERROR: $target_label target still differs after apply" >&2
  printf '%s\n' "$changes" >&2
  exit 1
fi
echo "APPLIED AND VERIFIED: $target_label LifeOS Skill"
