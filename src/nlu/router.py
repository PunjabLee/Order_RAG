# -*- coding: utf-8 -*-
"""查询理解分层路由（DD-3.1 整改 M-2：信号→动作决策表 + 阈值收口）。

分层策略（阈值全部走配置，禁硬编码；nlu.conf_low 默认 0.6）：
1. 规则/词表命中（词表置信 ≥conf_rule_strong=0.9）→ 直接路由（目标 ≤50ms）；
2. 小模型（provider 可注入；≥conf_low 采纳）→ 路由；
3. 低于 conf_low 或多轮复杂 → LLM 结构化输出（JSON Schema 约束、intent 枚举
   受限、additionalProperties:false）；解析失败重试 llm_retry_max 次 →
   降级小模型结果 → 模板澄清；**解析失败**计入 nlu.parse_fail 指标回调
   （DD-11.2；provider 未注入/异常为独立事件 nlu.llm_not_wired / nlu.llm_error，
   不混入 parse_fail —— 评审 R5）。

信号→处置决策表（8 行）逐行落地于 _detect_signals/_finalize：
  1 指代/序数      → 标注 coref，消解算法属 IT-2.03（DD-3.2.4）；置信<conf_low 再澄清
  2 省略句         → 从槽位快照补全；补不出则澄清
  3 多意图         → action=FANOUT（DD-3.5.3 DAG 并行扇出，IT-2.08 执行）
  4 实体歧义 1→N   → action=CLARIFY + options[]（点选，DD-7.3）
  5 槽位缺失       → 必填矩阵（DD-3.3.2）比对 → 定向澄清
  6 领域外         → intent=OUT_OF_SCOPE 或全部置信<conf_oos → 引导话术，禁止 API（US-007-S4）
  7 低置信 0.4–0.6 → 升级 LLM；仍低 → 澄清
  8 极短输入 ≤3字  → 含焦点实体则继承，否则澄清

合入前评审整改（IT-2.02 R1/R2/R3）：
- R1：指代/省略/极短输入在无继承证据时**不再提前返回澄清**——小模型层与 LLM
  兜底照常执行（行 7"低置信→升级 LLM"结构性可达），澄清下移为降级链末端；
- R2：FANOUT 前对每个参与意图跑必填矩阵/1→N 实体歧义校验，未过 → 澄清
  （可执行子集/待澄清子集记入 notes），未校验目标不得下发；
- R3：_collect_slots 多候选禁止静默取 cands[0] 且不得把 SKU id 写入 product；
  仅单候选且可映射商品名（命中别名）时回填。

二轮评审整改（N1/N2/N3/N4，修复后可合入）：
- N1：行 6 领域外判定在小模型段加护栏 `and not follow_up`——域内跟进信号
  （指代/省略/极短）在 softmax 低分时**不得**按领域外处置（省略/指代本身是
  域内信号，评审 R1 同源口径），继续下探至降级链末端澄清；LLM 段显式判定
  OUT_OF_SCOPE 属显式证据，保留；
- N2：焦点实体继承路径 refs[0] 为 SKU id 形态时仅写 sku，product 须经别名
  反查命中商品名才回填，否则留空走行 5 澄清（DD-3.3.1：product＝商品名/
  别名文本，SKU id 直写 product 且 ROUTE 违规）；
- N3：本轮命中非法/模糊数量表达（qty.ambiguous_phrases 非空，含 "0台"/"3.5台"
  等 invalid 形态）时 quantity 视同待校验——无论快照携带何种合法值都转澄清
  并清除快照值（用户显式表达不得被旧快照静默覆盖后 ROUTE；R6 验收），
  单意图与扇出两路同检；
- N4：两条"LLM 未接入"降级路径（各层均无证据早退分支与 _run_llm None 分支）
  统一恰计一次独立事件 nlu.llm_not_wired（attempt=0），消除 DD-11.2 观测盲区。

**零网络**：本模块只产出路由决策对象；API 调用由 IT-2.08 调用器执行，
决策经 on_api_call sink 记录（OUT_OF_SCOPE/澄清/引导恒不触发，测试以 call-spy 断言）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from src.nlu import intents as cat
from src.nlu.config import Config, ConfigError, load_config
from src.nlu.normalize import (
    QuantityResult,
    extract_quantity,
    match_model_alias,
    normalize_text,
    sku_candidates,
)
from src.nlu.schema import build_nlu_schema, validate_nlu_output


class Action:
    """处置动作（DD-3.1 决策表'处置动作'列的可测枚举）。"""
    ROUTE = "route"                 # 只读路由目标（IT-2.08 执行）
    FANOUT = "fanout"               # 多意图 DAG 并行扇出（DD-3.5.3）
    CLARIFY = "clarify"             # 澄清追问（模板或点选 options）
    GUIDE = "guide"                 # 领域外引导话术，禁止 API（US-007-S4）
    TRANSFER = "transfer"           # 转人工（US-009）


class Layer:
    RULE = "rule"
    SMALL = "small"
    LLM = "llm"
    FALLBACK = "fallback"           # 降级链末端（模板澄清）


class Signal:
    COREF = "coref"
    ELLIPSIS = "ellipsis"
    MULTI_INTENT = "multi_intent"
    SKU_AMBIGUOUS = "sku_ambiguous"
    SLOT_MISSING = "slot_missing"
    OUT_OF_SCOPE = "out_of_scope"
    LOW_CONFIDENCE = "low_confidence"
    SHORT_INPUT = "short_input"
    QUANTITY_AMBIGUOUS = "quantity_ambiguous"


@dataclass(frozen=True)
class ScoreLabel:
    intent: str
    confidence: float
    weight: float = 0.0             # 规则层证据强度（scoring.rule_weight_* 累计）
    matched: tuple = ()

    @property
    def is_readonly_route(self) -> bool:
        return self.intent in cat.INTENTS and self.intent not in (
            cat.OUT_OF_SCOPE, cat.ESCALATE_HUMAN)


@dataclass
class Decision:
    """路由决策对象（可 JSON 序列化进 trace，DD-11.1）。"""
    query: str
    normalized: str = ""
    intent: str | None = None
    confidence: float = 0.0
    layer: str = Layer.FALLBACK
    action: str = Action.CLARIFY
    api_calls: list = field(default_factory=list)   # 只读路由目标声明（OUT_OF_SCOPE 恒空）
    multi_intents: list = field(default_factory=list)
    missing_slots: list = field(default_factory=list)
    slots: dict = field(default_factory=dict)
    options: list = field(default_factory=list)      # 实体歧义点选（决策表行 4）
    signals: list = field(default_factory=list)
    template: str | None = None                      # 澄清/引导话术（模板直出基线）
    llm_attempts: int = 0
    parse_fail_events: int = 0
    notes: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    # ---- 便捷判定（测试/回调断言用） ----
    @property
    def is_out_of_scope(self) -> bool:
        return self.intent == cat.OUT_OF_SCOPE or self.action == Action.GUIDE

    @property
    def involves_api(self) -> bool:
        return self.action in (Action.ROUTE, Action.FANOUT) and bool(self.api_calls)


class RuleLayer:
    """词表/正则规则层：DD-3.1 分层策略第一层（词表置信 ≥0.9 直接路由）。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.priority = cfg.priority()
        # 证据权重来自配置标尺（scoring.*），非硬编码
        self.w_strong = cfg.rule_weight_strong
        self.w_weak = cfg.rule_weight_weak
        self._compiled = {}
        for intent in cat.INTENTS:
            v = cfg.vocab(intent)
            self._compiled[intent] = {
                "strong": list(v["strong"]),
                "weak": list(v["weak"]),
                "regex_strong": [re.compile(r) for r in v["regex_strong"]],
                "regex_weak": [re.compile(r) for r in v["regex_weak"]],
            }

    def classify(self, query: str) -> list:
        """返回按 (weight desc, priority asc) 排序的标签；无命中 → []。"""
        text = normalize_text(query)
        labels = []
        for intent, packs in self._compiled.items():
            matched, weight, has_strong = [], 0.0, False
            for term in packs["strong"]:
                if term and term in text:
                    matched.append(term)
                    weight += self.w_strong
                    has_strong = True
            for term in packs["weak"]:
                if term and term in text:
                    matched.append(term)
                    weight += self.w_weak
            for rx in packs["regex_strong"]:
                m = rx.search(text)
                if m:
                    matched.append(m.group(0))
                    weight += self.w_strong
                    has_strong = True
            for rx in packs["regex_weak"]:
                m = rx.search(text)
                if m:
                    matched.append(m.group(0))
                    weight += self.w_weak
            if weight > 0:
                conf = (self.cfg.conf_rule_strong if has_strong
                        else self.cfg.conf_rule_weak)
                labels.append(ScoreLabel(intent, conf, weight, tuple(matched)))
        labels.sort(key=lambda l: (-l.weight, self._prio(l.intent)))
        return labels

    def _prio(self, intent: str) -> int:
        return self.priority.index(intent)


class Pipeline:
    """规则 → 小模型 → LLM 兜底的分层查询理解管线（provider 可注入，零网络）。"""

    def __init__(self, cfg: Config | None = None, small_model=None, llm=None,
                 on_parse_fail=None, on_api_call=None):
        self.cfg = cfg or load_config()
        self.rule = RuleLayer(self.cfg)
        self.small_model = small_model
        self.llm = llm
        # 指标回调：nlu.parse_fail / nlu.llm_error / nlu.llm_not_wired（DD-11.2）；默认无操作
        self._on_parse_fail = on_parse_fail or (lambda event: None)
        # API 调用 sink：真实调用属 IT-2.08；本管线只声明+记账（call-spy 测试 US-007-S4）
        self._on_api_call = on_api_call

    # ------------------------------------------------------------------
    def understand(self, query: str, context: dict | None = None) -> Decision:
        cfg = self.cfg
        ctx = context or {}
        norm = normalize_text(query)
        dec = Decision(query=query, normalized=norm)

        # ---- 0. 归一化 + 信号检测（决策表行 1/2/4/8 的信号位） ----
        qty = extract_quantity(query, cfg)
        alias_matches = match_model_alias(query, cfg)
        cands = sku_candidates(alias_matches)
        signals = self._detect_signals(norm, ctx, qty, cands)
        dec.signals = signals
        # ---- 1. 规则/词表层（≥conf_rule_strong 直接路由） ----
        rule_labels = self.rule.classify(norm)
        best = rule_labels[0] if rule_labels else None

        # 决策表行 6/转接：领域外与转人工词表强/弱命中即短路
        # （US-007-S4：OUT_OF_SCOPE 处置＝引导话术，api_calls 恒空；
        #   "多少钱"类通用弱词不足以压制显式领域外信号，故这两类 ≥conf_rule_weak 即处置）
        if best is not None and best.confidence >= cfg.conf_rule_weak:
            if best.intent == cat.OUT_OF_SCOPE:
                return self._issue(self._oos_decision(dec, signals,
                                                      "词表命中领域外（DD-3.1 行 6，禁止 API 调用）"))
            if best.intent == cat.ESCALATE_HUMAN:
                dec.intent = cat.ESCALATE_HUMAN
                dec.confidence = best.confidence
                dec.layer = Layer.RULE
                dec.action = Action.TRANSFER
                dec.api_calls = []
                dec.template = cfg.template("escalate_ack")
                return self._issue(dec)

        slots = self._collect_slots(ctx, qty, cands, norm, alias_matches)
        dec.slots = slots

        # 决策表行 1/2/8：指代·省略·极短输入（规则层证据不足时）→ 先尝试上下文继承。
        # R1 整改（合入前评审）：无继承证据时**不再提前返回澄清**——小模型层与
        # LLM 兜底必须继续执行，否则行 7"低置信→升级 LLM"结构性不可达；
        # "澄清"下移为降级链末端（follow_up_clarify 标记由分层段末尾消费）。
        follow_up = (Signal.SHORT_INPUT in signals or Signal.COREF in signals
                     or Signal.ELLIPSIS in signals)
        follow_up_clarify = False
        if follow_up and (best is None or best.confidence < cfg.conf_low):
            inherited = self._inherit_context(dec, slots, ctx, qty, cands, signals)
            if inherited is not None:
                return inherited
            follow_up_clarify = True
            # 有指代/省略/极短信号但无法确定继承 → 继续走后续层；
            # **不得**按领域外处置：省略/指代本身是域内信号（评审 R1）

        # 决策表行 4（模糊别名 1→N sku → 点选澄清）统一在 _finalize 处置，
        # 位于意图判定之后——LLM/小模型仍有机会给出意图，再行 options 澄清。
        multi = self._rule_multi_intent(rule_labels)
        if multi:
            signals.append(Signal.MULTI_INTENT)
            dec.signals = signals
            return self._issue(self._fanout_decision(dec, multi, slots, cands,
                                                     qty, signals))
        if best is not None and best.confidence >= cfg.conf_rule_strong:
            dec.intent = best.intent
            dec.confidence = best.confidence
            dec.layer = Layer.RULE
            dec.multi_intents = [l.intent for l in rule_labels if l.weight > 0]
            return self._finalize(dec, slots, ctx, qty, cands, signals)

        # ---- 2. 小模型层（provider 可注入；≥conf_low 采纳） ----
        small_labels = self._run_small_model(norm, query)
        small_best = small_labels[0] if small_labels else None
        complex_multi_turn = self._is_complex_multi_turn(ctx)

        if (best is None or best.confidence < cfg.conf_low) and not complex_multi_turn:
            if small_best is not None and small_best.confidence >= cfg.conf_low:
                dec.intent = small_best.intent
                dec.confidence = small_best.confidence
                dec.layer = Layer.SMALL
                return self._finalize(dec, slots, ctx, qty, cands, signals)
            # 决策表行 6：全部置信 < conf_oos → 领域外处置（禁止 API）
            # N1 护栏（二轮评审）：有指代/省略/极短跟进信号（COREF/ELLIPSIS/
            # SHORT_INPUT 任一）即不按领域外处置——softmax 低分形态下跟进句
            # 继续下探至降级链末端澄清（评审 R1"省略/指代本身是域内信号"同源；
            # 否则 R1 整改后 follow-up 输入首次流经小模型段会被误贴引导话术）。
            # LLM 段显式判定 OUT_OF_SCOPE 属显式证据，保留（见下方行 6/7）。
            if (small_best is not None and small_best.confidence < cfg.conf_oos
                    and not follow_up):
                return self._issue(self._oos_decision(dec, signals,
                                                      "小模型全部置信低于 conf_oos（DD-3.1 行 6）"))

        # ---- 3. LLM 兜底（低置信 0.4–0.6 或多轮复杂；决策表行 7） ----
        # 注：规则层与小模型**均无输出**且未注入 LLM 时 → 澄清而非领域外
        #（行 6 的领域外判定需显式证据：领域外词表命中，或分类器全部置信 <conf_oos）。
        if best is not None and best.confidence >= cfg.conf_low and not complex_multi_turn:
            # 规则弱命中（conf_low ≤ c < conf_rule_strong）→ 规则层直接路由
            dec.intent = best.intent
            dec.confidence = best.confidence
            dec.layer = Layer.RULE
            return self._finalize(dec, slots, ctx, qty, cands, signals)

        signals.append(Signal.LOW_CONFIDENCE)
        dec.signals = signals
        if self.llm is None and best is None and small_best is None:
            # 各层均无证据且 LLM 未接入 → 降级链末端澄清（行 7/8/1/2）。
            # N4 整改（二轮评审）：补发与 _run_llm None 分支同形状的独立事件
            # nlu.llm_not_wired（attempt=0、不计尝试数/parse_fail）——本轮基线
            # （无 provider）最高频降级样本不得在 DD-11.2 观测上隐身，两条
            # 未接入路径口径一致（各恰计一次）。
            dec.notes_llm_errors = self._emit_llm_not_wired(dec)
            dec.action = Action.CLARIFY
            dec.layer = Layer.FALLBACK
            dec.template, dec.notes = self._follow_up_fallback(
                follow_up_clarify, signals)
            return self._issue(dec)
        llm_payload, llm_errors, attempts, fails = self._run_llm(norm, dec, small_best)
        dec.llm_attempts = attempts
        dec.parse_fail_events = fails
        if llm_payload is None:
            # 降级链：小模型结果 → 模板澄清（DD-3.1）
            if small_best is not None and small_best.confidence >= cfg.conf_low:
                dec.intent = small_best.intent
                dec.confidence = small_best.confidence
                dec.layer = Layer.SMALL
                dec.notes = "LLM 未产出（解析/调用失败，含重试）→ 降级小模型结果"
                return self._finalize(dec, slots, ctx, qty, cands, signals,
                                     ignore_llm_errors=True)
            dec.action = Action.CLARIFY
            dec.layer = Layer.FALLBACK
            dec.template = cfg.template("clarify_llm_fail")
            dec.notes = ("LLM 未产出（解析/调用失败，含重试）→ 降级小模型无果 → 模板澄清；"
                         "parse_fail 已计 " + str(fails))
            return self._issue(dec)

        intent = llm_payload["intent"]
        conf = float(llm_payload["confidence"])
        dec.intent = intent
        dec.confidence = conf
        dec.layer = Layer.LLM
        if intent == cat.OUT_OF_SCOPE or conf < cfg.conf_oos:
            # LLM 显式判定领域外/低置信 → 行 6 处置（显式证据，不受 N1 跟进
            # 信号护栏影响：小模型 softmax 低分是弱证据，LLM 判定是强证据）
            return self._issue(self._oos_decision(dec, signals,
                                                  "LLM 判定领域外或置信<conf_oos（行 6/7）"))
        if intent == cat.ESCALATE_HUMAN:
            dec.action = Action.TRANSFER
            dec.api_calls = []
            dec.template = cfg.template("escalate_ack")
            return self._issue(dec)
        llm_slots = dict(slots)
        for k, v in (llm_payload.get("entities") or {}).items():
            if v is not None:
                llm_slots[k] = v
        return self._finalize(dec, llm_slots, ctx, qty, cands, signals)

    def _follow_up_fallback(self, follow_up_clarify: bool, signals: list):
        """降级链末端的澄清话术（R1：继承/澄清下移至各层跑完之后）。

        返回 (template, notes)。省略/指代/极短为域内信号，恒不按领域外处置。
        """
        cfg = self.cfg
        if follow_up_clarify and Signal.SHORT_INPUT in signals:
            return (cfg.template("clarify_short_input"),
                    "极短输入且无继承证据，各层均无证据 → 澄清（DD-3.1 行 8，降级链末端）")
        if follow_up_clarify:
            return (cfg.template("clarify_generic"),
                    "指代/省略且上下文无可继承证据，各层均无证据 → 澄清"
                    "（DD-3.1 行 1/2，降级链末端；域内信号不按领域外处置）")
        return (cfg.template("clarify_generic"),
                "各层均无证据且 LLM 未接入 → 澄清（行 7 降级链末端）")

    def _inherit_context(self, dec: Decision, slots: dict, ctx: dict,
                         qty: QuantityResult, cands: list, signals: list):
        """决策表行 1/2/8 的继承处置：上一轮意图 + 焦点实体/槽位快照。

        返回 Decision 表示已处置；None 表示无继承证据（由调用方继续走后续层，
        澄清移至降级链末端 —— 评审 R1）。置信取配置 conf_coref_min
        （消解下限，默认＝conf_low＝0.6）：证据＝焦点实体单值 **或** 会话快照
        已有槽位；两者皆无 → 不继承（禁猜测）。
        """
        cfg = self.cfg
        last_intent = ctx.get("last_intent")
        if last_intent not in cat.INTENT_SET:
            return None
        focus = ctx.get("focus_entity") or {}
        refs = focus.get("ref") if isinstance(focus, dict) else None
        # 栈顶存在候选即可继承**意图**；实体仅当单值时才写入槽位（多候选不猜测，
        # 交由 _finalize 的必填矩阵/实体歧义澄清 —— DD-3.1 行 1"置信<conf_low 再澄清"）
        has_focus = isinstance(refs, list) and len(refs) >= 1
        has_snapshot = bool(ctx.get("slots"))
        if not (has_focus or has_snapshot):
            return None
        if len(cands) > 1:
            # 本轮自身仍有实体歧义 → 交由 _finalize 点选（行 4 优先于继承）
            pass
        dec.intent = last_intent
        dec.confidence = cfg.conf_coref_min
        dec.layer = Layer.RULE
        dec.notes = ("上下文继承：意图＝last_intent、实体＝焦点实体/槽位快照"
                     "（DD-3.1 行 1/2/8；消解算法本体属 IT-2.03）")
        return self._finalize(dec, slots, ctx, qty, cands, signals)

    # ------------------------------------------------------------------
    # 信号检测（决策表 8 行的检测方式列）
    def _detect_signals(self, norm: str, ctx: dict, qty: QuantityResult,
                        cands: list) -> list:
        cfg = self.cfg
        signals = []
        for pat in cfg.signals("coref"):
            if re.search(pat, norm):
                signals.append(Signal.COREF)
                break
        for pat in cfg.signals("ellipsis"):
            if pat in norm:
                signals.append(Signal.ELLIPSIS)
                break
        if len(norm.replace(" ", "")) <= cfg.short_input_max_chars:
            signals.append(Signal.SHORT_INPUT)
        if qty.ambiguous:
            signals.append(Signal.QUANTITY_AMBIGUOUS)
        if len(cands) > 1:
            signals.append(Signal.SKU_AMBIGUOUS)
        return signals

    def _collect_slots(self, ctx: dict, qty: QuantityResult, cands: list,
                       norm: str = "", alias_matches: list | None = None) -> dict:
        """槽位快照（DD-3.2 会话服务读侧）+ 归一化结果合并；只读、不校验（校验属 IT-2.04）。

        R3 整改（合入前评审）：多候选（1→N 实体歧义）**禁止**静默取 cands[0]，
        也不得把 SKU id 写入 product——不回填 sku/product，交由 _finalize 出
        options[] 点选；仅单候选且能映射到商品名（命中别名文本，DD-3.3.1
        product 来源＝商品服务名称/别名索引）时才回填 product。

        N2 整改（二轮评审，DD-3.3.1 同源）：焦点实体继承亦不得把 SKU id 写入
        product——refs[0] 以 SKU 前缀识别时仅写 sku；product 须经别名反查命中
        商品名才回填，否则留空（必填缺失 → 行 5 定向澄清），禁止携带 SKU id
        直接 ROUTE。

        F3 整改（终轮评审）：别名反查回填另加 sku 一致性守卫——焦点 ref 与
        快照 sku 错配时不回填（防跨实体错配 ROUTE），保持行 5 澄清兜底。
        """
        alias_matches = alias_matches or []
        slots = {}
        for name in cat.ALL_SLOTS + ("order_id",):
            v = (ctx.get("slots") or {}).get(name)
            if v is not None:
                slots[name] = v
        if qty.value is not None and "quantity" not in slots:
            slots["quantity"] = qty.value
        if len(cands) == 1 and "sku" not in slots:
            slots["sku"] = cands[0]
        # 焦点实体继承（DD-3.1 行 8 / DD-3.2.1 focus_entity；消解算法本体属 IT-2.03，
        # 此处仅做栈顶单值继承；多候选不继承、交由实体歧义澄清）
        focus = ctx.get("focus_entity") or {}
        refs = focus.get("ref") if isinstance(focus, dict) else None
        if isinstance(refs, list) and len(refs) == 1:
            ref = refs[0]
            if str(ref).upper().startswith("SKU"):
                # N2：SKU id 形态仅写 sku；product 只在别名反查命中商品名时回填
                if "sku" not in slots:
                    slots["sku"] = ref
                if "product" not in slots:
                    # F3 整改（终轮评审）：焦点 ref 与快照 sku 错配时不得把
                    # ref 的别名商品名回填进 slots——否则下游按 sku=快照值取价
                    # 却播报 product=焦点别名，跨实体错配 ROUTE。仅当生效 sku
                    # 即本焦点 ref 才回填；错配保持 product 留空→行 5 定向澄清。
                    # N5（复核评审）：一致性比较按 upper 规范化——与 ref 形态
                    # 识别（upper().startswith）及 _product_name_for_sku 同口径，
                    # 同一实体仅大小写差异不得误判错配。
                    if str(slots.get("sku", ref)).strip().upper() == str(ref).strip().upper():
                        product_name = self._product_name_for_sku(ref)
                        if product_name:
                            slots["product"] = product_name
            elif "product" not in slots:
                # 非 SKU id 形态（商品名/别名文本）→ 按 DD-3.3.1 原样继承 product
                slots["product"] = ref
        # product：商品词表（配置 slot_hints.product_terms）或型号别名命中（别名＝商品名级）
        if "product" not in slots:
            for term in self.cfg.get("slot_hints.product_terms"):
                if term and term in norm:
                    slots["product"] = term
                    break
            else:
                if len(cands) == 1:
                    # 单候选：以命中的**别名文本**（商品名形态）回填，不写 SKU id（R3）
                    for m in alias_matches:
                        if m.sku == cands[0] and m.alias:
                            slots["product"] = m.alias
                            break
        return slots

    def _product_name_for_sku(self, sku) -> str | None:
        """N2（二轮评审）：SKU id → 商品名/别名 的反查（DD-3.3.1 product 来源＝
        商品服务名称/别名索引；单源＝配置 model_alias.entries）。取配置表中该
        sku 的首个命中别名（配置顺序，确定性可复算）；无命中 → None（product
        留空，由行 5 必填澄清兜底，禁猜测、禁写回 SKU id）。真实商品名服务查询
        属商品服务联调回填（附录 F Q7）。
        """
        want = str(sku).upper()
        for entry in self.cfg.alias_entries():
            if str(entry.get("sku", "")).upper() == want and entry.get("alias"):
                return entry["alias"]
        return None

    def _rule_multi_intent(self, labels: list) -> list:
        """决策表行 3 的规则侧信号：恰好两个只读意图以同一最高权重强命中
        （US-007-S2『多少钱，什么时候到』）；≥3 视为歧义取 top（交由澄清/消解）。"""
        if len(labels) < 2:
            return []
        top_w = labels[0].weight
        # 权重为配置标尺（scoring.rule_weight_*）同序累加值，同命中结构下浮点
        # 等值确定（R7 守卫：不留 1e-9 容差字面，比较右值不得为数值字面）
        tied = [l for l in labels if l.weight == top_w
                and l.confidence >= self.cfg.conf_rule_strong]
        ro = [l for l in tied if l.is_readonly_route]
        if len(ro) == 2:
            return ro
        return []

    def _required_missing(self, intent: str, slots: dict, cands: list,
                          qty: QuantityResult) -> list:
        """单意图必填矩阵（DD-3.3.2）＋1→N 实体歧义校验（评审 R2/R3）。

        未消解的别名歧义（多候选且 sku 未落槽）按该意图涉及 sku 槽位记为
        缺失『sku』；模糊量词（"一批"）按涉及 quantity 槽位记为缺失『quantity』。

        N3 整改（二轮评审）：本轮命中非法/模糊数量表达（qty.ambiguous_phrases
        非空）时 quantity 视同待校验——**无论快照携带何种合法值**都记缺失，
        快照合法值不得静默覆盖本轮显式表达后扇出。
        """
        missing = sorted(s for s in cat.required_slots(intent)
                         if self._slot_missing_value(slots.get(s), s))
        relevant = cat.slots_of(intent)   # 该意图可携带的全部槽位（required∪optional）
        if (len(cands) > 1 and "sku" not in slots and "sku" in relevant
                and "sku" not in missing):
            missing.append("sku")
        if (qty.ambiguous and qty.ambiguous_phrases
                and "quantity" in relevant and "quantity" not in missing):
            missing.append("quantity")
        return missing

    @staticmethod
    def _slot_missing_value(v, slot: str) -> bool:
        """槽位缺失口径（评审 R6）：None/空串＝缺失；quantity 必须为 ≥1 的整数
        （DD-3.3.1 "1≤q"、本模块 LLM Schema minimum:1）——0/负数/小数等非法值
        视同缺失，交澄清链路，禁止携带进入 ROUTE/FANOUT 决策。"""
        if v is None or v == "":
            return True
        if slot == "quantity":
            return isinstance(v, bool) or not isinstance(v, int) or v < 1
        return False

    def _fanout_decision(self, dec: Decision, labels: list, slots: dict,
                         cands: list, qty: QuantityResult,
                         signals: list) -> Decision:
        """多意图 → DAG 并行扇出声明（DD-3.5.3，执行属 IT-2.08；预算 +200ms）。

        R2 整改（合入前评审）：行 3 扇出**不是豁免**——先对每个参与意图跑
        必填矩阵/1→N 实体歧义校验（行 5/行 4）；任一意图缺必填或存在未消解
        歧义 → 转 CLARIFY/options（api_calls 恒空，未校验目标不得下发），
        可执行子集与待澄清子集记入 notes。全部通过才声明扇出。
        """
        dec.layer = Layer.RULE
        dec.multi_intents = [l.intent for l in labels]
        dec.intent = labels[0].intent  # top（权重同则按目录优先级）
        dec.confidence = labels[0].confidence
        pending = {}                       # 待澄清子集：intent → 缺失槽位
        for l in labels:
            miss = self._required_missing(l.intent, slots, cands, qty)
            if miss:
                pending[l.intent] = miss
        if pending:
            dec.action = Action.CLARIFY
            dec.api_calls = []             # 硬约束：未过校验零下发
            union = []
            for miss in pending.values():
                for s in miss:
                    if s not in union:
                        union.append(s)
            dec.missing_slots = sorted(union)
            # F2 整改（终轮评审）：N3 快照清除口径在扇出 pending 分支同样生效——
            # 本轮非法/模糊数量表达命中任一参与意图必填/可带 quantity 时，
            # dec.slots 携带的快照合法值须清除（对齐 _finalize N3 分支），
            # 兑现“两路同检”承诺，不得残留 slots 随澄清结果进入下游。
            if qty.ambiguous and qty.ambiguous_phrases:
                stale = dec.slots.pop("quantity", None)
                if stale is not None:
                    dec.notes = ((dec.notes + "；") if dec.notes else "") + (
                        "本轮数量表达非法/模糊（" + "、".join(qty.ambiguous_phrases)
                        + "），快照 quantity=" + str(stale) + " 已清除并转待校验"
                        "（终轮评审 F2，对齐 N3）")
            ambiguous = len(cands) > 1 and "sku" not in slots
            if ambiguous:
                # 行 4 优先：点选澄清（options）
                dec.options = list(cands)
                dec.template = self.cfg.template("clarify_sku_ambiguous").format(
                    options="、".join(cands))
                dec.signals = self._uniq(signals + [Signal.SKU_AMBIGUOUS])
            else:
                texts = [self._slot_template(i, miss, qty)
                         for i, miss in pending.items()]
                dec.template = " ".join(
                    dict.fromkeys(t for chunk in texts for t in chunk.split()))
                dec.signals = self._uniq(signals + [Signal.SLOT_MISSING])
            ready = [i for i in dec.multi_intents if i not in pending]
            fanout_note = ("多意图扇出前置校验未通过（DD-3.1 行 3×行 4/5）→ 澄清；"
                           "可执行子集=" + ("、".join(ready) if ready else "无") +
                           "；待澄清子集=" + "、".join(pending))
            # F2：保留本分支早前写入的快照清除备注（终轮评审），不得被覆盖丢弃
            dec.notes = (dec.notes + "；" + fanout_note) if dec.notes else fanout_note
            return dec
        dec.action = Action.FANOUT
        targets = []
        for l in labels:
            for t in cat.route_targets(l.intent):
                if t not in targets:
                    targets.append(t)
        dec.api_calls = targets
        dec.notes = ("多意图 DAG 并行扇出（DD-3.1 行 3 / DD-3.5.3）；"
                     "各意图必填槽位与实体歧义校验全部通过（评审 R2）")
        return dec

    def _oos_decision(self, dec: Decision, signals: list, note: str) -> Decision:
        """行 6：领域外＝引导话术，禁止 API 调用（US-007-S4：api_calls 恒空）。"""
        if Signal.OUT_OF_SCOPE not in signals:
            signals = signals + [Signal.OUT_OF_SCOPE]
        dec.signals = signals
        dec.intent = cat.OUT_OF_SCOPE
        dec.action = Action.GUIDE
        dec.api_calls = []            # 硬约束：零 API
        dec.layer = dec.layer if dec.layer != Layer.FALLBACK else Layer.RULE
        dec.template = self.cfg.template("out_of_scope_guide")
        dec.notes = note
        return dec

    # ------------------------------------------------------------------
    def _run_small_model(self, norm: str, query: str) -> list:
        if self.small_model is None:
            return []
        scores = self.small_model.classify(query, norm) or {}
        labels = [ScoreLabel(intent=i, confidence=float(c))
                  for i, c in scores.items() if i in cat.INTENT_SET]
        labels.sort(key=lambda l: (-l.confidence, self.rule._prio(l.intent)))
        return labels

    def _is_complex_multi_turn(self, ctx: dict) -> bool:
        """『多轮复杂』信号：显式标记或轮数 ≥ complex_turn_threshold（配置化）。"""
        if ctx.get("multi_turn_complex"):
            return True
        return int(ctx.get("turn_count") or 0) >= self.cfg.complex_turn_threshold

    def _emit_llm_not_wired(self, dec: Decision) -> list:
        """独立事件 nlu.llm_not_wired（DD-11.2；评审 R5 口径：未注入≠parse_fail）。

        N4 整改（二轮评审）：抽出公共发射器——"各层均无证据且未注入 LLM"早退
        分支与 _run_llm None 分支同形状、各恰计一次（attempt=0、不产生 LLM
        尝试数/parse_fail），消除本轮基线（无 provider）最高频降级样本的观测
        盲区。返回 errors 供 dec.notes_llm_errors 调试位。
        """
        errors = ["未注入 LLM provider（本轮基线为可插拔接口）"]
        self._on_parse_fail({
            "metric": "nlu.llm_not_wired",
            "query": dec.query,
            "attempt": 0,
            "errors": list(errors),
        })
        return errors

    def _run_llm(self, norm: str, dec: Decision, small_best):
        """LLM 结构化输出：JSON 解析 + Schema 校验；失败重试 llm_retry_max → None。

        指标口径（DD-3.1 L629，评审 R5 整改）：**仅 JSON/Schema 解析失败**逐次
        计入 nlu.parse_fail；provider 未注入属装配问题（独立事件
        nlu.llm_not_wired，计一次、不计尝试数）；provider 异常/返回 None 属
        调用可用性问题（独立事件 nlu.llm_error，逐次计）——均不混入 parse_fail。
        返回 (payload, errors, attempts, parse_fail_count)。
        """
        cfg = self.cfg
        schema = build_nlu_schema()
        attempts = 0
        fails = 0                     # nlu.parse_fail 事件数（纯解析失败口径）
        errors: list = []
        payload = None
        if self.llm is None:
            errors = self._emit_llm_not_wired(dec)   # N4：与早退分支共用发射器
            dec.notes_llm_errors = errors
            return None, errors, 0, 0
        while attempts <= cfg.llm_retry_max:
            attempts += 1
            raw = None
            unavailable = False
            try:
                raw = self.llm.generate(dec.query, schema, attempts)
            except Exception as exc:  # provider 异常 → 降级链；计 llm_error 非 parse_fail（R5）
                errors = [f"provider_error: {exc}"]
                unavailable = True
            if raw is None and not unavailable:
                errors = ["provider 返回 None（调用失败）"]
                unavailable = True
            if unavailable:
                self._on_parse_fail({
                    "metric": "nlu.llm_error",
                    "query": dec.query,
                    "attempt": attempts,
                    "errors": list(errors),
                })
                if attempts > cfg.llm_retry_max:
                    break
                continue
            parse_failed = True
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError) as exc:
                errors = [f"json_parse: {exc}"]
                parsed = None
            if parsed is not None:
                errs = validate_nlu_output(parsed, schema)
                if not errs and parsed.get("intent") in cat.INTENT_SET:
                    payload = parsed
                    parse_failed = False
                    break
                errors = errs or ["intent 不在七枚举"]
            if parse_failed:
                # 纯 JSON/Schema 解析失败 → 逐次计入 nlu.parse_fail（DD-11.2）
                fails += 1
                self._on_parse_fail({
                    "metric": "nlu.parse_fail",
                    "query": dec.query,
                    "attempt": attempts,
                    "errors": list(errors),
                })
            if attempts > cfg.llm_retry_max:
                break
        dec.notes_llm_errors = errors  # 调试用（不进 trace 契约）
        return payload, errors, attempts, fails

    # ------------------------------------------------------------------
    def _finalize(self, dec: Decision, slots: dict, ctx: dict,
                  qty: QuantityResult, cands: list, signals: list,
                  ignore_llm_errors: bool = False) -> Decision:
        """路由前槽位处置：行 2（快照补全）/行 4（歧义点选）/行 5（必填矩阵澄清）。"""
        cfg = self.cfg
        intent = dec.intent
        if intent in (cat.OUT_OF_SCOPE,):
            return self._issue(self._oos_decision(dec, signals, "intent=OUT_OF_SCOPE（行 6）"))
        if intent == cat.ESCALATE_HUMAN:
            dec.action = Action.TRANSFER
            dec.api_calls = []
            dec.template = cfg.template("escalate_ack")
            dec.signals = self._uniq(signals)
            return self._issue(dec)

        dec.slots = {k: v for k, v in slots.items() if v is not None}

        # 行 4：实体歧义（1→N sku）→ clarification + options[]
        if len(cands) > 1 and "sku" not in dec.slots:
            dec.action = Action.CLARIFY
            dec.options = list(cands)
            dec.missing_slots = ["sku"]
            dec.template = cfg.template("clarify_sku_ambiguous").format(
                options="、".join(cands))
            dec.signals = self._uniq(signals + [Signal.SKU_AMBIGUOUS])
            return self._issue(dec)

        # 行 1：省略句 → 快照补全（_collect_slots 已做），仍缺必填 → 澄清
        # R6 整改：quantity 携带非法值（<1/非整数，含快照注入）视同缺失——
        # route 决策恒携带 quantity≥1（DD-3.3.1 "1≤q"、Schema minimum:1）
        required = cat.required_slots(intent)
        missing = sorted(s for s in required
                         if self._slot_missing_value(dec.slots.get(s), s))
        # 数量模糊（"一批"→触发数量澄清，DD-3.1 归一化行）：任何意图携带 quantity 皆澄清
        # N3 整改（二轮评审）：本轮命中非法/模糊数量表达（qty.ambiguous_phrases
        # 非空，含 "0台"/"3.5台" 等 invalid 形态）时 quantity 视同待校验——无论
        # 快照携带何种合法值都转澄清，并清除快照值防止携带进入 ROUTE（用户显式
        # 给出的非法表达被旧快照静默覆盖即 R6 验收"0台/3.5台不 ROUTE"的旁路）。
        if qty.ambiguous and qty.ambiguous_phrases:
            stale = dec.slots.pop("quantity", None)
            if "quantity" not in missing:
                missing.append("quantity")
            if stale is not None:
                dec.notes = ((dec.notes + "；") if dec.notes else "") + (
                    "本轮数量表达非法/模糊（" + "、".join(qty.ambiguous_phrases)
                    + "），快照 quantity=" + str(stale) + " 已清除并转待校验"
                    "（二轮评审 N3）")
        else:
            q = dec.slots.get("quantity")
            if q is not None and self._slot_missing_value(q, "quantity") \
                    and "quantity" not in missing:
                missing.append("quantity")
        if missing:
            dec.action = Action.CLARIFY
            dec.missing_slots = missing
            dec.signals = self._uniq(signals + [Signal.SLOT_MISSING])
            dec.template = self._slot_template(intent, missing, qty)
            return self._issue(dec)

        # 行 7 尾部：LLM/小模型仍低置信（≥conf_oos 但 <conf_low 且链路已到顶）→ 澄清
        if dec.layer in (Layer.LLM, Layer.SMALL) and dec.confidence < cfg.conf_low \
                and not ignore_llm_errors:
            dec.action = Action.CLARIFY
            dec.signals = self._uniq(signals + [Signal.LOW_CONFIDENCE])
            dec.template = cfg.template("clarify_generic")
            dec.notes = (dec.notes + "；仍低置信 → 澄清（行 7）").strip("；")
            return self._issue(dec)

        # 通过：只读路由声明
        dec.action = Action.ROUTE
        dec.api_calls = list(cat.route_targets(intent))
        dec.signals = self._uniq(signals)
        return self._issue(dec)

    def _slot_template(self, intent: str, missing: list, qty: QuantityResult | None = None) -> str:
        cfg = self.cfg
        parts = []
        for slot in missing:
            if slot == "quantity" and qty is not None and qty.ambiguous:
                # 模糊量词（"一批"）/非法表达（"0台"）→ 专用数量澄清话术
                # （DD-3.1 归一化行；N3 后含快照覆盖场景）
                parts.append(cfg.template("clarify_quantity_ambiguous").format(
                    phrase="、".join(qty.ambiguous_phrases) or "您说的数量"))
                continue
            t = cfg.slot_clarify(intent, slot)
            if not t:
                t = cfg.template("clarify_generic")
            parts.append(t)
        return " ".join(dict.fromkeys(parts))  # 去重保序

    def _issue(self, dec: Decision) -> Decision:
        """经 sink 记录路由声明（真实调用属 IT-2.08）；GUIDE/CLARIFY/TRANSFER 恒不记录。"""
        if self._on_api_call is not None:
            for target in dec.api_calls:
                self._on_api_call(target)
        return dec

    @staticmethod
    def _uniq(seq: list) -> list:
        return list(dict.fromkeys(seq))
