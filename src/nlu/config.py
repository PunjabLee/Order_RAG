# -*- coding: utf-8 -*-
"""nlu 配置加载（DD-3.1 阈值配置化；IT-2.02 禁硬编码，DD-16 R-07）。

原则（参照 scripts/ci/pricing_baseline.json 的单源教训，评审 N3）：
- **缺键即致命**：ConfigError，禁止静默兜底/默认漂移；
- **单源**：默认值唯一来自 config/nlu.defaults.json（标准库无 yaml，取 JSON，
  任务卡允许自行取舍）；可用 env `NLU_CONFIG_FILE` 指向 JSON 做覆盖层（深合并）；
- **env 数值覆盖**：NLU_CONF_LOW / NLU_CONF_RULE_STRONG / NLU_CONF_RULE_WEAK /
  NLU_CONF_OOS / NLU_SHORT_INPUT_MAX_CHARS / NLU_LLM_RETRY_MAX /
  NLU_COMPLEX_TURN_THRESHOLD；解析失败或越界即致命，不做静默回退；
- 本模块零网络；金额类数值禁止进入 nlu 配置（tests 有守卫用例）。
"""
from __future__ import annotations

import json
import os
import re

DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "config", "nlu.defaults.json")

# 必须存在且类型合法的键路径（缺任一 → ConfigError，禁静默兜底）
REQUIRED_PATHS = (
    "thresholds.conf_rule_strong:float",
    "thresholds.conf_rule_weak:float",
    "thresholds.conf_low:float",
    "thresholds.conf_oos:float",
    "thresholds.conf_coref_min:float",
    "thresholds.short_input_max_chars:int",
    "thresholds.llm_retry_max:int",
    "thresholds.complex_turn_threshold:int",
    "rule_vocab.priority:list",
    "scoring.rule_weight_strong:float",
    "scoring.rule_weight_weak:float",
    "scoring.small_model_base:float",
    "scoring.small_model_step:float",
    "scoring.small_model_cap:float",
    "signals.coref:list",
    "signals.ellipsis:list",
    "signals.quantity_ambiguous:list",
    "slot_hints.product_terms:list",
    "model_alias.entries:list",
    "templates.out_of_scope_guide:str",
    "templates.escalate_ack:str",
    "templates.clarify_generic:str",
    "templates.clarify_llm_fail:str",
    "templates.clarify_quantity_ambiguous:str",
    "templates.clarify_sku_ambiguous:str",
    "templates.clarify_short_input:str",
    "templates.clarify_slot:dict",
)

#: intent → 词表桶；七枚举每个都必须有词表条目（缺失即致命）
_VOCAB_INTENT_KEYS = ("strong", "weak", "regex_strong", "regex_weak")

_ENV_THRESHOLD_MAP = {
    "NLU_CONF_LOW": ("conf_low", float),
    "NLU_CONF_RULE_STRONG": ("conf_rule_strong", float),
    "NLU_CONF_RULE_WEAK": ("conf_rule_weak", float),
    "NLU_CONF_OOS": ("conf_oos", float),
    "NLU_SHORT_INPUT_MAX_CHARS": ("short_input_max_chars", int),
    "NLU_LLM_RETRY_MAX": ("llm_retry_max", int),
    "NLU_COMPLEX_TURN_THRESHOLD": ("complex_turn_threshold", int),
}

#: 金额字段名守卫（不含 PRICE_QUERY 等枚举键：仅拦 *_cents / price 裸键 / 金额语义键）
_MONEY_KEY_RE = re.compile(r"(cents$|_cents|^cents$|^price$|^unit_price$|amount|yuan|\u91d1\u989d|\u5355\u4ef7)", re.I)


class ConfigError(RuntimeError):
    """配置读取失败＝致命（单源原则，禁兜底漂移；参照 pricing_baseline 教训）。"""


def _split_path(path: str) -> list:
    return [p for p in path.split(".") if p]


def _get(node, path: str):
    cur = node
    for key in _split_path(path):
        if not isinstance(cur, dict) or key not in cur:
            raise ConfigError(f"配置缺键: {path}（禁静默兜底，见 DD-3.1 阈值配置化）")
        cur = cur[key]
    return cur


_TYPE_CHECKS = {
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "str": lambda v: isinstance(v, str),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _assert_no_amount_keys(node, path=""):
    """守卫：nlu 配置禁止出现金额类字段（金额属促销服务域，DD-8.2/DD-16 R-07）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.startswith("_"):
                continue
            if _MONEY_KEY_RE.search(str(k)):
                raise ConfigError(f"nlu 配置出现金额类键 {path + '.' if path else ''}{k}（禁止，IT-2.02 约束）")
            _assert_no_amount_keys(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _assert_no_amount_keys(v, f"{path}[{i}]")


class Config:
    """只读访问器；任何缺键访问立即 ConfigError。"""

    def __init__(self, data: dict, source: str):
        self._data = data
        self.source = source

    # ---- 通用 ----
    def raw(self) -> dict:
        return self._data

    def get(self, path: str):
        return _get(self._data, path)

    # ---- 阈值 ----
    def threshold(self, name: str):
        return _get(self._data, f"thresholds.{name}")

    @property
    def conf_rule_strong(self) -> float:
        return float(self.threshold("conf_rule_strong"))

    @property
    def conf_rule_weak(self) -> float:
        return float(self.threshold("conf_rule_weak"))

    @property
    def conf_low(self) -> float:
        """nlu.conf_low（DD-3.1 显式配置项，默认 0.6）。"""
        return float(self.threshold("conf_low"))

    @property
    def conf_oos(self) -> float:
        return float(self.threshold("conf_oos"))

    @property
    def conf_coref_min(self) -> float:
        return float(self.threshold("conf_coref_min"))

    @property
    def short_input_max_chars(self) -> int:
        return int(self.threshold("short_input_max_chars"))

    @property
    def llm_retry_max(self) -> int:
        return int(self.threshold("llm_retry_max"))

    @property
    def complex_turn_threshold(self) -> int:
        return int(self.threshold("complex_turn_threshold"))

    # ---- 打分标尺（内部假设值，配置化，禁硬编码） ----
    def score(self, name: str) -> float:
        return float(_get(self._data, f"scoring.{name}"))

    @property
    def rule_weight_strong(self) -> float:
        return self.score("rule_weight_strong")

    @property
    def rule_weight_weak(self) -> float:
        return self.score("rule_weight_weak")

    @property
    def small_model_base(self) -> float:
        return self.score("small_model_base")

    @property
    def small_model_step(self) -> float:
        return self.score("small_model_step")

    @property
    def small_model_cap(self) -> float:
        return self.score("small_model_cap")

    # ---- 词表 / 信号 / 别名 / 话术 ----
    def vocab(self, intent: str) -> dict:
        node = _get(self._data, f"rule_vocab.{intent}")
        for key in _VOCAB_INTENT_KEYS:
            if key not in node:
                raise ConfigError(f"rule_vocab.{intent} 缺桶 {key}（七枚举每个意图必须齐全）")
        return node

    def priority(self) -> list:
        return list(self.get("rule_vocab.priority"))

    def signals(self, name: str) -> list:
        return list(_get(self._data, f"signals.{name}"))

    def alias_entries(self) -> list:
        return list(self.get("model_alias.entries"))

    def alias_pending_question(self):
        return self._data.get("model_alias", {}).get("pending_question")

    def template(self, name: str) -> str:
        v = _get(self._data, f"templates.{name}")
        if not isinstance(v, str):
            raise ConfigError(f"templates.{name} 必须为字符串")
        return v

    def slot_clarify(self, intent: str, slot: str):
        node = self.get("templates.clarify_slot")
        return node.get(intent, {}).get(slot)


def _validate(cfg: Config) -> None:
    # 1) 必填键路径 + 类型
    for spec in REQUIRED_PATHS:
        path, typ = spec.rsplit(":", 1)
        val = cfg.get(path)
        if not _TYPE_CHECKS[typ](val):
            raise ConfigError(f"配置 {path} 类型应为 {typ}，实为 {type(val).__name__}")
    # 2) 阈值语义区间与次序（conf_oos < conf_low ≤ conf_rule_weak ≤ conf_rule_strong ≤ 1）
    oos, low = cfg.conf_oos, cfg.conf_low
    weak, strong = cfg.conf_rule_weak, cfg.conf_rule_strong
    if not (0.0 <= oos < low <= weak <= strong <= 1.0):
        raise ConfigError(
            f"阈值次序非法：conf_oos={oos} < conf_low={low} ≤ conf_rule_weak={weak} "
            f"≤ conf_rule_strong={strong} ≤ 1 必须成立"
        )
    if cfg.rule_weight_strong <= 0 or cfg.rule_weight_weak <= 0 \
            or cfg.rule_weight_weak > cfg.rule_weight_strong:
        raise ConfigError("scoring.rule_weight_* 非法：必须 >0 且 weak ≤ strong")
    if not (0.0 <= cfg.small_model_base <= cfg.small_model_cap <= 1.0) \
            or cfg.small_model_step <= 0:
        raise ConfigError("scoring.small_model_* 非法：0 ≤ base ≤ cap ≤ 1 且 step > 0")
    if cfg.short_input_max_chars < 1 or cfg.llm_retry_max < 0 or cfg.complex_turn_threshold < 1:
        raise ConfigError("整数阈值非法：short_input_max_chars≥1、llm_retry_max≥0、complex_turn_threshold≥1")
    # 3) 七枚举词表齐全（对齐 intents.INTENTS —— 单源校验）
    from src.nlu.intents import INTENTS
    for intent in INTENTS:
        cfg.vocab(intent)
    pri = cfg.priority()
    if set(pri) != set(INTENTS) or len(pri) != len(INTENTS):
        raise ConfigError("rule_vocab.priority 必须恰好覆盖七枚举一次")
    # 4) 别名表条目形状
    for i, e in enumerate(cfg.alias_entries()):
        if not isinstance(e, dict) or "alias" not in e or "sku" not in e:
            raise ConfigError(f"model_alias.entries[{i}] 需含 alias/sku 两键")
    # 5) 金额守卫
    _assert_no_amount_keys(cfg.raw())


def load_config(env=None) -> Config:
    """加载默认值 + NLU_CONFIG_FILE 覆盖 + env 阈值覆盖；任何异常即 ConfigError。"""
    env = os.environ if env is None else env
    try:
        with open(DEFAULTS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ConfigError(f"nlu.defaults.json 读取失败（单源要求，禁兜底）: {exc}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"nlu.defaults.json JSON 解析失败: {exc}")

    overlay_path = env.get("NLU_CONFIG_FILE")
    if overlay_path:
        try:
            with open(overlay_path, encoding="utf-8") as fh:
                overlay = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"NLU_CONFIG_FILE={overlay_path} 读取失败（禁静默忽略）: {exc}")
        if not isinstance(overlay, dict):
            raise ConfigError("NLU_CONFIG_FILE 顶层必须是 JSON 对象")
        data = _deep_merge(data, overlay)

    # env 数值覆盖（解析失败即致命，不回退默认）
    for name, (key, caster) in _ENV_THRESHOLD_MAP.items():
        raw = env.get(name)
        if raw is None or str(raw) == "":
            continue
        try:
            data.setdefault("thresholds", {})[key] = caster(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"env {name}={raw!r} 无法解析为 {caster.__name__}: {exc}")

    cfg = Config(data, source="defaults+env")
    _validate(cfg)
    return cfg
