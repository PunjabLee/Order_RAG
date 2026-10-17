# -*- coding: utf-8 -*-
"""中文数量/型号归一化用例集（DD-3.1 归一化行；DoD："归一化用例集 CI 过"）。

覆盖：中文数字（含 十/百/千/万 组合、口语 两/俩、大写 壹~捌）、模糊量词触发
数量澄清、型号别名 → sku（含一别名多 sku 的实体歧义）、全角/大小写归一。
"""
from __future__ import annotations

import unittest

from src.nlu.config import load_config
from src.nlu.normalize import (
    extract_quantity,
    match_model_alias,
    normalize_text,
    parse_chinese_number,
    sku_candidates,
)

CFG = load_config()


class TestNormalizeText(unittest.TestCase):
    def test_fullwidth_and_casefold(self):
        self.assertEqual(normalize_text("ＭＡＣ　Studio"), "mac studio")

    def test_slash_spacing_unified(self):
        self.assertEqual(normalize_text("64G / 1T"), "64g/1t")
        self.assertEqual(normalize_text("64G／1T"), "64g/1t")

    def test_whitespace_collapsed(self):
        self.assertEqual(normalize_text("  m2   ultra \n 64g "), "m2 ultra 64g")

    def test_deterministic_idempotent(self):
        once = normalize_text("MacBook  Pro　1台")
        self.assertEqual(once, normalize_text(once))


class TestChineseNumber(unittest.TestCase):
    def test_basic_digits(self):
        cases = {"一": 1, "三": 3, "八": 8, "两": 2, "俩": 2,
                 "壹": 1, "叁": 3, "捌": 8, "十": 10, "十五": 15}
        for text, want in cases.items():
            self.assertEqual(parse_chinese_number(text), want, text)

    def test_composed_numbers(self):
        cases = {"二十五": 25, "两百": 200, "一千": 1000, "三万二千": 32000,
                 "一百零五": 105, "二千零五十": 2050, "十": 10}
        for text, want in cases.items():
            self.assertEqual(parse_chinese_number(text), want, text)

    def test_arabic_string(self):
        self.assertEqual(parse_chinese_number("7"), 7)
        self.assertEqual(parse_chinese_number("32"), 32)

    def test_illegal_input_raises_not_silent_none(self):
        """禁静默兜底：非法输入必须 ValueError（由调用方显式处理）。"""
        for bad in ("", "  ", "abc", "三台", "3.5"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_chinese_number(bad)


class TestExtractQuantity(unittest.TestCase):
    """DD-3.1："三台"→3；"一批"→触发数量澄清。"""

    def test_chinese_numeral_with_unit(self):
        r = extract_quantity("我要三台", CFG)
        self.assertEqual(r.value, 3)
        self.assertFalse(r.ambiguous)
        self.assertFalse(r.needs_clarification)

    def test_arabic_with_unit(self):
        self.assertEqual(extract_quantity("来 10 台", CFG).value, 10)

    def test_large_chinese_number(self):
        self.assertEqual(extract_quantity("采购二十五台", CFG).value, 25)

    def test_colloquial_liang_without_unit(self):
        r = extract_quantity("俩32G的多少钱", CFG)
        self.assertEqual(r.value, 2)

    def test_ambiguous_quantifier_triggers_clarification(self):
        for phrase in ("一批", "若干", "几台", "十来个"):
            r = extract_quantity(f"这个要{phrase}", CFG)
            self.assertTrue(r.ambiguous, phrase)
            self.assertIsNone(r.value, phrase)
            self.assertTrue(r.needs_clarification, phrase)
            self.assertIn(phrase, r.ambiguous_phrases)

    def test_mixed_values_are_not_silently_picked(self):
        """多值（三台和两台）→ value=None，交调用方澄清/校验，禁猜测。"""
        r = extract_quantity("三台和两台分别多久到", CFG)
        self.assertEqual(r.values, [3, 2])
        self.assertIsNone(r.value)

    def test_negative_rejected_as_invalid_not_silent(self):
        r = extract_quantity("-3台 可以吗", CFG)
        self.assertTrue(r.negative)
        self.assertIsNone(r.value)

    def test_decimal_or_runon_quantity_never_guessed(self):
        """R6 验收：小数/连号不猜尾数（原实现 "3.5台"→5 静默取错）。"""
        for bad in ("3.5台", "1.2.3台", "买29,999台", "要 3.5 台"):
            r = extract_quantity(bad, CFG)
            with self.subTest(q=bad):
                self.assertTrue(r.invalid, bad)
                self.assertTrue(r.ambiguous, "invalid 归入模糊/待校验处置")
                self.assertIsNone(r.value, bad)
                self.assertTrue(r.ambiguous_phrases, bad)

    def test_below_one_quantity_marked_invalid(self):
        """R6 验收："0台"/"零台" 与 DD-3.3.1 "1≤q" 冲突 → invalid，不路由。"""
        for bad in ("0台", "零台", "来0台"):
            r = extract_quantity(bad, CFG)
            with self.subTest(q=bad):
                self.assertTrue(r.invalid, bad)
                self.assertIsNone(r.value, bad)
                self.assertNotIn(0, r.values, "非法值不得进入 values")

    def test_one_is_valid_minimum(self):
        """边界：quantity=1 合法（DD-3.3.1 "1≤q"）。"""
        r = extract_quantity("要1台", CFG)
        self.assertEqual(r.value, 1)
        self.assertFalse(r.invalid)
        self.assertFalse(r.ambiguous)

    def test_ordinal_is_not_quantity(self):
        """序数『第一个』≠ 数量（指代消解属 IT-2.03/DD-3.2.4）。"""
        r = extract_quantity("第一个到了吗", CFG)
        self.assertEqual(r.values, [])
        self.assertIsNone(r.value)

    def test_no_quantity(self):
        r = extract_quantity("多少钱", CFG)
        self.assertFalse(r.ambiguous)
        self.assertEqual(r.values, [])

    def test_mixed_chinese_arabic_in_sentence(self):
        r = extract_quantity("买三台M2 Ultra 64G/1T", CFG)
        self.assertEqual(r.value, 3)


class TestModelAlias(unittest.TestCase):
    """型号别名 → sku（model_alias 表配置化，真实别名挂 Q7）。"""

    def test_pending_question_marked(self):
        self.assertEqual(CFG.alias_pending_question(), "Q7")

    def test_longest_alias_wins_single_candidate(self):
        m = match_model_alias("M2 Ultra 64G/1T 多少钱", CFG)
        self.assertEqual(sku_candidates(m), ["SKU_A"])

    def test_full_name_not_double_counted(self):
        m = match_model_alias("mac studio m2 ultra 64g/1t 报价", CFG)
        self.assertEqual(sku_candidates(m), ["SKU_A"])
        self.assertEqual(len(m), 1, "最长别名占用区间，不得被 64g/ultra 重复命中")

    def test_one_alias_multiple_sku_is_ambiguity(self):
        """决策表行 4：同一别名映射多 sku → options 候选（点选澄清）。"""
        m = match_model_alias("Ultra那款什么价", CFG)
        self.assertEqual(sku_candidates(m), ["SKU_A", "SKU_B", "SKU_D"])

    def test_colloquial_alias(self):
        self.assertEqual(sku_candidates(match_model_alias("顶配多久到", CFG)),
                         ["SKU_B"])
        self.assertEqual(sku_candidates(match_model_alias("入门款有货吗", CFG)),
                         ["SKU_C"])

    def test_fullwidth_alias_normalized(self):
        m = match_model_alias("ＭＡＣ　Ｓｔｕｄｉｏ　Ｍ２　Ｕｌｔｒａ　64G/1T", CFG)
        self.assertIn("SKU_A", sku_candidates(m))

    def test_no_alias_returns_empty(self):
        self.assertEqual(match_model_alias("今天天气怎么样", CFG), [])
        self.assertEqual(sku_candidates([]), [])

    def test_alias_table_is_config_driven(self):
        """别名来自配置而非硬编码：改配置即改结果。"""
        import copy
        data = copy.deepcopy(CFG.raw())
        data["model_alias"]["entries"] = [{"alias": "占位机型", "sku": "SKU_Z"}]
        from src.nlu.config import Config
        probe = Config(data, source="test")
        self.assertEqual(
            sku_candidates(match_model_alias("占位机型多少钱", probe)), ["SKU_Z"])


if __name__ == "__main__":
    unittest.main()
