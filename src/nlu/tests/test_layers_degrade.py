# -*- coding: utf-8 -*-
"""分层路由与降级链用例（DD-3.1 分层策略 + LLM 结构化输出契约）。

链路：规则/词表（≥conf_rule_strong 直路由）→ 小模型（≥conf_low 采纳）
→ 低于 conf_low 或多轮复杂升级 LLM（JSON Schema 约束）→
解析失败重试 llm_retry_max 次 → 降级小模型结果 → 模板澄清；
**仅 JSON/Schema 解析失败**逐次计入 nlu.parse_fail（DD-3.1 L629，评审 R5）；
provider 未注入/调用异常/返回 None 为独立事件 nlu.llm_not_wired / nlu.llm_error，
不混入 parse_fail。

全部 provider 均为内存假实现（零网络、无真实模型）。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from src.nlu import intents as cat
from src.nlu.config import load_config
from src.nlu.providers import HeuristicBigramSmallModel
from src.nlu.router import Action, Layer, Pipeline
from src.nlu.schema import (
    build_nlu_schema,
    validate_json_schema,
    validate_nlu_output,
)
from src.nlu.tests.fakes import (
    ApiCallSpy,
    FakeLlmProvider,
    FakeSmallModel,
    ParseFailRecorder,
)

CFG = load_config(env={})
NLU_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEUTRAL = "咋回事啊"          # 规则层无命中、无上下文信号的干净输入


def make(cfg=None, small=None, llm=None):
    spy, fails = ApiCallSpy(), ParseFailRecorder()
    pipe = Pipeline(cfg or CFG, small_model=small, llm=llm,
                    on_api_call=spy, on_parse_fail=fails)
    return pipe, spy, fails


class TestLayerOrdering(unittest.TestCase):
    def test_rule_strong_hit_skips_models(self):
        small, llm = FakeSmallModel({cat.PRICE_QUERY: 0.99}), FakeLlmProvider([{}])
        pipe, _, _ = make(small=small, llm=llm)
        dec = pipe.understand("M2 Ultra 64G/1T 报价")
        self.assertEqual(dec.layer, Layer.RULE)
        self.assertEqual(dec.confidence, CFG.conf_rule_strong)
        self.assertEqual(small.calls, [], "规则强命中不应调用小模型")
        self.assertEqual(llm.calls, [], "规则强命中不应调用 LLM")

    def test_small_model_adopted_when_above_conf_low(self):
        small = FakeSmallModel({cat.PRICE_QUERY: CFG.conf_low})
        llm = FakeLlmProvider([FakeLlmProvider.payload()])
        pipe, spy, _ = make(small=small, llm=llm)
        dec = pipe.understand(NEUTRAL, {"slots": {"product": "mac studio"}})
        self.assertEqual(dec.layer, Layer.SMALL)
        self.assertEqual(dec.action, Action.ROUTE)
        self.assertEqual(llm.calls, [])
        self.assertEqual(spy.calls, dec.api_calls)

    def test_small_model_below_conf_low_escalates(self):
        small = FakeSmallModel({cat.PRICE_QUERY: CFG.conf_low - 0.05})
        llm = FakeLlmProvider([FakeLlmProvider.payload(
            confidence=0.9, entities={"product": "mac studio"})])
        pipe, _, _ = make(small=small, llm=llm)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(dec.layer, Layer.LLM)

    def test_small_model_scores_filtered_by_catalog(self):
        """provider 越界输出（非七枚举 intent）一律丢弃，不污染路由。"""
        small = FakeSmallModel({"CHAT_GPT": 0.99, cat.PRICE_QUERY: 0.7})
        pipe, _, _ = make(small=small)
        dec = pipe.understand(NEUTRAL, {"slots": {"product": "x"}})
        self.assertEqual(dec.intent, cat.PRICE_QUERY)


class TestDegradeChain(unittest.TestCase):
    def test_parse_fail_twice_then_template_clarify(self):
        """Fake provider 解析失败×2（1 次 + 重试 1 次）→ 模板澄清 + parse_fail 计数。"""
        llm = FakeLlmProvider(["这不是 JSON {{{"])
        pipe, spy, fails = make(llm=llm)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(dec.llm_attempts, CFG.llm_retry_max + 1)
        self.assertEqual(dec.llm_attempts, 2)
        self.assertEqual(dec.parse_fail_events, 2)
        self.assertEqual(fails.count, 2, "每次失败即时计入 nlu.parse_fail")
        self.assertEqual(fails.metric_names(), {"nlu.parse_fail"})
        self.assertEqual(dec.layer, Layer.FALLBACK)
        self.assertEqual(dec.action, Action.CLARIFY)
        self.assertEqual(dec.template, CFG.template("clarify_llm_fail"))
        self.assertEqual(spy.calls, [], "降级澄清不得声明 API 调用")
        # 事件载荷（DD-11.2 指标维度）
        ev = fails.events[0]
        self.assertEqual(set(("metric", "query", "attempt", "errors")) - set(ev), set())
        self.assertEqual(ev["attempt"], 1)

    def test_pure_parse_failure_still_counts_parse_fail(self):
        """R5 对照：纯 JSON/Schema 解析失败逐次计 nlu.parse_fail（口径不放宽）。"""
        llm = FakeLlmProvider(["非 JSON {{"])
        pipe, _, fails = make(llm=llm)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(fails.metric_names(), {"nlu.parse_fail"})
        self.assertEqual(fails.count, CFG.llm_retry_max + 1)
        self.assertEqual(dec.parse_fail_events, CFG.llm_retry_max + 1)
        self.assertEqual(dec.llm_attempts, CFG.llm_retry_max + 1)

    def test_retry_then_success(self):
        good = FakeLlmProvider.payload(intent=cat.PRICE_QUERY, confidence=0.9,
                                       entities={"product": "mac studio"})
        llm = FakeLlmProvider(["坏输出", good])
        pipe, spy, fails = make(llm=llm)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(dec.llm_attempts, 2)
        self.assertEqual(dec.parse_fail_events, 1)
        self.assertEqual(fails.count, 1)
        self.assertEqual(dec.layer, Layer.LLM)
        self.assertEqual(dec.action, Action.ROUTE)
        self.assertEqual(spy.calls, dec.api_calls)

    def test_degrade_to_small_model_result(self):
        """LLM 全失败 → 降级小模型结果（多轮复杂场景小模型本已被跳过）。"""
        small = FakeSmallModel({cat.PRICE_QUERY: 0.65})
        llm = FakeLlmProvider([None])          # 调用失败（返回 None）
        pipe, spy, fails = make(small=small, llm=llm)
        ctx = {"turn_count": CFG.complex_turn_threshold,
               "slots": {"product": "mac studio"}}
        dec = pipe.understand(NEUTRAL, ctx)
        self.assertEqual(dec.layer, Layer.SMALL)
        self.assertEqual(dec.intent, cat.PRICE_QUERY)
        self.assertEqual(dec.action, Action.ROUTE)
        self.assertIn("降级小模型", dec.notes)
        self.assertEqual(fails.count, CFG.llm_retry_max + 1)
        # R5：provider 返回 None＝可用性事件，逐次计 llm_error、不计 parse_fail
        self.assertEqual(fails.metric_names(), {"nlu.llm_error"})
        self.assertEqual(dec.parse_fail_events, 0)
        self.assertEqual(spy.calls, dec.api_calls)

    def test_provider_exception_enters_degrade_chain(self):
        llm = FakeLlmProvider([RuntimeError("网关超时")])
        pipe, _, fails = make(llm=llm)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(dec.action, Action.CLARIFY)
        self.assertEqual(fails.count, 2)
        self.assertIn("provider_error", json.dumps(fails.events, ensure_ascii=False))
        # R5：异常逐次计 nlu.llm_error（独立事件名），不污染 parse_fail 口径
        self.assertEqual(fails.metric_names(), {"nlu.llm_error"})
        self.assertEqual(dec.parse_fail_events, 0)

    def test_retry_count_comes_from_config(self):
        cfg = load_config(env={"NLU_LLM_RETRY_MAX": "2"})
        llm = FakeLlmProvider(["垃圾"])
        pipe, _, fails = make(cfg=cfg, llm=llm)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(dec.llm_attempts, 3, "重试次数必须走配置（llm_retry_max）")
        self.assertEqual(fails.count, 3)

    def test_conf_low_threshold_comes_from_config(self):
        """nlu.conf_low 覆盖后同一小模型分数不再被采纳 → 升级 LLM（禁硬编码）。"""
        small_scores = {cat.PRICE_QUERY: 0.65}
        cfg_high = load_config(env={"NLU_CONF_LOW": "0.7"})
        llm = FakeLlmProvider([FakeLlmProvider.payload(
            intent=cat.PRICE_QUERY, confidence=0.95,
            entities={"product": "mac studio"})])
        pipe, _, _ = make(cfg=cfg_high, small=FakeSmallModel(dict(small_scores)),
                          llm=llm)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(dec.layer, Layer.LLM)
        self.assertEqual(len(llm.calls), 1)
        # 默认 0.6 时同分直接被小模型层采纳
        pipe2, _, _ = make(small=FakeSmallModel(dict(small_scores)), llm=llm)
        self.assertEqual(pipe2.understand(NEUTRAL, {"slots": {"product": "x"}}).layer,
                         Layer.SMALL)

    def test_llm_payload_slots_merge_into_decision(self):
        llm = FakeLlmProvider([FakeLlmProvider.payload(
            intent=cat.PROMO_QUERY, confidence=0.88,
            entities={"sku": "SKU_C", "quantity": 3})])
        pipe, spy, _ = make(llm=llm)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(dec.slots["sku"], "SKU_C")
        self.assertEqual(dec.slots["quantity"], 3)
        self.assertEqual(dec.action, Action.ROUTE)
        self.assertEqual(spy.calls, dec.api_calls)


class TestLlmSchemaContract(unittest.TestCase):
    """DD-3.1 LLM 结构化输出契约：枚举受限 / 类型受限 / additionalProperties:false。"""

    def setUp(self):
        self.schema = build_nlu_schema()

    def test_schema_shape(self):
        self.assertEqual(self.schema["properties"]["intent"]["enum"], list(cat.INTENTS))
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertIs(self.schema["properties"]["entities"]["additionalProperties"],
                      False)
        self.assertEqual(self.schema["required"], ["intent", "confidence"])
        self.assertEqual(self.schema["properties"]["confidence"]["maximum"], 1)

    def test_valid_payload_passes(self):
        errs = validate_nlu_output({
            "intent": cat.PRICE_QUERY, "confidence": 0.8,
            "entities": {"product": "mac studio", "quantity": 3},
            "missing_slots": []}, self.schema)
        self.assertEqual(errs, [])

    def test_illegal_payloads_rejected(self):
        cases = [
            ({"intent": "CHAT_GPT", "confidence": 0.9}, "枚举"),
            ({"intent": cat.PRICE_QUERY, "confidence": 1.5}, "maximum"),
            ({"intent": cat.PRICE_QUERY, "confidence": 0.9,
              "reasoning": "脑补"}, "additionalProperties"),
            ({"intent": cat.PRICE_QUERY, "confidence": 0.9,
              "entities": {"quantity": "3"}}, "integer"),
            ({"intent": cat.PRICE_QUERY, "confidence": 0.9,
              "entities": {"unknown_field": 1}}, "additionalProperties"),
            ({"intent": cat.PRICE_QUERY}, "缺必填"),
            ({"intent": cat.ORDER_CREATE, "confidence": 0.9,
              "missing_slots": ["no_such_slot"]}, "枚举"),
            ({"intent": cat.PRICE_QUERY, "confidence": "高"}, "number"),
        ]
        for payload, hint in cases:
            errs = validate_nlu_output(payload, self.schema)
            with self.subTest(payload=json.dumps(payload, ensure_ascii=False)):
                self.assertTrue(errs, "非法输出必须校验失败")

    def test_minimal_subtree_validator(self):
        self.assertEqual(validate_json_schema("x", {"type": "string"}), [])
        self.assertTrue(validate_json_schema(5, {"type": "string"}))
        self.assertTrue(validate_json_schema(True, {"type": "integer"}),
                        "bool 不得当作 integer")
        self.assertTrue(validate_json_schema([1, "a"],
                                             {"type": "array",
                                              "items": {"type": "string"}}))

    def test_schema_violation_counts_as_parse_fail(self):
        bad = FakeLlmProvider.payload(intent="CHAT_GPT", confidence=0.9)
        llm = FakeLlmProvider([bad])
        pipe, _, fails = make(llm=llm)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(fails.count, CFG.llm_retry_max + 1)
        self.assertEqual(dec.action, Action.CLARIFY)
        self.assertNotEqual(dec.intent, "CHAT_GPT")

    def test_llm_receives_schema_and_attempt(self):
        llm = FakeLlmProvider([FakeLlmProvider.payload(
            confidence=0.9, entities={"product": "p"})])
        pipe, _, _ = make(llm=llm)
        pipe.understand(NEUTRAL)
        self.assertEqual(llm.calls[0]["attempt"], 1)
        self.assertEqual(llm.calls[0]["schema"]["properties"]["intent"]["enum"],
                         list(cat.INTENTS))

    def test_llm_provider_absent_clarifies_without_crash(self):
        """R5 口径：未注入 LLM（本轮基线）→ **不计任何解析类指标**——独立事件
        nlu.llm_not_wired、llm_attempts=0；降级链末端模板澄清行为保持。"""
        small = FakeSmallModel({cat.PRICE_QUERY: 0.5})
        pipe, spy, fails = make(small=small, llm=None)
        dec = pipe.understand(NEUTRAL)
        self.assertEqual(dec.action, Action.CLARIFY)
        self.assertEqual(dec.template, CFG.template("clarify_llm_fail"))
        self.assertEqual(dec.llm_attempts, 0, "未注入不产生 LLM 尝试数")
        self.assertEqual(dec.parse_fail_events, 0)
        self.assertEqual(fails.count, 1, "可观测性由独立事件承载（禁静默）")
        self.assertEqual(fails.metric_names(), {"nlu.llm_not_wired"},
                         "未注入 ≠ parse_fail（DD-3.1 L629 仅解析失败计入）")
        self.assertEqual(spy.calls, [])


class TestHeuristicSmallModelBaseline(unittest.TestCase):
    """启发式基线（本轮交付＝可插拔接口 + 规则统计基线，非训练模型）。"""

    def test_scores_stay_below_rule_strong_confidence(self):
        model = HeuristicBigramSmallModel(CFG)
        scores = model.classify("M2 Ultra 64G/1T 多少钱", "m2 ultra 64g/1t 多少钱")
        self.assertTrue(scores)
        self.assertLess(max(scores.values()), CFG.conf_rule_strong,
                        "小模型不得冒充规则层高置信")
        self.assertGreaterEqual(min(scores.values()), CFG.small_model_base)

    def _overlay(self, mutate) -> str:
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "config", "nlu.defaults.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        mutate(data)
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        self.addCleanup(os.unlink, path)
        return path

    def test_score_band_comes_from_config(self):
        path = self._overlay(lambda d: d["scoring"].update(
            {"small_model_base": 0.2, "small_model_cap": 0.3}))
        cfg = load_config(env={"NLU_CONFIG_FILE": path})
        model = HeuristicBigramSmallModel(cfg)
        scores = model.classify("有什么优惠", "有什么优惠")
        self.assertTrue(scores)
        self.assertTrue(all(0.2 <= v <= 0.3 for v in scores.values()), scores)

    def test_inconsistent_score_band_is_fatal(self):
        """打分标尺次序非法（base > cap）→ 配置致命，不静默截断。"""
        path = self._overlay(lambda d: d["scoring"].update({"small_model_cap": 0.1}))
        with self.assertRaises(Exception) as ctx:
            load_config(env={"NLU_CONFIG_FILE": path})
        self.assertIn("small_model", str(ctx.exception))

    def test_interface_not_implemented(self):
        from src.nlu.providers import LlmProvider, SmallModelProvider
        with self.assertRaises(NotImplementedError):
            SmallModelProvider().classify("q", "q")
        with self.assertRaises(NotImplementedError):
            LlmProvider().generate("q", {}, 1)


class TestNoHardcodedThresholds(unittest.TestCase):
    """禁硬编码 **AST 白名单守卫**（评审 R7 改造；DD-16 R-07 / DD-3.1 阈值配置化）。

    旧黑名单（BANNED_FLOATS＝已知阈值枚举）可被绕过：把字面改成其它数值
    （如 0.55）或新增未列入的阈值即逃逸。白名单口径反转——业务模块
    （router/normalize/providers/schema/intents，**R7 起含 config.py**）内的
    ast.Constant 数值字面仅允许显式豁免集，其余一律失败：

    - COMMON_ALLOWED＝0/1：概率闭界 [0,1]、循环/下标起点、最小计数下界
      （DD-3.3.1 "1≤q" 为文档常数；业务阈值数值一律走 thresholds/scoring 配置）；
    - MODULE_ALLOWED：逐模块补充的**结构/语言事实**（见行内注释，逐项注明理由）；
    - STRUCTURAL_NAMES：模块级结构常量赋值（中文数字系统表 _CN_*、槽位矩阵
      SLOT_MATRIX 等）整子树豁免——其数值是语言学/文档结构常量，非业务阈值。

    自证（不入库）：临时向 router.py 注入 `best.confidence < 0.55` 字面后本类
    两用例必须翻红——已按评审要求在临时副本验证后还原。
    """

    COMMON_ALLOWED = {0, 1}
    MODULE_ALLOWED = {
        "router.py": {2},        # 决策表行 3 结构定义：扇出＝"恰好两个只读意图"
        "normalize.py": {2},     # 口语"俩"＝2（语言事实，DD-3.1 中文数字归一）
        "providers.py": {2, 4},  # bigram 窗口＝2、打分取整 4 位（统计结构常量）
        "schema.py": set(),      # 0/1 已覆盖（confidence [0,1]、quantity minimum 1）
        "intents.py": set(),
        "config.py": set(),      # 0/1 已覆盖（阈值区间校验的闭界 [0,1]）
    }
    STRUCTURAL_NAMES = {
        "normalize.py": {"_CN_DIGIT", "_CN_UNIT", "_CN_SECTION"},
        "intents.py": {"SLOT_MATRIX", "ROUTE_TARGETS"},
        "config.py": {"REQUIRED_PATHS", "_ENV_THRESHOLD_MAP"},
    }
    MODULES = ("router.py", "normalize.py", "providers.py", "schema.py",
               "intents.py", "config.py")

    def _numeric_exemptions(self, fname, tree):
        """模块级结构常量赋值子树内的数值节点 id 集合（显式豁免）。"""
        import ast
        exempt = set()
        want = self.STRUCTURAL_NAMES.get(fname, set())
        if not want:
            return exempt
        for node in tree.body:
            targets, value = [], None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if value is not None and names & want:
                for c in ast.walk(value):
                    if (isinstance(c, ast.Constant)
                            and isinstance(c.value, (int, float))
                            and not isinstance(c.value, bool)):
                        exempt.add(id(c))
        return exempt

    def _offenders(self, fname):
        """返回白名单之外的数值字面 [(行号, 值)]。"""
        import ast
        path = os.path.join(NLU_DIR, fname)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=fname)
        allowed = set(self.COMMON_ALLOWED) | self.MODULE_ALLOWED.get(fname, set())
        exempt = self._numeric_exemptions(fname, tree)
        bad = []
        for c in ast.walk(tree):
            if id(c) in exempt:
                continue
            if (isinstance(c, ast.Constant)
                    and isinstance(c.value, (int, float))
                    and not isinstance(c.value, bool)
                    and c.value not in allowed):
                bad.append((c.lineno, c.value))
        return bad

    def test_business_modules_free_of_unexempted_numeric_literals(self):
        for fname in self.MODULES:
            bad = self._offenders(fname)
            self.assertFalse(bad,
                             f"{fname} 出现白名单外数值字面（行,值）：{bad}")

    def test_router_reads_thresholds_from_config(self):
        """R7 升级：由文本存在性 → AST 检查。判定处（Compare）比较右值不得为
        白名单外数值字面——业务置信/重试/轮数阈值必须来自 cfg.*（DD-3.1）。"""
        import ast
        path = os.path.join(NLU_DIR, "router.py")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        tree = ast.parse(text, filename="router.py")
        allowed = set(self.COMMON_ALLOWED) | self.MODULE_ALLOWED["router.py"]
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for comp in node.comparators:
                v = comp
                if (isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.USub)
                        and isinstance(v.operand, ast.Constant)):
                    v = v.operand          # -1 与 1 同源判定
                if (isinstance(v, ast.Constant)
                        and isinstance(v.value, (int, float))
                        and not isinstance(v.value, bool)
                        and v.value not in allowed):
                    bad.append((node.lineno, v.value))
        self.assertFalse(bad,
                         f"router 判定处比较右值含数值字面阈值（行,值）：{bad}")
        # 文本存在性保留：防改名后 cfg 读取点整体消失而 AST 仍空转
        for name in ("conf_low", "conf_oos", "conf_rule_strong",
                     "conf_rule_weak", "llm_retry_max", "short_input_max_chars",
                     "complex_turn_threshold"):
            self.assertIn(f"cfg.{name}", text, f"router 未从配置读取 {name}")


if __name__ == "__main__":
    unittest.main()
