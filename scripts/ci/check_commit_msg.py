#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IT-1.11 · commit-msg 校验器（钩子 + CI 范围模式）。

权威依据：《研发迭代计划.md》§8.3 提交规范（Conventional Commits + 任务可追溯）：

    <type>(<scope>): <描述>

    Task: IT-x.xx
    Refs: <DD/PRD 章节 或 US 用例 | Open Question>

    type：feat/fix/docs/refactor/test/ci/chore/perf/build
    scope：chat/session/nlu/retrieval/factcheck/slot/draft/txn-saga/security/
           observability/contracts/docs

硬约束（§8.3）：① 每条提交必携 `Task: IT-x.xx`（DoD 回写与进度看板依赖此关联）；
③ 禁 `--no-verify`（禁跳钩子由分支保护/评审保障，本脚本无法强制）。

用法：
    python3 check_commit_msg.py <MSG_FILE>      # commit-msg 钩子模式（参数=消息文件）
    python3 check_commit_msg.py --range A..B    # CI 模式：校验范围内每条提交
    python3 check_commit_msg.py --rev <SHA>     # CI 模式：校验单条提交

返回码：0 = 通过（可能有 warning）；非 0 = 拒绝（钩子阻断提交 / CI 步骤失败）。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# ---------------------------------------------------------------- 枚举与规则
# type 枚举：§8.3，违规即拒绝。
TYPES = ("feat", "fix", "docs", "refactor", "test", "ci", "chore", "perf", "build")
# scope 枚举：§8.3 给定的是业务模块口径。计划文档尚未定义 infra/ci 类工程 scope，
# 因此本脚本：scope 语法强制（小写 kebab），枚举不符仅告警不阻断。
# 依据见文件头说明；扩展 scope 入表建议已记入 IT-1.11 交付报告"发现与建议"（不擅改计划文档）。
SCOPES = (
    "chat", "session", "nlu", "retrieval", "factcheck", "slot", "draft",
    "txn-saga", "security", "observability", "contracts", "docs",
)
SCOPE_SYNTAX_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[a-z0-9-]+)*$")

# 标题：<type>[(<scope>)][!]: <描述>（描述非空）
SUBJECT_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^()]*)\))?(?P<bang>!)?: (?P<desc>\S.*)$"
)
# 正文 trailer：Task: IT-<数字>.<数字>，允许尾部 [Qn] 注记（对应计划 §1 任务卡标注格式）
TASK_OK_RE = re.compile(r"^Task:[ \t]+IT-\d+\.\d+(?:[ \t]+\[Q\d+\])?[ \t]*$", re.M)
# 豁免白名单 1：`Task: N/A` 仅限"初始 docs 基线"类提交，且必须随行给出理由
# （理由 = N/A 同行冒号后的文字，或正文独立 `Reason:` 行）。
# 白名单理由：v2.2-baseline 之前的文档基线提交产生于任务卡体系（§1/§8.3）建立之前，
# 无法回溯任务 ID；此后新提交一律走 Task: IT-x.xx。
TASK_NA_RE = re.compile(r"^Task:[ \t]+N/A(?P<inline>[：:][ \t]*\S.*)?[ \t]*$", re.M)
# 白名单收敛（评审 M-4）：N/A 需显式 `Baseline:` trailer；历史豁免＝已核准提交登记表（含双 Task 遗留）
BASELINE_RE = re.compile(r"^Baseline:[ \t]+\S+", re.M)
LEGACY_EXEMPT_COMMITS = {
    "90d894c": "N/A 无 Baseline trailer（AGENTS.md 初始化，收敛规则前提交，追认）",
    "942367c": "N/A 无 Baseline trailer（post-commit 钩子，收敛规则前提交，追认）",
    "28d0001": "双 Task trailer（状态回写覆盖 IT-1.09/1.11，多 Task 拒绝规则前提交，追认）",
}
REASON_RE = re.compile(r"^Reason:[ \t]+\S.{2,}$", re.M)
# 豁免白名单 2：机器生成/中间态提交——
#   Merge*：平台或 --no-ff 合并自动生成，非人工撰写，合入 squash（§8.3④）后主线不保留；
#   Revert*：git revert 自动生成，可追溯性由被回退提交保证；
#   fixup!/squash!：交互压缩中间态，压缩后的最终提交信息受本规则约束。
EXEMPT_SUBJECT_RE = re.compile(r"^(?:Merge\b|Revert\b|fixup!\s|squash!\s)")

SUBJECT_SOFT_LIMIT = 72  # 告警阈值（约定俗成；§8.3 未定义硬性长度）


def _clean_lines(text: str) -> list[str]:
    """去掉 git commit -v 的 # 注释与剪绒线之后的 diff，保留原始顺序。"""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("# ------------------------ >8"):
            break
        if raw.startswith("#"):
            continue
        lines.append(raw.rstrip())
    return lines


def validate_message(text: str, commit: str | None = None) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。errors 非空即应拒绝。"""
    errors: list[str] = []
    warnings: list[str] = []

    lines = _clean_lines(text)
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return (["提交信息为空（§8.3：必须含标题与 Task trailer）"], warnings)

    subject = lines[first].strip()

    # --- 标题格式 ---
    m = SUBJECT_RE.match(subject)
    if EXEMPT_SUBJECT_RE.match(subject):
        warnings.append(f"豁免提交（Merge/Revert/fixup!/squash! 白名单）：跳过格式与 Task 校验 → {subject[:60]}")
        return (errors, warnings)

    if not m:
        errors.append(
            "标题不符合 Conventional Commits：应为 `<type>(<scope>): <描述>`"
            "（§8.3，注意冒号后有一个空格、描述非空）"
        )
    else:
        t = m.group("type")
        if t not in TYPES:
            errors.append(f"type `{t}` 不在枚举：{'/'.join(TYPES)}（§8.3）")
        if t != t.lower():
            errors.append(f"type 必须小写：`{t}` → `{t.lower()}`")
        scope = m.group("scope")
        if scope is None:
            warnings.append("缺少 scope：§8.3 模板为 type(scope): 描述，建议补齐")
        elif not SCOPE_SYNTAX_RE.match(scope):
            errors.append(
                f"scope `{scope}` 语法非法：应为小写 kebab-case（可 `a/b` 组合），§8.3"
            )
        elif scope not in SCOPES:
            warnings.append(
                f"scope `{scope}` 不在 §8.3 枚举（业务模块口径）；工程/基建类扩展 scope"
                " 待计划修订确认后转正，本次放行"
            )
        if m.group("bang"):
            warnings.append(
                "破坏性变更 `!`：契约类改动须走 contracts/* 分支并双人 Approve（§8.2/§8.4），"
                "并在 MR 声明兼容性"
            )
    if len(subject) > SUBJECT_SOFT_LIMIT:
        warnings.append(f"标题 {len(subject)} 字符 > {SUBJECT_SOFT_LIMIT}，建议精简")

    # --- 标题与正文空行 ---
    rest = [ln for ln in lines[first + 1:] if ln.strip()]
    if rest and lines[first + 1].strip():
        errors.append("标题与正文之间必须空一行（trailer 解析依赖此结构）")

    # --- Task trailer（硬约束①；N/A 白名单收敛，评审 M-4）---
    body = "\n".join(lines)
    legacy = (commit or "")[:7] in LEGACY_EXEMPT_COMMITS
    if not TASK_OK_RE.search(body):
        na = TASK_NA_RE.search(body)
        if not na:
            errors.append(
                "缺少 `Task: IT-x.xx` trailer（§8.3 硬约束①）。豁免通道："
                "① Merge/Revert/fixup!/squash! 前缀自动豁免；"
                "② 基线序列遗留提交可用 `Task: N/A: <理由>` + `Baseline: <tag|hash>` 显式标记"
                "（CI 范围模式另认历史豁免登记表 LEGACY_EXEMPT_COMMITS）"
            )
        elif not (na.group("inline") or REASON_RE.search(body)):
            errors.append(
                "`Task: N/A` 必须附理由：同行 `Task: N/A: <理由>` 或正文 `Reason: <理由>`"
                "（仅限基线类提交）"
            )
        elif not (BASELINE_RE.search(body) or legacy):
            errors.append(
                "`Task: N/A` 需显式 `Baseline: <tag|hash>` trailer（§8.3 白名单收敛，评审 M-4）；"
                "新提交一律走 `Task: IT-x.xx`"
            )
        else:
            warnings.append("Task: N/A 经 Baseline trailer/历史豁免登记放行，请确认确属基线序列提交")
    if body.count("Task:") > 1 and not legacy:
        errors.append(
            "出现多条 `Task:` trailer：一任务一提交，跨任务请拆分（§8.3 ④，已升级为拒绝，评审 M-4）"
        )

    # --- Refs trailer（§8.3 模板项，非硬约束 → 告警）---
    if not re.search(r"^Refs:[ \t]+\S", body, re.M):
        warnings.append("建议补 `Refs: <DD/PRD 章节 或 US 用例>`（§8.3 模板）")

    return (errors, warnings)


# ---------------------------------------------------------------- 输入模式
def check_file(path: str) -> int:
    with open(path, "r", encoding="utf-8") as fh:
        return _report(f"<MSG_FILE {path}>", fh.read())


def _git(*args: str) -> str:
    return subprocess.run(
        ("git", *args), capture_output=True, text=True, check=True
    ).stdout


def check_range(rev_range: str) -> int:
    shas = _git("rev-list", rev_range).split()
    if not shas:
        print(f"[check-commit-msg] 范围 {rev_range} 无提交，跳过")
        return 0
    rc = 0
    for sha in shas:
        msg = _git("show", "-s", "--format=%B", sha)
        rc |= _report(sha[:10], msg, quiet_ok=True, commit=sha)
    return rc


def _report(label: str, msg: str, quiet_ok: bool = False, commit: str | None = None) -> int:
    errors, warnings = validate_message(msg, commit=commit)
    for w in warnings:
        print(f"[check-commit-msg] ⚠ {label}: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"[check-commit-msg] ✗ {label}: {e}", file=sys.stderr)
        print("[check-commit-msg] 拒绝：规范见《研发迭代计划.md》§8.3", file=sys.stderr)
        return 1
    if not quiet_ok:
        print(f"[check-commit-msg] ✓ {label} 通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="commit-msg 校验器（IT-1.11，§8.3）")
    ap.add_argument("msg_file", nargs="?", help="commit-msg 钩子传入的消息文件")
    ap.add_argument("--range", dest="rev_range", help="CI 模式：如 origin/develop..HEAD")
    ap.add_argument("--rev", dest="rev", help="CI 模式：单个提交 SHA")
    args = ap.parse_args(argv)

    if args.rev_range:
        return check_range(args.rev_range)
    if args.rev:
        return _report(args.rev[:10], _git("show", "-s", "--format=%B", args.rev), commit=args.rev)
    if args.msg_file:
        return check_file(args.msg_file)
    ap.error("需要 <MSG_FILE> 或 --range/--rev")


if __name__ == "__main__":
    sys.exit(main())
