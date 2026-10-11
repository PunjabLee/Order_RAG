#!/bin/sh
# IT-1.11 · 安装本地 Git 钩子：把 hooks 目录指向仓内版本化的 .githooks/。
# 仅执行 git config（仓库级），不触碰全局配置，不做任何 commit/branch/tag/push。
set -eu

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

chmod +x .githooks/commit-msg
git config core.hooksPath .githooks

echo "[install-hooks] core.hooksPath = $(git config --get core.hooksPath)  (repo: $ROOT)"
echo "[install-hooks] 完成。commit-msg 卡口生效：规范见《研发迭代计划.md》§8.3"
