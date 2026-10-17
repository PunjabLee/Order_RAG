# -*- coding: utf-8 -*-
"""LLM 结构化输出契约（DD-3.1）：JSON Schema 构造 + 标准库最小校验器。

契约要点（替代旧"示例 JSON"）：
- `intent` 枚举受限（＝意图目录七枚举，单源注入）；
- `entities` 类型受限（槽位名引用 DD-3.3.1，值类型受限）；
- `missing_slots` 引用意图目录（枚举受限）；
- `confidence` ∈ [0,1]；
- `additionalProperties: false`（多余字段即非法）。

本校验器只覆盖上述所需子集（type/enum/required/properties/
additionalProperties/items/minimum/maximum），不引入 jsonschema 三方依赖。
"""
from __future__ import annotations

from src.nlu.intents import INTENTS, SLOT_MATRIX, ALL_SLOTS

# LLM 输出允许出现的实体键（DD-3.3.1 槽位 + ORDER_QUERY 的 order_id）
ENTITY_SLOTS = tuple(ALL_SLOTS) + ("order_id",)


def build_nlu_schema() -> dict:
    """查询理解 LLM 结构化输出的 JSON Schema（response_format=json_schema 用）。"""
    entity_props = {
        "product": {"type": "string"},
        "sku": {"type": "string"},
        "quantity": {"type": "integer", "minimum": 1},
        "address_id": {"type": "string"},
        "coupon_id": {"type": "string"},
        "delivery_date": {"type": "string"},
        "order_id": {"type": "string"},
    }
    return {
        "$schema_note": "DD-3.1 LLM 结构化输出契约（IT-2.02）",
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": list(INTENTS)},
            "entities": {
                "type": "object",
                "properties": entity_props,
                "additionalProperties": False,
            },
            "missing_slots": {
                "type": "array",
                "items": {"type": "string", "enum": list(ENTITY_SLOTS)},
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["intent", "confidence"],
        "additionalProperties": False,
    }


def validate_json_schema(node, schema: dict, path: str = "$") -> list:
    """最小子集校验，返回错误列表（空＝通过）。"""
    errors: list = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(node, dict):
            return [f"{path}: 应为 object，实为 {type(node).__name__}"]
        props = schema.get("properties", {})
        for rq in schema.get("required", []):
            if rq not in node:
                errors.append(f"{path}: 缺必填字段 {rq}")
        if schema.get("additionalProperties") is False:
            extra = [k for k in node if k not in props]
            if extra:
                errors.append(f"{path}: 出现未定义字段 {extra}（additionalProperties=false）")
        for k, v in node.items():
            if k in props:
                errors.extend(validate_json_schema(v, props[k], f"{path}.{k}"))
    elif t == "array":
        if not isinstance(node, list):
            return [f"{path}: 应为 array，实为 {type(node).__name__}"]
        items = schema.get("items")
        if items:
            for i, v in enumerate(node):
                errors.extend(validate_json_schema(v, items, f"{path}[{i}]"))
    elif t == "string":
        if not isinstance(node, str):
            errors.append(f"{path}: 应为 string，实为 {type(node).__name__}")
    elif t == "integer":
        if isinstance(node, bool) or not isinstance(node, int):
            errors.append(f"{path}: 应为 integer，实为 {type(node).__name__}: {node!r}")
    elif t == "number":
        if isinstance(node, bool) or not isinstance(node, (int, float)):
            errors.append(f"{path}: 应为 number，实为 {type(node).__name__}: {node!r}")
    elif t == "boolean":
        if not isinstance(node, bool):
            errors.append(f"{path}: 应为 boolean，实为 {type(node).__name__}")
    if "enum" in schema and isinstance(node, str) and node not in schema["enum"]:
        errors.append(f"{path}: {node!r} 不在枚举 {schema['enum']}")
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        if "minimum" in schema and node < schema["minimum"]:
            errors.append(f"{path}: {node} < minimum {schema['minimum']}")
        if "maximum" in schema and node > schema["maximum"]:
            errors.append(f"{path}: {node} > maximum {schema['maximum']}")
    return errors


def validate_nlu_output(payload, schema: dict | None = None) -> list:
    """校验 LLM 解析后的对象；返回错误列表（空＝合法）。"""
    return validate_json_schema(payload, schema or build_nlu_schema())


def slots_referenced_by_catalog() -> dict:
    """missing_slots 引用意图目录的映射（各意图必填/可选槽位），供提示与复核。"""
    return {
        intent: {"required": sorted(spec["required"]), "optional": sorted(spec["optional"])}
        for intent, spec in SLOT_MATRIX.items()
    }
