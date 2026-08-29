#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE="$REPO_ROOT/skills/lifeos"
TARGET="$HOME/.cc-switch/skills/lifeos"

if [ ! -f "$SOURCE/SKILL.md" ]; then
  echo "ERROR: missing LifeOS Skill source: $SOURCE/SKILL.md" >&2
  exit 1
fi

echo "=== Sync LifeOS Skill to cc-switch: $TARGET ==="
mkdir -p "$TARGET"
rsync -a --delete --delete-excluded \
  --exclude='.DS_Store' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "$SOURCE/" "$TARGET/"
find "$TARGET" -name '.DS_Store' -delete 2>/dev/null || true
echo "=== Done ==="
