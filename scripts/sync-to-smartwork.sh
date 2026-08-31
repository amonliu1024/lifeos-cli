#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
target_dir="$HOME/.SmartWork/skills/lifeos"

echo "=== Sync to SmartWork: $target_dir ==="
mkdir -p "$target_dir"
rsync -a --delete "$script_dir/../skills/lifeos/" "$target_dir/"
echo "  OK: lifeos"
echo "=== Done: 1 skill synced ==="
