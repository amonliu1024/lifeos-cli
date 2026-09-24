#!/usr/bin/env bash
# 把当前工作树离线装进本机 pipx：用系统 Python 自带的 setuptools 打 wheel，再让 pipx 装这个 wheel。
# 全程不访问任何索引，与 pip 配置和网络环境无关。
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

if command -v pipx >/dev/null 2>&1; then
  pipx=(pipx)
elif python3 -m pipx --version >/dev/null 2>&1; then
  pipx=(python3 -m pipx)
else
  echo "ERROR: 找不到 pipx（PATH 里没有，python3 -m pipx 也不可用）" >&2
  exit 1
fi

python3 -c "import setuptools" 2>/dev/null || {
  echo "ERROR: 系统 python3 缺少 setuptools，无法离线构建" >&2
  exit 1
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "=== Build wheel (offline) ==="
python3 -m pip wheel --no-build-isolation --no-deps --no-index --quiet -w "$work" .
wheel="$(ls "$work"/lifeos_cli-*.whl)"
echo "  $(basename "$wheel")"

echo "=== Install into pipx ==="
"${pipx[@]}" install --force --pip-args=--no-index "$wheel" >/dev/null
lifeos --version
