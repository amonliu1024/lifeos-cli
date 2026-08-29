#!/usr/bin/env bash
set -eo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"
exec python3 -m unittest discover -s tests -p 'test_*.py' "$@"
