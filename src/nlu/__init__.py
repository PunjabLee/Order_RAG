# -*- coding: utf-8 -*-
"""src.nlu — 查询理解模块（IT-2.02，设计依据 DD-3.1）。

零网络约束（本包任何模块不得 import requests/http/socket 等网络栈）：
- 分层路由（规则/词表 → 小模型 → LLM 兜底）以**可注入 provider** 表达，
  真实小模型/LLM 接入由上层服务（IT-2.10 MG 网关）在进程外完成；
- OUT_OF_SCOPE 处置＝引导话术，api_calls 恒为空（US-007-S4）。

契约红线：意图七枚举为唯一单源（intents.INTENTS），与
eval/metrics/validate_schema.py::INTENTS、eval/ANNOTATION_GUIDE.md §2 完全一致；
枚举变更走《研发迭代计划.md》§7 变更控制专道，本模块只对齐、不变更。
"""
from src.nlu.intents import (  # noqa: F401
    INTENTS,
    PRICE_QUERY,
    PROMO_QUERY,
    DELIVERY_QUERY,
    ORDER_CREATE,
    ORDER_QUERY,
    OUT_OF_SCOPE,
    ESCALATE_HUMAN,
    route_targets,
    required_slots,
    optional_slots,
    forbidden_slots,
    slots_of,
)
from src.nlu.config import Config, ConfigError, load_config  # noqa: F401
from src.nlu.normalize import (  # noqa: F401
    normalize_text,
    parse_chinese_number,
    extract_quantity,
    match_model_alias,
)
from src.nlu.router import (  # noqa: F401
    Action,
    Decision,
    Layer,
    Pipeline,
    RuleLayer,
    ScoreLabel,
    Signal,
)

__all__ = [
    "INTENTS", "PRICE_QUERY", "PROMO_QUERY", "DELIVERY_QUERY", "ORDER_CREATE",
    "ORDER_QUERY", "OUT_OF_SCOPE", "ESCALATE_HUMAN", "route_targets",
    "required_slots", "optional_slots", "forbidden_slots", "slots_of",
    "Config", "ConfigError", "load_config",
    "normalize_text", "parse_chinese_number", "extract_quantity",
    "match_model_alias",
    "Action", "Decision", "Layer", "Pipeline", "RuleLayer", "ScoreLabel", "Signal",
]
