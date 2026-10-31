#!/usr/bin/env bash
# 把上游最新 master 合进本地 local 分支。
# 用法: bash local/sync-upstream.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "工作区不干净，先 commit 或 stash:" >&2
  git status --short >&2
  exit 1
fi

echo "==> 拉取上游"
git fetch upstream --prune

BEHIND="$(git rev-list --count local..upstream/master)"
if [ "$BEHIND" = "0" ]; then
  echo "已经是最新的 upstream/master，无需同步。"
  exit 0
fi
echo "==> 上游有 $BEHIND 个新提交，开始 rebase"
git rebase upstream/master

echo "==> 跑测试"
if [ -x .venv/Scripts/python.exe ]; then
  PY=.venv/Scripts/python.exe
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  echo "没找到 .venv，跳过测试（先 python -m venv .venv && .venv/Scripts/python.exe -m pip install -e '.[test]'）"
  exit 0
fi

"$PY" -m pytest tests/ -q
