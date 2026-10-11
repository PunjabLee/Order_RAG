#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IT-1.11 · CI 脚本单元自测（DD-13 测试分层之"单元层"骨架种子）。

运行：python3 -m unittest discover -s scripts/ci/tests
说明：仅标准库，无第三方依赖；后续业务单测合入时沿用本目录约定（tests/ + discover）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_commit_msg  # noqa: E402
import check_mermaid  # noqa: E402
import recalc_amount  # noqa: E402


class TestCommitMsg(unittest.TestCase):
    def assert_ok(self, msg):
        errors, _ = check_commit_msg.validate_message(msg)
        self.assertEqual(errors, [], f"应通过但报错: {errors}")

    def assert_reject(self, msg):
        errors, _ = check_commit_msg.validate_message(msg)
        self.assertNotEqual(errors, [], "应拒绝但通过了")

    def test_valid_standard(self):
        self.assert_ok(
            "feat(retrieval): 向量+关键词并行RRF融合与pre-filter\n\n"
            "Task: IT-2.05\nRefs: DD-3.4.3/US-008-S3\n"
        )

    def test_valid_ci_scope_extension_warns_not_blocks(self):
        # 未转正 scope（如 infra）仍告警放行；工程类 scope 已随计划 V1.1 转正（L-03）
        errors, warnings = check_commit_msg.validate_message(
            "ci(infra): 未转正 scope 验证\n\nTask: IT-1.11\nRefs: DD-13/附录D\n"
        )
        self.assertEqual(errors, [])
        self.assertTrue(any("scope" in w for w in warnings), "未知 scope 应告警")

    def test_engineering_scopes_formal_no_warning(self):
        for sc in ("repo", "ci", "eval", "scripts", "hooks", "plan"):
            errors, warnings = check_commit_msg.validate_message(
                f"chore({sc}): 工程 scope 转正验证\n\nTask: IT-1.11\nRefs: DD-13\n")
            self.assertEqual(errors, [])
            self.assertFalse(any("scope" in w for w in warnings), f"`{sc}` 应已枚举内零告警")

    def test_task_with_q_annotation(self):
        self.assert_ok("feat(promo): 联调spike桩\n\nTask: IT-1.01 [Q1]\nRefs: DD-15 D-1\n")

    def test_missing_task(self):
        self.assert_reject("fix(chat): 修个bug\n\nRefs: DD-7.3\n")

    def test_bad_type(self):
        self.assert_reject("wip(chat): 临时\n\nTask: IT-1.11\n")

    def test_no_space_after_colon(self):
        self.assert_reject("feat(chat):描述缺空格\n\nTask: IT-1.11\n")

    def test_subject_body_need_blank_line(self):
        self.assert_reject("docs(ci): README 更新\nTask: IT-1.11\nRefs: DD-13\n")

    def test_exempt_merge(self):
        errors, warnings = check_commit_msg.validate_message("Merge branch 'develop' into feature/IT-1.11-x")
        self.assertEqual(errors, [])
        self.assertTrue(any("豁免" in w for w in warnings))

    def test_exempt_revert_and_fixup(self):
        self.assert_ok("Revert \"feat(chat): x\"\n")
        self.assert_ok("fixup! ci(scripts): y\n")

    def test_na_requires_reason(self):
        self.assert_reject("docs(docs): 基线\n\nTask: N/A\n")
        # 白名单收敛（评审 M-4）：N/A 除理由外须显式 `Baseline:` trailer，否则拒绝
        self.assert_reject("docs(docs): 初始基线\n\nTask: N/A: v2.2-baseline 文档基线先于任务卡体系\n")
        self.assert_ok(
            "docs(docs): 初始基线\n\nTask: N/A: v2.2-baseline 文档基线先于任务卡体系\n"
            "Baseline: v2.2-baseline\n"
        )

    def test_na_legacy_commit_exemption(self):
        # 历史豁免登记表（CI 范围模式）：收敛规则前的已核准提交追认放行
        errors, _ = check_commit_msg.validate_message(
            "docs(repo): AGENTS.md\n\nTask: N/A: 工程配置，无对应任务卡\n", commit="90d894c1234")
        self.assertEqual(errors, [])

    def test_multiple_task_trailer_rejected(self):
        # 多 Task 由告警升级为拒绝（评审 M-4）
        self.assert_reject("feat(chat): a\n\nTask: IT-1.11\nTask: IT-1.09\n")
        errors, _ = check_commit_msg.validate_message(
            "docs(plan): 回写\n\nTask: IT-1.11\nTask: IT-1.09\n", commit="28d00011234")
        self.assertEqual(errors, [])  # 历史豁免登记追认

    def test_literal_task_in_body_not_counted(self):
        # 行首锚定（复核 N4）：正文提及字面 "Task:" 不误拒，仅行首 trailer 计数
        self.assert_ok(
            "feat(chat): 注释说明\n\n正文提及旧规则 Task: 相\u5173关键字已修复。\n"
            "Task: IT-1.11\nRefs: DD-7\n")


class TestMermaid(unittest.TestCase):
    def scan(self, text):
        return check_mermaid.scan_text("t.md", text)

    def test_balanced_ok_headers(self):
        text = "前\n\n```mermaid\nflowchart TB\n  A-->B\n```\n后\n```json\n{}\n```\n"
        self.assertEqual(self.scan(text), [])

    def test_odd_fence(self):
        text = "```mermaid\nsequenceDiagram\nA->>B: x\n```\n```json\n{}\n"
        vs = self.scan(text)
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0].kind, "fence-unclosed")

    def test_bad_header(self):
        vs = self.scan("```mermaid\nflowchat LR\nA-->B\n```\n")
        self.assertEqual(vs[0].kind, "mermaid-header")
        self.assertEqual(vs[0].fence_no, 1)

    def test_statediagram_v2_ok(self):
        self.assertEqual(self.scan("```mermaid\nstateDiagram-v2\n[*]-->S\n```\n"), [])

    def test_literal_backslash_n_flagged(self):
        vs = self.scan("```mermaid\nflowchart TB\n  A[\"第一行\\n第二行\"]\n```\n")
        self.assertEqual(vs[0].kind, "mermaid-br")

    def test_br_tag_ok(self):
        self.assertEqual(self.scan("```mermaid\nflowchart TB\n  A[\"x<br/>y\"]\n```\n"), [])


class TestRecalc(unittest.TestCase):
    def setUp(self):
        cfg_path = Path(__file__).resolve().parent.parent / "pricing_baseline.json"
        import json
        self.cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    def test_default_yields_baseline(self):
        res = recalc_amount.recalc(self.cfg)
        self.assertEqual(res.total_cents, 8049700)          # DD-8.1 应得
        self.assertEqual(res.display, "80,497.00")
        self.assertEqual([abs(v) for _, v in res.breakdown], [8999700, 900000, 50000])

    def test_alt_sequence_matches_doc(self):
        res = recalc_amount.recalc(self.cfg, sequence="coupon_then_discount")
        self.assertEqual(res.total_cents, 8054730)           # 文档备选值 80,547.30
        self.assertEqual(res.display, "80,547.30")

    def test_coupon_threshold_not_met(self):
        cfg = dict(self.cfg, quantity=2)  # 2×29,999=59,998 → 折后 53,998.20 < 80,000
        res = recalc_amount.recalc(cfg)
        self.assertEqual(res.coupon_cents, 0)

    def test_discount_requires_min_quantity(self):
        cfg = dict(self.cfg, quantity=1, coupon_threshold_cents=0)
        res = recalc_amount.recalc(cfg)
        self.assertEqual(res.discount_cents, 0)

    def test_round_half_up(self):
        self.assertEqual(recalc_amount.round_half_up_div(5, 10), 1)     # 0.5 → 1
        self.assertEqual(recalc_amount.round_half_up_div(8999700 * 1000, 10000), 899970)
        self.assertEqual(recalc_amount.round_to_step(899970, 100), 900000)

    def test_doc_grep_on_real_design_doc(self):
        doc = Path(__file__).resolve().parent.parent.parent.parent / "设计文档.md"
        if not doc.is_file():
            self.skipTest("设计文档不在仓库预期位置")
        res = recalc_amount.recalc(self.cfg)
        issues = recalc_amount.check_doc(doc.read_text(encoding="utf-8"), res)
        self.assertEqual(issues, [], f"文档数值不一致: {issues}")


if __name__ == "__main__":
    unittest.main()
