# -*- coding: utf-8 -*-
"""DD-3.1 意图目录 —— 七枚举唯一单源（整改 H-9/M-2 术语统一）。

**契约红线**：本枚举与 eval/metrics/validate_schema.py::INTENTS、
eval/ANNOTATION_GUIDE.md §2 `intent` 行完全一致；任何增/删/改名必须走
《研发迭代计划.md》§7 变更控制专道（IT-2.02 仅对齐，不变更）。

路由目标（只读，ADR-001 白名单口径）仅为**声明**：本模块零网络，
实际调用由 IT-2.08 业务 API 调用器执行；OUT_OF_SCOPE / ESCALATE_HUMAN
的路由目标为空——领域外**禁止 API 调用**（DD-3.1 决策表行 6，US-007-S4）。
"""
from __future__ import annotations

# ------- 七枚举（DD-3.1 意图目录表，顺序与文档一致） -------
PRICE_QUERY = "PRICE_QUERY"
PROMO_QUERY = "PROMO_QUERY"
DELIVERY_QUERY = "DELIVERY_QUERY"
ORDER_CREATE = "ORDER_CREATE"
ORDER_QUERY = "ORDER_QUERY"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
ESCALATE_HUMAN = "ESCALATE_HUMAN"

INTENTS = (
    PRICE_QUERY,
    PROMO_QUERY,
    DELIVERY_QUERY,
    ORDER_CREATE,
    ORDER_QUERY,
    OUT_OF_SCOPE,
    ESCALATE_HUMAN,
)
INTENT_SET = frozenset(INTENTS)

#: 意图名不含金额/价格数值——本模块禁止出现任何金额值（DD-16 R-07；
#: 金额权威方＝促销服务 DD-8.2），tests 有守卫用例。

# ------- 路由目标（只读）——DD-3.1 表"路由目标"列 -------
ROUTE_TARGETS = {
    PRICE_QUERY: ("product_service", "kb"),
    PROMO_QUERY: ("promo_service", "policy_kb"),
    DELIVERY_QUERY: ("logistics_service", "rule_kb"),
    # 草稿组装经应用服务（DD-2.2 BIZ→DRAFT），RAG 侧无写权限（ADR-001）
    ORDER_CREATE: ("draft_assembly",),
    ORDER_QUERY: ("order_service",),          # 只读
    OUT_OF_SCOPE: (),                          # 引导话术，不调 API（US-007-S4）
    ESCALATE_HUMAN: ("escalate_service",),     # 转接服务（US-009，非业务只读 API）
}

# ------- 意图 × 槽位必填矩阵（DD-3.3.2，●必填 ○可选 ✕不问） -------
# 槽位命名严格 DD-3.3.1：product/sku/quantity/address_id/coupon_id/delivery_date
# ORDER_QUERY 的 order_id 允许"可列表"（不指名单号时可拉列表，DD-3.1 行 5）。
ALL_SLOTS = ("product", "sku", "quantity", "address_id", "coupon_id", "delivery_date")

SLOT_MATRIX = {
    PRICE_QUERY: {
        "required": {"product"},
        # sku ○（可歧义澄清）、quantity ○（默认 1 并告知）
        "optional": {"sku", "quantity"},
        "forbidden": {"address_id", "coupon_id", "delivery_date"},
    },
    PROMO_QUERY: {
        # product ○（可由 sku 推出）
        "required": {"sku", "quantity"},
        "optional": {"product", "coupon_id", "delivery_date"},
        "forbidden": {"address_id"},
    },
    DELIVERY_QUERY: {
        "required": {"sku", "quantity", "address_id"},
        "optional": {"product", "delivery_date"},
        "forbidden": {"coupon_id"},
    },
    ORDER_CREATE: {
        "required": {"sku", "quantity", "address_id"},
        "optional": {"product", "coupon_id", "delivery_date"},
        "forbidden": set(),
    },
    ORDER_QUERY: {
        "required": {"order_id"},
        "optional": set(),
        "forbidden": set(),
        "order_id_allow_list": True,   # "可列表"语义：无单号→列表查询
    },
    OUT_OF_SCOPE: {"required": set(), "optional": set(), "forbidden": set()},
    ESCALATE_HUMAN: {"required": set(), "optional": set(), "forbidden": set()},
}


def _spec(intent: str) -> dict:
    if intent not in INTENT_SET:
        raise ValueError(f"未知意图 {intent!r}：必须属 DD-3.1 七枚举 {INTENTS}")
    return SLOT_MATRIX[intent]


def route_targets(intent: str) -> tuple:
    """只读路由目标声明；OUT_OF_SCOPE 恒为空元组（US-007-S4）。"""
    _spec(intent)
    return ROUTE_TARGETS[intent]


def required_slots(intent: str) -> frozenset:
    return frozenset(_spec(intent)["required"])


def optional_slots(intent: str) -> frozenset:
    return frozenset(_spec(intent)["optional"])


def forbidden_slots(intent: str) -> frozenset:
    return frozenset(_spec(intent)["forbidden"])


def slots_of(intent: str) -> frozenset:
    """该意图可携带的全部槽位（必填∪可选∪order_id）。"""
    s = _spec(intent)
    return frozenset(s["required"] | s["optional"] | s.get("extras", set()))
