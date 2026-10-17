# -*- coding: utf-8 -*-
"""小模型 / LLM 可注入 provider（IT-2.02 分层路由的外接面）。

**零网络约束**：本模块不发起任何调用；真实实现（模型网关 MG、推理服务，
DD-3.7/IT-2.10）由装配方注入。此处仅提供：

- SmallModelProvider 协议（classify → intent→score）；
- HeuristicBigramSmallModel：启发式基线（字符 bigram 统计，规则统计而非训练，
  任务卡明确"本轮提供可插拔接口+启发式基线实现"）；置信落 [conf_oos, conf_rule)
  区间，永不冒充词表层高置信；
- LlmProvider 协议（generate → JSON 文本）。本轮不内置真实 LLM 实现，
  未注入 provider 时路由器直接走降级链（小模型结果 → 模板澄清）。
"""
from __future__ import annotations

from src.nlu.intents import INTENTS
from src.nlu.normalize import normalize_text


class SmallModelProvider:
    """小模型意图分类接口（多标签：返回 intent→score，score∈[0,1]）。"""

    name = "interface"

    def classify(self, query: str, normalized: str) -> dict:
        raise NotImplementedError


class LlmProvider:
    """LLM 结构化输出接口：给定用户文本与 JSON Schema，返回**模型原始输出文本**。

    attempt 从 1 起（供真实实现做温度/提示微调）；返回 None 表示调用失败。
    解析（json.loads + Schema 校验）由路由器完成，解析失败计入 nlu.parse_fail。
    """

    name = "interface"

    def generate(self, query: str, schema: dict, attempt: int) -> str | None:
        raise NotImplementedError


class HeuristicBigramSmallModel(SmallModelProvider):
    """启发式基线：查询与意图词表的字符 bigram 重合统计（无训练、纯标准库）。

    定位＝多标签打分器（DD-3.1 决策表行 3 的"分类器多标签输出"基线）。
    score = base + step×命中词数，封顶 cap（三者均为配置标尺 scoring.*，
    禁硬编码；G-B 校准挂 Q：阈值标定）。cap < conf_rule_strong，
    永不冒充词表层高置信、避免与规则层语义混淆。
    """

    name = "heuristic-bigram"

    def __init__(self, cfg):
        self._cfg = cfg
        self.base = cfg.small_model_base
        self.step = cfg.small_model_step
        self.cap = cfg.small_model_cap
        self._terms = {}
        for intent in INTENTS:
            v = cfg.vocab(intent)
            terms = list(v["strong"]) + list(v["weak"])
            # 正则桶退化为字面片段集合（启发式基线不做正则求值）
            for rx in v["regex_strong"] + v["regex_weak"]:
                for frag in _regex_fragments(rx):
                    terms.append(frag)
            self._terms[intent] = [normalize_text(t) for t in terms if t]

    def classify(self, query: str, normalized: str) -> dict:
        text = normalized or normalize_text(query)
        scores = {}
        for intent, terms in self._terms.items():
            hits = 0
            for t in terms:
                if not t or t in ("|", "("):
                    continue
                if _bigram_overlap(text, t):
                    hits += 1
            if hits:
                scores[intent] = round(min(self.cap, self.base + self.step * hits), 4)
        return scores


def _bigram_overlap(text: str, term: str) -> bool:
    """text 与 term 是否存在 ≥2 字符连续公共子串（词面近似统计特征）。"""
    if not term:
        return False
    if term in text or text in term and len(text) >= 2:
        return True
    n = 2
    grams = {text[i:i + n] for i in range(len(text) - n + 1)}
    tgrams = {term[i:i + n] for i in range(len(term) - n + 1)}
    return bool(grams & tgrams)


def _regex_fragments(rx: str) -> list:
    """从受限词表正则提取中文字面片段（启发式基线专用，尽力而为）。"""
    import re
    frags = re.findall(r"[一-鿿]{2,}", rx)
    return frags
