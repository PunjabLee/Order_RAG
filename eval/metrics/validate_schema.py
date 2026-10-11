#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_schema.py — IT-1.11 评测集结构校验（零依赖，Python 3.9+）

校验范围（对应任务卡要求）：
  1) 字段齐全：13 个必填字段 + 类型/枚举合法（intent 七枚举、expected_behavior、identity、source）
  2) 金额：所有 *_cents 必须为 int（禁止 float/str）；breakdown 求和 == total_cents（0 容差）；
     标记 recompute="DD-8.1" 的 fact 独立复算（先折后减 + 满减门槛判定）；DD-8.1 锚点 8049700 强断言
  3) B5 行为约束：expected_behavior ∈ {refuse, escalate} 且 no_unauthorized_api=true 且 threat_ref 合法
  4) 门禁辅助：双签红线（reviewed_by 必须为 null、禁止"已双签"）、must_hit ≤ 5（Recall@5）、
     过期陷阱样本必须带 forbidden_docs、PII 明文检测、假设值 pending_question 覆盖、
     B3/B4 交期端点按 DD-8.4 工作日规则复算

用法：
  python3 eval/metrics/validate_schema.py                 # 默认校验 eval/golden-set-v1
  python3 eval/metrics/validate_schema.py --strict-fill   # IT-2.05 前回填门禁：仍为假设值即 FAIL
退出码：0 = 无 FAIL（WARN 允许存在）；1 = 存在 FAIL；2 = 无法读取文件
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter

# ---------------- 常量（与设计文档/夹具对齐，勿在业务代码中硬编码） ----------------
REQUIRED_FIELDS = [
    "id", "batch", "query", "intent", "turn_context", "expected_facts", "expected_docs",
    "expected_behavior", "acl_context", "effective_context", "source", "annotated_by", "reviewed_by",
]
INTENTS = {"PRICE_QUERY", "PROMO_QUERY", "DELIVERY_QUERY", "ORDER_CREATE", "ORDER_QUERY",
           "OUT_OF_SCOPE", "ESCALATE_HUMAN"}
BEHAVIORS = {"answer", "clarify", "refuse", "escalate"}
IDENTITIES = {"guest", "individual_verified", "enterprise_verified", "enterprise_pending", "enterprise_expired"}
SOURCES = {"手写", "线上脱敏采样"}
FACT_SOURCES = {"product_service", "promo_service", "logi_api", "order_service", "user_service",
           "session_service", "kb"}   # session_service = DD-3.2.1 会话服务（SESS，槽位快照/升级触发）
DOC_TYPES = {"policy", "product", "faq", "logistics"}
THREATS = {"T-PI-1", "T-PI-2", "T-PI-3", "T-TENANT", "T-ABUSE", "T-TOOL", "T-PII"}
ID_RE = re.compile(r"^B[1-5]-[0-9]{3}$")
MASKED_PHONE_RE = re.compile(r"^1[3-9]\d\*{4}\d{4}$")
RAW_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
RAW_IDCARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
CENTS_KEY_RE = re.compile(r"cents$")

EXPECTED_COUNTS = {"B1": 50, "B2": 60, "B3": 30, "B4": 30, "B5": 30}
FILES = {
    "B1": "b1_price.jsonl", "B2": "b2_policy.jsonl", "B3": "b3_delivery.jsonl",
    "B4": "b4_context.jsonl", "B5": "b5_adversarial.jsonl",
}
TRAP_TAG = "过期陷阱"
TRAP_MIN = {"B2": 15}
ANCHOR_TOTAL_CENTS = 8049700          # DD-8.1 权威声明值
ANCHOR_UNIT, ANCHOR_QTY = 2999900, 3
DD81_TOLERANCE_CENTS = 100            # 非锚点场景仅允许因取整产生的 ≤1 元尾差（且须 Q7）
BATCHES_REQUIRING_ASSUMPTION_TAG = {"B1", "B2", "B3", "B4"}
FIX_PRICING_VERSION = "pv_20261011_0930"      # 夹具中的假设价格/政策快照版本
# 引用夹具假设值的事实类型（金额/交期/政策/限购/库存…）；纯拒绝-澄清类守卫事实不在此列
ASSUMPTION_DEPENDENT_FACT_TYPES = {
    "pricing_breakdown", "pricing_breakdown_cart", "sku_price", "pricing_quote", "candidate_option",
    "delivery_estimate", "stock_snapshot", "policy_status", "coupon_status", "stack_matrix",
    "slot_check", "limit_check", "freight_snapshot", "draft_state", "invoice_rule",
    "warranty_snapshot", "coverage_check", "warehouse_offset", "recommendation", "order_snapshot",
}


class Report:
    def __init__(self) -> None:
        self.fail: list[tuple[str, str, str]] = []
        self.warn: list[tuple[str, str, str]] = []
        self.stats: dict[str, dict[str, int]] = {}

    def F(self, sid: str, code: str, msg: str) -> None:
        self.fail.append((sid, code, msg))

    def W(self, sid: str, code: str, msg: str) -> None:
        self.warn.append((sid, code, msg))


# ---------------- 金额复算 ----------------
def iter_cents(node, path=""):
    """递归收集所有 *_cents 字段（含 breakdown[].cents / lines[] 内嵌）。"""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if CENTS_KEY_RE.search(k):
                out.append((p, v))
            out.extend(iter_cents(v, p))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(iter_cents(v, f"{path}[{i}]"))
    return out


def recompute_dd81(fact, policy, sample):
    """返回 (declared, math_total, note) 或 None（不可复算）。"""
    unit, qty = fact.get("unit_price_cents"), fact.get("quantity")
    if not isinstance(unit, int) or not isinstance(qty, int):
        return None
    bp = policy["params"]
    list_price = unit * qty
    bulk = 0
    if fact.get("apply_bulk"):
        rate = bp["rate"]
        bulk = -int(round(list_price * (1 - rate)))
    after_discount = list_price + bulk
    threshold = 0
    if fact.get("apply_threshold"):
        threshold = bp_threshold(policy) if after_discount >= bp["threshold_cents"] else 0
    math_total = after_discount + threshold
    return list_price, bulk, after_discount, threshold, math_total


def bp_threshold(policy):
    return policy["params"]["amount_cents"]


# ---------------- 交期复算（DD-8.4） ----------------
def is_workday(d: dt.date) -> bool:
    return d.weekday() < 5


def next_workday(d: dt.date) -> dt.date:
    d = d + dt.timedelta(days=1)
    while not is_workday(d):
        d += dt.timedelta(days=1)
    return d


def add_workdays(d: dt.date, n: int) -> dt.date:
    for _ in range(n):
        d = next_workday(d)
    return d


def parse_local(now_iso: str) -> dt.datetime:
    s = now_iso.replace("Z", "")
    return dt.datetime.fromisoformat(s)


def compute_delivery(now_iso, cutoff, cutoff_passed_expected, offsets, anchor_expected):
    local = parse_local(now_iso)
    cut_h, cut_m = [int(x) for x in cutoff.split(":")]
    cutoff_dt = local.replace(hour=cut_h, minute=cut_m, second=0, microsecond=0)
    passed = local > cutoff_dt                       # inclusive 边界：等于视为未过
    d = local.date() if not passed else next_workday(local.date())
    return passed, d, add_workdays(d, 3), add_workdays(d, 5)


def depends_on_fixture(s) -> bool:
    """样本是否引用了夹具中的假设值（金额 / 日期端点 / 政策快照 / 限购）。"""
    facts = s.get("expected_facts") or []
    types = {f.get("type") for f in facts if isinstance(f, dict)}
    if types & ASSUMPTION_DEPENDENT_FACT_TYPES:
        return True
    if any(isinstance(v, int) and not isinstance(v, bool) for _, v in iter_cents(facts)):
        return True
    if any(isinstance(f, dict) and any(k.endswith("_date") for k in f) for f in facts):
        return True
    for d in s.get("expected_docs") or []:
        if isinstance(d, dict) and (d.get("doc_type") == "policy" or d.get("policy_version") == FIX_PRICING_VERSION):
            return True
    for d in s.get("forbidden_docs") or []:
        if isinstance(d, dict) and d.get("policy_version"):
            return True
    return False


# ---------------- 主校验 ----------------
def load_fixture(path):
    with open(path, encoding="utf-8") as f:
        fx = json.load(f)
    policies = {p["policy_id"]: p for p in fx.get("policies", [])}
    users = {u["user_id"]: u for u in fx.get("users", [])}
    addrs = {a["address_id"]: a for a in fx.get("addresses", [])}
    policies["PRICING_RULES"] = {"params": {**policies["BULK_ENTERPRISE"]["params"], **policies["THRESHOLD_500"]["params"]}}
    return fx, policies, users, addrs


def check_sample(s, batch, fx_policies, fx_users, fx_addrs, rep, strict_fill):
    sid = s.get("id", "<no-id>")

    # --- 1 必填字段 / 类型 ---
    missing = [k for k in REQUIRED_FIELDS if k not in s]
    if missing:
        rep.F(sid, "SCHEMA_MISSING", f"缺必填字段: {','.join(missing)}")
    if not isinstance(s.get("turn_context", []), list):
        rep.F(sid, "SCHEMA_TYPE", "turn_context 必须为数组")
    if not isinstance(s.get("expected_facts", []), list):
        rep.F(sid, "SCHEMA_TYPE", "expected_facts 必须为数组")
    if not isinstance(s.get("expected_docs", []), list):
        rep.F(sid, "SCHEMA_TYPE", "expected_docs 必须为数组")
    if not isinstance(s.get("acl_context"), dict):
        rep.F(sid, "SCHEMA_TYPE", "acl_context 必须为对象")
    if not isinstance(s.get("effective_context"), dict):
        rep.F(sid, "SCHEMA_TYPE", "effective_context 必须为对象")

    # --- 2 id / batch 一致 ---
    if not ID_RE.match(str(sid)):
        rep.F(sid, "ID_FORMAT", f"id 不符合 ^B[1-5]-[0-9]{{3}}$: {sid}")
    if s.get("batch") != batch:
        rep.F(sid, "BATCH_MISMATCH", f"batch={s.get('batch')} 与所在文件 {batch} 不符")
    elif not str(sid).startswith(batch + "-"):
        rep.F(sid, "BATCH_MISMATCH", "id 前缀与 batch 不一致")

    # --- 3 query 口语化 + PII ---
    q = s.get("query", "")
    if not isinstance(q, str) or not q.strip():
        rep.F(sid, "QUERY_EMPTY", "query 为空或非字符串")
    elif len(q.strip()) < 2 and "超短输入" not in (s.get("tags") or []):
        rep.W(sid, "QUERY_SHORT", f"query 极短（{q!r}），仅『超短输入』标签样本可豁免")
    if not re.search(r"[\u4e00-\u9fff]", str(q)):
        rep.W(sid, "QUERY_NOT_CN", f"query 非中文: {q!r}")
    blob = json.dumps({"q": q, "t": s.get("turn_context"), "n": s.get("notes"),
                       "sn": s.get("source_note")}, ensure_ascii=False)
    hits = RAW_PHONE_RE.findall(blob) + RAW_IDCARD_RE.findall(blob)
    if hits:
        rep.F(sid, "PII_RAW", f"疑似未脱敏 PII（{len(hits)} 处），须改为 138****0000 形式")

    # --- 4 枚举 ---
    if s.get("intent") not in INTENTS:
        rep.F(sid, "INTENT_ENUM", f"intent 非法: {s.get('intent')}（须属 DD-3.1 七枚举）")
    if s.get("expected_behavior") not in BEHAVIORS:
        rep.F(sid, "BEHAVIOR_ENUM", f"expected_behavior 非法: {s.get('expected_behavior')}")
    if s.get("source") not in SOURCES:
        rep.F(sid, "SOURCE_ENUM", f"source 非法: {s.get('source')}")
    if s.get("source") == "线上脱敏采样" and not s.get("source_note"):
        rep.W(sid, "SOURCE_NOTE", "线上脱敏采样未附 source_note（脱敏口径可追溯）")
    acl = s.get("acl_context") or {}
    if acl.get("identity") not in IDENTITIES:
        rep.F(sid, "IDENTITY_ENUM", f"identity 非法: {acl.get('identity')}")
    if acl.get("address_scope") not in {"self"}:
        rep.W(sid, "ACL_SCOPE", "address_scope 应为 self（越权由断言拒绝）")

    # --- 5 双签红线 ---
    if s.get("reviewed_by") is not None:
        rep.F(sid, "DOUBLE_SIGN_FORBIDDEN", "v0 阶段 reviewed_by 必须为 null（禁止标为已双签）")
    if "已双签" in str(s.get("review_status", "")):
        rep.F(sid, "DOUBLE_SIGN_FORBIDDEN", "review_status 不得提前置为已双签通过")
    if not s.get("annotated_by"):
        rep.F(sid, "ANNOTATOR_MISSING", "annotated_by 必填")

    # --- 6 检索基线 / 文档 ---
    docs = s.get("expected_docs") or []
    must_hit = [d for d in docs if isinstance(d, dict) and d.get("must_hit") is True]
    if len(must_hit) > 5:
        rep.F(sid, "RECALL_AT_5", f"must_hit=true 共 {len(must_hit)} 条 > 5，违反 Recall@5 可达成性")
    for d in docs:
        if not isinstance(d, dict) or "doc_id" not in d:
            rep.F(sid, "DOC_SCHEMA", "expected_docs 元素缺 doc_id")
            continue
        if d.get("doc_type") not in DOC_TYPES:
            rep.F(sid, "DOC_TYPE_ENUM", f"doc_type 非法: {d.get('doc_type')}")
        if d.get("doc_type") == "policy" and not d.get("policy_version"):
            rep.W(sid, "DOC_VERSION_NULL", f"政策文档缺 policy_version: {d.get('doc_id')}")
    fb = s.get("forbidden_docs") or []
    for d in fb:
        if not isinstance(d, dict) or not d.get("doc_id") or not d.get("reason"):
            rep.F(sid, "FORBIDDEN_DOC_SCHEMA", "forbidden_docs 元素须含 doc_id + reason")

    # --- 7 金额：整数分 + breakdown 自洽 + DD-8.1 复算 ---
    for fact in s.get("expected_facts") or []:
        if not isinstance(fact, dict):
            rep.F(sid, "FACT_SCHEMA", "expected_facts 元素必须为对象")
            continue
        if fact.get("source") not in FACT_SOURCES:
            rep.F(sid, "FACT_SOURCE_ENUM", f"fact.source 非法: {fact.get('source')}")
        if not fact.get("as_of"):
            rep.W(sid, "FACT_AS_OF", f"type={fact.get('type')} 缺 as_of")
        null_ok = bool(fact.get("note") or fact.get("reason") or fact.get("null_reason")
                       or fact.get("applicable") is False)
        for path, val in iter_cents(fact):
            if val is None:
                if not null_ok:
                    rep.F(sid, "MONEY_NULL", f"{path} 为 null 但无 note/reason/applicable=false 说明（期望\"无金额\"须给依据）")
            elif isinstance(val, bool) or not isinstance(val, int):
                rep.F(sid, "MONEY_NOT_INT", f"{path} 必须为整数分，实为 {type(val).__name__}: {val!r}")
        bd = fact.get("breakdown")
        if isinstance(bd, list) and bd:
            tot = sum(x.get("cents", 0) for x in bd if isinstance(x, dict))
            if tot != fact.get("total_cents"):
                rep.F(sid, "BREAKDOWN_SUM", f"breakdown 之和 {tot} != total_cents {fact.get('total_cents')}")
        if fact.get("recompute") == "DD-8.1" and fact.get("type") == "pricing_breakdown":
            res = recompute_dd81(fact, fx_policies["PRICING_RULES"], s)
            if res is None:
                rep.F(sid, "RECOMPUTE_INPUT", "缺 unit_price_cents/quantity，无法复算")
            else:
                list_price, bulk, after, th, math_total = res
                declared = fact.get("total_cents")
                is_anchor = (fact.get("unit_price_cents") == ANCHOR_UNIT and fact.get("quantity") == ANCHOR_QTY
                             and fact.get("apply_bulk") and fact.get("apply_threshold"))
                if is_anchor:
                    if declared != ANCHOR_TOTAL_CENTS:
                        rep.F(sid, "DD81_ANCHOR", f"DD-8.1 锚点场景 total 必须为 {ANCHOR_TOTAL_CENTS}，实为 {declared}")
                    if math_total != ANCHOR_TOTAL_CENTS:
                        rep.W(sid, "DD81_TAIL", f"锚点严格数学值 {math_total} 与声明值差 {declared - math_total} 分（Q7 待回填）")
                elif declared != math_total:
                    delta = declared - math_total
                    if 0 < abs(delta) <= DD81_TOLERANCE_CENTS and s.get("pending_question") == "Q7":
                        rep.W(sid, "MONEY_TAIL", f"declared {declared} 与复算 {math_total} 差 {delta} 分（Q7 尾差）")
                    else:
                        rep.F(sid, "MONEY_RECOMPUTE", f"declared {declared} != 复算 {math_total}（差 {delta} 分）")
                # 明细项一致性：DD-8.1 声明值与严格数学值差 ≤1 元且带 Q7 → WARN（fixtures/README §7 尾差裁决）
                exp_map = {"list_price": list_price, "bulk_discount_10pct": bulk,
                           "threshold_minus_500": th, "after_discount": after}
                for item in bd or []:
                    it, cv = item.get("item"), item.get("cents")
                    exp = exp_map.get(it)
                    if exp is None or cv == exp:
                        continue
                    delta = (cv or 0) - exp
                    if abs(delta) <= DD81_TOLERANCE_CENTS and s.get("pending_question") == "Q7":
                        rep.W(sid, "DD81_TAIL", f"明细项 {it} 声明 {cv} 与复算 {exp} 差 {delta} 分（DD-8.1 取整尾差，Q7 待回填）")
                    else:
                        rep.F(sid, "BREAKDOWN_ITEM", f"明细项 {it} 声明 {cv} != 复算 {exp}（差 {delta} 分）")
                # 门槛判定一致性
                thres_cents = fx_policies["THRESHOLD_500"]["params"]["threshold_cents"]
                if fact.get("apply_threshold") and after < thres_cents:
                    rep.F(sid, "THRESHOLD_MISUSE", f"折后 {after} < 门槛 {thres_cents} 却套用满减")
        if fact.get("recompute") == "DD-8.1-cart":
            lines = fact.get("lines") or []
            if not lines:
                rep.F(sid, "CART_LINES", "recompute=DD-8.1-cart 必须给出 lines[]（跨 SKU 逐行单价×数量）")
            else:
                tot = 0
                for l in lines:
                    u, qty, lc = l.get("unit_price_cents"), l.get("quantity"), l.get("line_cents")
                    if not isinstance(u, int) or not isinstance(qty, int) or not isinstance(lc, int):
                        rep.F(sid, "CART_LINE", f"行字段须为整数分: {l.get('sku_id')}")
                        continue
                    if u * qty != lc:
                        rep.F(sid, "CART_LINE", f"{l.get('sku_id')} {u}×{qty}={u * qty} != line_cents {lc}")
                    tot += lc
                list_items = sum(i.get("cents", 0) for i in (bd or [])
                                 if str(i.get("item", "")).startswith("list_price"))
                if list_items and list_items != tot:
                    rep.F(sid, "CART_LIST", f"list_price 明细合计 {list_items} != 行合计 {tot}")
                if fact.get("apply_bulk"):
                    rep.F(sid, "CART_BULK", "跨 SKU 草稿不得叠加 BULK_ENTERPRISE（假设 A2，fixtures/README §3）")
                th_amt = 0
                if fact.get("apply_threshold"):
                    tc = fx_policies["THRESHOLD_500"]["params"]
                    if tot < tc["threshold_cents"]:
                        rep.F(sid, "THRESHOLD_MISUSE", f"折后合计 {tot} < 门槛 {tc['threshold_cents']} 却套用满减")
                    else:
                        th_amt = tc["amount_cents"]
                exp_total = tot + th_amt
                if fact.get("total_cents") != exp_total:
                    rep.F(sid, "CART_RECOMPUTE", f"购物车 declared {fact.get('total_cents')} != 复算 {exp_total}")

    # --- 8 交期端点复算 ---
    eff = s.get("effective_context") or {}
    for fact in s.get("expected_facts") or []:
        if isinstance(fact, dict) and fact.get("type") == "delivery_estimate" \
                and fact.get("working_days_only") is True and not fact.get("preorder_lead_days"):
            now_iso = eff.get("now")
            if not now_iso:
                rep.F(sid, "EFFECTIVE_NOW", "交期样本缺 effective_context.now")
                continue
            try:
                passed, anchor, e3, e5 = compute_delivery(
                    now_iso, eff.get("cutoff_time", "15:00"), None, None, None)
            except Exception as exc:  # noqa: BLE001
                rep.F(sid, "DATE_PARSE", f"时间解析失败: {exc}")
                continue
            off = int(fact.get("offset_working_days") or 0)
            earliest = add_workdays(e3, off) if off else e3
            latest = add_workdays(e5, off) if off else e5
            if fact.get("cutoff_passed") != passed:
                rep.F(sid, "CUTOFF_FLAG", f"cutoff_passed={fact.get('cutoff_passed')} 复算为 {passed}")
            if str(fact.get("order_anchor_date")) != anchor.isoformat():
                rep.F(sid, "ANCHOR_DATE", f"order_anchor_date={fact.get('order_anchor_date')} 复算为 {anchor}")
            if str(fact.get("earliest_date")) != earliest.isoformat():
                rep.F(sid, "EARLIEST_DATE", f"earliest {fact.get('earliest_date')} 复算为 {earliest}")
            if str(fact.get("latest_date")) != latest.isoformat():
                rep.F(sid, "LATEST_DATE", f"latest {fact.get('latest_date')} 复算为 {latest}")

    # --- 9 B5 行为约束 ---
    if batch == "B5":
        if s.get("expected_behavior") not in {"refuse", "escalate"}:
            rep.F(sid, "B5_BEHAVIOR", f"B5 样本 expected_behavior 必须为 refuse/escalate，实为 {s.get('expected_behavior')}")
        if s.get("no_unauthorized_api") is not True:
            rep.F(sid, "B5_NO_API", "B5 样本必须 no_unauthorized_api=true（期望无未授权 API 调用）")
        if s.get("threat_ref") not in THREATS:
            rep.F(sid, "B5_THREAT_REF", f"threat_ref 非法或缺失: {s.get('threat_ref')}")
        joined = json.dumps(s.get("assertions") or [], ensure_ascii=False)
        if "未授权API" not in joined and "未授权 API" not in joined:
            rep.F(sid, "B5_ASSERT", "B5 断言须显式写明\"不得触发未授权API\"")
        if s.get("expected_facts"):
            for fact in s["expected_facts"]:
                if isinstance(fact, dict) and fact.get("type") in {"pricing_breakdown"} and fact.get("apply_bulk") is True \
                        and "8折" in joined and fact.get("total_cents") != ANCHOR_TOTAL_CENTS:
                    rep.W(sid, "B5_FACT", "注入类样本携带折扣事实需复核未被改写")

    # --- 10 陷阱样本 / 假设值覆盖 ---
    tags = s.get("tags") or []
    if TRAP_TAG in tags and not fb:
        rep.F(sid, "TRAP_NO_FORBIDDEN", "过期陷阱样本必须列出 forbidden_docs（pre-filter 断言）")
    if batch in BATCHES_REQUIRING_ASSUMPTION_TAG and not s.get("pending_question") and depends_on_fixture(s):
        rep.F(sid, "ASSUMPTION_UNTAGGED", "引用假设夹具（金额/交期/政策版本/限购）的样本必须带 pending_question（Q7/Q3/Q6）")
    if strict_fill and s.get("pending_question"):
        rep.F(sid, "STRICT_FILL", f"仍为假设值（{s.get('pending_question')}），IT-2.05 前须回填")

    # --- 11 身份/地址与夹具一致（交叉校验） ---
    uid = acl.get("user_id")
    if uid in fx_users:
        ref = fx_users[uid]
        if acl.get("identity") != ref["identity"]:
            rep.F(sid, "IDENTITY_CONFLICT", f"user {uid} 夹具身份 {ref['identity']} != 样本 {acl.get('identity')}")
        if ref.get("tenant_id") and acl.get("tenant_id") != ref.get("tenant_id"):
            rep.F(sid, "TENANT_CONFLICT", f"user {uid} 属 {ref.get('tenant_id')}，样本 acl.tenant_id={acl.get('tenant_id')}")
    for fact in s.get("expected_facts") or []:
        aid = fact.get("address_id") if isinstance(fact, dict) else None
        if aid in fx_addrs and not fx_addrs[aid].get("deliverable", True):
            if any(k in fact for k in ("earliest_date", "latest_date")):
                rep.F(sid, "UNREACHABLE_HAS_DATE", "不可达地址不得生成交期端点")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="eval/golden-set-v1")
    ap.add_argument("--fixture", default="eval/fixtures/promotion.json")
    ap.add_argument("--strict-fill", action="store_true")
    ap.add_argument("--max-print", type=int, default=40)
    args = ap.parse_args()

    root = os.getcwd()
    dpath = os.path.join(root, args.dir)
    fpath = os.path.join(root, args.fixture)
    if not os.path.isdir(dpath) or not os.path.isfile(fpath):
        print(f"[ERR] 路径不存在: {dpath} / {fpath}", file=sys.stderr)
        return 2

    fx, fx_policies, fx_users, fx_addrs = load_fixture(fpath)
    rep = Report()
    seen_ids: set[str] = set()
    per_batch: dict[str, dict] = {}

    for batch, fname in FILES.items():
        fp = os.path.join(dpath, fname)
        rows, bad_json = [], 0
        if not os.path.isfile(fp):
            rep.F(batch, "FILE_MISSING", f"{fname} 不存在")
            continue
        with open(fp, encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    rep.F(f"{fname}:{ln}", "JSON_PARSE", str(e))
                    bad_json += 1
                    continue
                rows.append(obj)
        if bad_json:
            rep.F(batch, "JSON_LINES", f"{fname} 有 {bad_json} 行 JSON 解析失败")
        counts = Counter(r.get("id", "") for r in rows)
        dups = [k for k, v in counts.items() if v > 1]
        if dups:
            rep.F(batch, "DUP_ID", f"重复 id: {dups}")
        seen_ids.update(counts)
        for r in rows:
            check_sample(r, batch, fx_policies, fx_users, fx_addrs, rep, args.strict_fill)
        traps = sum(1 for r in rows if TRAP_TAG in (r.get("tags") or []))
        per_batch[batch] = {"file": fname, "count": len(rows), "traps": traps}
        exp = EXPECTED_COUNTS[batch]
        if len(rows) != exp:
            rep.F(batch, "BATCH_COUNT", f"{fname} 条数 {len(rows)} != 要求 {exp}")
        min_trap = TRAP_MIN.get(batch)
        if min_trap and traps < min_trap:
            rep.F(batch, "TRAP_COUNT", f"{batch} 过期陷阱样本 {traps} < {min_trap}")

    # ---------- 输出 ----------
    print("=" * 78)
    print("IT-1.11 评测集结构校验  (validate_schema.py)" + ("   [--strict-fill]" if args.strict_fill else ""))
    print("=" * 78)
    total = sum(v["count"] for v in per_batch.values())
    print(f"{'批次':<6}{'文件':<22}{'条数':>6}{'期望':>6}{'陷阱':>6}  FAIL  WARN")
    for b in ["B1", "B2", "B3", "B4", "B5"]:
        v = per_batch.get(b, {"file": FILES[b], "count": 0, "traps": 0})
        f_ = sum(1 for x in rep.fail if x[0].startswith(b) or x[0] == b)
        w_ = sum(1 for x in rep.warn if x[0].startswith(b) or x[0] == b)
        print(f"{b:<6}{v['file']:<22}{v['count']:>6}{EXPECTED_COUNTS[b]:>6}{v['traps']:>6}  {f_:>4}  {w_:>4}")
    nf, nw = len(rep.fail), len(rep.warn)
    print("-" * 78)
    print(f"合计样本: {total} 条 | FAIL: {nf} | WARN: {nw}")
    if rep.fail:
        print("\n[FAIL 明细]")
        code_cnt = Counter(c for _, c, _ in rep.fail)
        for sid, code, msg in rep.fail[: args.max_print]:
            print(f"  - {sid} [{code}] {msg}")
        if nf > args.max_print:
            print(f"  ... 其余 {nf - args.max_print} 条略")
        print("  按错误码汇总: " + ", ".join(f"{k}×{v}" for k, v in sorted(code_cnt.items())))
    if rep.warn:
        print("\n[WARN 明细（前若干）]")
        for sid, code, msg in rep.warn[: args.max_print]:
            print(f"  - {sid} [{code}] {msg}")
        if nw > args.max_print:
            print(f"  ... 其余 {nw - args.max_print} 条略")
        print("  按告警码汇总: " + ", ".join(f"{k}×{v}" for k, v in sorted(Counter(c for _, c, _ in rep.warn).items())))
    print("\n结论:", "❌ 未通过（存在 FAIL，禁止进入 CI 门禁 / 禁止标为 v1 正式集）" if nf else
          ("✅ 结构校验通过（WARN 为待回填尾差，Q3/Q6/Q7 闭环后需复跑）") or "")
    return 1 if nf else 0


if __name__ == "__main__":
    sys.exit(main())
