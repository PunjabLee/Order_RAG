# -*- coding: utf-8 -*-
"""契约守卫：意图七枚举单源与对齐（DD-3.1 / 契约红线"枚举变更走 §7 变更控制专道"）。

本测试**对齐而非变更**枚举：断言 src.nlu.intents 与评测侧
eval/metrics/validate_schema.py::INTENTS、ANNOTATION_GUIDE §2 命名完全一致。
另含两条 IT-2.02 硬约束守卫：nlu 侧禁金额、禁网络。
"""
from __future__ import annotations

import importlib.util
import os
import re
import unittest

from src.nlu import intents as cat
from src.nlu.config import load_config

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                         "..", "..", ".."))
NLU_DIR = os.path.join(REPO_ROOT, "src", "nlu")
NLU_MODULES = ("intents.py", "router.py", "normalize.py", "config.py",
               "schema.py", "providers.py", "__init__.py")

EXPECTED_SEVEN = {
    "PRICE_QUERY", "PROMO_QUERY", "DELIVERY_QUERY", "ORDER_CREATE",
    "ORDER_QUERY", "OUT_OF_SCOPE", "ESCALATE_HUMAN",
}

#: 金额字面守卫：cents 记账字段、¥/$/元 金额字面、夹具锚点单价（29,999 系示例值）
_MONEY_IDENT_RE = re.compile(r"[A-Za-z0-9]_cents\b")
_MONEY_LITERAL_RE = re.compile(r"[¥$]\s*\d|\d+(?:[.,]\d{3})+\s*(?:元|块|块钱)")
_MONEY_ANCHORS = ("29999", "29,999", "8049700", "8000000", "2999")

#: 零网络守卫（IT-2.02 架构约束 2）
_NETWORK_RE = re.compile(
    r"^\s*(?:import|from)\s+(?:requests|urllib|http|socket|ssl|ftplib|smtplib|telnetlib|httpx|aiohttp)\b",
    re.M)


def _read(fname: str) -> str:
    with open(os.path.join(NLU_DIR, fname), encoding="utf-8") as fh:
        return fh.read()


def _code_only(src_text: str) -> str:
    """去掉整行注释（docstring 中的约束说明不算实现）。"""
    return "\n".join(ln for ln in src_text.splitlines()
                     if not ln.strip().startswith("#"))


def _load_validate_schema_intents():
    path = os.path.join(REPO_ROOT, "eval", "metrics", "validate_schema.py")
    spec = importlib.util.spec_from_file_location("_vs_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.INTENTS)


class TestIntentEnumContract(unittest.TestCase):
    def test_exactly_seven_enums(self):
        self.assertEqual(len(cat.INTENTS), 7)
        self.assertEqual(len(set(cat.INTENTS)), 7, "枚举不得重复")

    def test_enum_names_match_dd_3_1(self):
        self.assertEqual(set(cat.INTENTS), EXPECTED_SEVEN)

    def test_alignment_with_eval_schema(self):
        """契约红线：与评测 schema 单源一致（validate_schema.py INTENTS）。"""
        self.assertEqual(set(cat.INTENTS), _load_validate_schema_intents())

    def test_unknown_intent_rejected_everywhere(self):
        for fn in (cat.route_targets, cat.required_slots, cat.slots_of):
            with self.assertRaises(ValueError):
                fn("CHAT_GPT")


class TestNoAmount(unittest.TestCase):
    """IT-2.02 约束：金额相关值一律不得出现在 nlu 代码/配置。"""

    def test_no_amount_in_module_source(self):
        for fname in NLU_MODULES:
            code = _code_only(_read(fname))
            self.assertIsNone(_MONEY_IDENT_RE.search(code),
                              f"{fname} 出现 cents 记账字段")
            self.assertIsNone(_MONEY_LITERAL_RE.search(code),
                              f"{fname} 出现金额字面")
            for anchor in _MONEY_ANCHORS:
                self.assertNotIn(anchor, code, f"{fname} 含夹具金额锚点 {anchor}")

    def test_no_amount_keys_in_defaults(self):
        cfg = load_config(env={})   # R14②：钉死隔离，禁读真实 os.environ
        text = _read(os.path.join("config", "nlu.defaults.json"))

        def walk(node, path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    self.assertNotIn("_cents", str(k), f"{path}.{k}")
                    walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")
        walk(cfg.raw())
        for banned in _MONEY_ANCHORS + ("¥",):
            self.assertNotIn(banned, text, "nlu.defaults.json 含金额值 " + banned)


class TestZeroNetwork(unittest.TestCase):
    """架构约束 2：nlu 模块零网络（不 import requests/http/socket 等）。"""

    def test_no_network_imports(self):
        for fname in NLU_MODULES:
            self.assertIsNone(_NETWORK_RE.search(_read(fname)),
                              f"{fname} 引入网络依赖")

    def test_no_third_party_runtime_imports(self):
        allowed = {"json", "os", "re", "sys", "unicodedata", "dataclasses",
                   "importlib", "argparse", "collections", "typing",
                   "src.nlu", "__future__"}
        for fname in NLU_MODULES:
            for mod in re.findall(r"^\s*(?:import|from)\s+([A-Za-z_.][\w.]*)",
                                  _read(fname), re.M):
                root = mod.split(".")[0]
                self.assertIn(root, {a.split(".")[0] for a in allowed},
                              f"{fname} 引入非标准库/非本项目模块 {mod}")


class TestRouteTargets(unittest.TestCase):
    def test_out_of_scope_route_is_empty(self):
        """US-007-S4：领域外路由目标恒空（引导话术，不调 API）。"""
        self.assertEqual(cat.route_targets(cat.OUT_OF_SCOPE), ())

    def test_readonly_targets_declared(self):
        self.assertIn("product_service", cat.route_targets(cat.PRICE_QUERY))
        self.assertIn("promo_service", cat.route_targets(cat.PROMO_QUERY))
        self.assertIn("logistics_service", cat.route_targets(cat.DELIVERY_QUERY))
        self.assertEqual(cat.route_targets(cat.ORDER_QUERY), ("order_service",))
        # ORDER_CREATE 仅经应用服务草稿组装（DD-2.2 BIZ→DRAFT），无直写权限（ADR-001）
        self.assertEqual(cat.route_targets(cat.ORDER_CREATE), ("draft_assembly",))

    def test_every_intent_has_declaration(self):
        for it in cat.INTENTS:
            self.assertIsInstance(cat.route_targets(it), tuple)


class TestSlotMatrix(unittest.TestCase):
    """DD-3.3.2 必填矩阵关键行守卫（●/○/✕）。"""

    def test_price_query_matrix(self):
        self.assertEqual(cat.required_slots(cat.PRICE_QUERY), {"product"})
        self.assertIn("quantity", cat.optional_slots(cat.PRICE_QUERY))

    def test_promo_query_matrix(self):
        self.assertEqual(cat.required_slots(cat.PROMO_QUERY), {"sku", "quantity"})
        self.assertIn("product", cat.optional_slots(cat.PROMO_QUERY))  # 可由 sku 推出

    def test_delivery_and_order_required(self):
        for it in (cat.DELIVERY_QUERY, cat.ORDER_CREATE):
            self.assertEqual(cat.required_slots(it),
                             {"sku", "quantity", "address_id"}, it)

    def test_order_query_order_id(self):
        self.assertEqual(cat.required_slots(cat.ORDER_QUERY), {"order_id"})

    def test_slot_names_are_dd_3_3_1(self):
        allowed = {"product", "sku", "quantity", "address_id", "coupon_id",
                   "delivery_date", "order_id"}
        for it in cat.INTENTS:
            self.assertTrue(cat.slots_of(it) <= allowed, it)

    def test_forbidden_disjoint_required(self):
        for it in cat.INTENTS:
            self.assertFalse(cat.required_slots(it) & cat.forbidden_slots(it), it)


if __name__ == "__main__":
    unittest.main()
