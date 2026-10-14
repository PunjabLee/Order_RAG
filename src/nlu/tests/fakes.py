# -*- coding: utf-8 -*-
"""测试替身（fakes）：分层路由的可注入 provider 与 call-spy。

**零网络**：这里全是内存假实现，不含任何真实模型/HTTP 调用。
- FakeSmallModel / FakeLlmProvider：验证分层路由与降级链（IT-2.02）；
- ApiCallSpy：US-007-S4 断言（OUT_OF_SCOPE / 澄清 / 转人工 恒零 API 调用）；
- ParseFailRecorder：nlu.parse_fail 指标回调计数（DD-11.2）。
"""
from __future__ import annotations

import json


class ApiCallSpy:
    """记录管线声明的 API 调用目标（真实调用属 IT-2.08 调用器）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, target: str):
        self.calls.append(target)

    def reset(self):
        self.calls = []


class ParseFailRecorder:
    """nlu.parse_fail 回调收集器（DD-11.2 指标告警的注入位）。"""

    def __init__(self):
        self.events = []

    def __call__(self, event: dict):
        self.events.append(event)

    @property
    def count(self) -> int:
        return len(self.events)

    def metric_names(self):
        return {e.get("metric") for e in self.events}


class FakeSmallModel:
    """小模型 provider 假实现：固定返回 intent→score。"""

    name = "fake-small"

    def __init__(self, scores: dict | None = None, fn=None):
        self._scores = scores or {}
        self._fn = fn
        self.calls = []

    def classify(self, query: str, normalized: str) -> dict:
        self.calls.append(query)
        if self._fn is not None:
            return self._fn(query, normalized)
        return dict(self._scores)


class FakeLlmProvider:
    """LLM provider 假实现：按脚本序列返回原始文本 / 抛异常。

    responses 元素可为：
      str        → 原样返回（用于构造坏 JSON）
      dict       → json.dumps 后返回
      None       → 模拟调用失败
      Exception  → 抛出（provider 异常视作解析失败进入降级链）
    耗尽后重复最后一个元素，便于断言"重试 N 次"。
    """

    name = "fake-llm"

    def __init__(self, responses):
        if isinstance(responses, (str, dict)):
            responses = [responses]
        self._responses = list(responses)
        self.calls = []

    def generate(self, query: str, schema: dict, attempt: int):
        self.calls.append({"query": query, "attempt": attempt,
                           "schema": schema})
        idx = min(attempt - 1, len(self._responses) - 1)
        item = self._responses[idx]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return json.dumps(item, ensure_ascii=False)
        return item

    @staticmethod
    def payload(intent="PRICE_QUERY", confidence=0.85, entities=None,
                missing_slots=None):
        obj = {"intent": intent, "confidence": confidence}
        if entities is not None:
            obj["entities"] = entities
        if missing_slots is not None:
            obj["missing_slots"] = missing_slots
        return obj
