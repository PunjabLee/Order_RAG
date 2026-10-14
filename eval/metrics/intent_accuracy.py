#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""intent_accuracy.py — 意图准确率评测（IT-2.02 DoD：夹具评测 ≥92%，PRD 基线）。

**真实测量原则**：对 eval/golden-set-v1/*.jsonl 仅取 `query`（输入）与 `intent`
（标注答案，只作评分参照），预测由 nlu 层独立产生——不存在"读标注答案反推
预测"的假评测（任务卡红线）。

层选择（--layer）：
  rule  规则/词表层（RuleLayer，默认；本轮交付的可运行评测）
  full  分层全管线（规则→小模型→LLM）。--provider <dotted.path> 传入小模型
        provider 类/工厂（零参可调用，或可调用类）；不传则用启发式基线
        src.nlu.providers.HeuristicBigramSmallModel。真实模型接入位（IT-2.05/G-B）。

门槛 --min-accuracy 默认 0.92（PRD-4.1 基线，命令行可调、源码不写死判定值）。
退出码：0=达标；1=未达标（如实报告，不做门禁绿灯伪装）；2=输入/参数错误。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections import Counter, defaultdict

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.nlu.intents import INTENTS                       # noqa: E402
from src.nlu.config import load_config, ConfigError       # noqa: E402
from src.nlu.router import RuleLayer, Pipeline            # noqa: E402

FILES = ("b1_price.jsonl", "b2_policy.jsonl", "b3_delivery.jsonl",
         "b4_context.jsonl", "b5_adversarial.jsonl")


def load_samples(dirpath: str) -> list:
    samples = []
    for fname in FILES:
        fp = os.path.join(dirpath, fname)
        if not os.path.isfile(fp):
            print(f"[ERR] 缺文件: {fp}", file=sys.stderr)
            return []
        with open(fp, encoding="utf-8") as fh:
            for ln, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(f"[ERR] {fname}:{ln} JSON 解析失败: {exc}", file=sys.stderr)
                    return []
                samples.append(obj)
    return samples


def resolve_provider(dotted: str):
    """--provider src.pkg.module:Attr 或 src.pkg.module.Attr → 小模型 provider 实例。"""
    if not dotted:
        from src.nlu.providers import HeuristicBigramSmallModel
        return HeuristicBigramSmallModel(load_config())
    mod_name, _, attr = dotted.replace(":", ".").rpartition(".")
    if not mod_name:
        raise SystemExit(f"[ERR] --provider 需为 dotted.path（模块.类名）: {dotted}")
    mod = importlib.import_module(mod_name)
    obj = getattr(mod, attr)
    return obj(load_config()) if callable(obj) else obj


def predict(rule: RuleLayer, pipe: Pipeline | None, query: str):
    """返回 (预测 intent | None, 置信)。None＝该层无证据（评测计未覆盖）。"""
    if pipe is not None:
        dec = pipe.understand(query)
        return dec.intent, dec.confidence
    labels = rule.classify(query)
    if not labels:
        return None, 0.0
    return labels[0].intent, labels[0].confidence


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IT-2.02 意图准确率评测")
    ap.add_argument("--dir", default=os.path.join(REPO_ROOT, "eval", "golden-set-v1"))
    ap.add_argument("--layer", choices=("rule", "full"), default="rule",
                    help="rule=规则/词表层实测（默认）；full=分层全管线（需注入 provider）")
    ap.add_argument("--provider", default="",
                    help="full 层小模型 provider 的 dotted.path（留真实模型接入口）")
    ap.add_argument("--min-accuracy", type=float, default=0.92,
                    help="门槛（默认 0.92＝PRD 基线；判定值由命令行给出，源码不写死）")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[ERR] 配置加载失败（致命，禁兜底）: {exc}", file=sys.stderr)
        return 2
    samples = load_samples(args.dir)
    if not samples:
        return 2

    rule = RuleLayer(cfg)
    pipe = None
    if args.layer == "full":
        small = resolve_provider(args.provider)
        # LLM provider 本轮无真实接入位（DD-3.1 降级链：解析失败→小模型→模板澄清）
        pipe = Pipeline(cfg, small_model=small, llm=None)

    n = 0
    correct = 0
    no_pred = 0            # 该层无证据（未覆盖）
    gold_cnt = Counter()
    per_intent_correct = Counter()
    confusion = defaultdict(Counter)   # gold → pred → n
    miss_list = []
    for s in samples:
        q = s.get("query", "")
        gold = s.get("intent")
        if gold not in set(INTENTS):
            print(f"[ERR] {s.get('id')} intent 非法: {gold}", file=sys.stderr)
            return 2
        pred, conf = predict(rule, pipe, q)
        n += 1
        gold_cnt[gold] += 1
        pred_key = pred if pred else "__NO_PRED__"
        confusion[gold][pred_key] += 1
        if pred == gold:
            correct += 1
            per_intent_correct[gold] += 1
        else:
            if pred is None:
                no_pred += 1
            miss_list.append((s.get("id"), gold, pred_key, conf, q))

    acc = correct / n if n else 0.0
    print("=" * 76)
    print(f"IT-2.02 意图准确率评测  layer={args.layer}"
          + (f"  provider={args.provider or 'HeuristicBigramSmallModel(基线)'}" if args.layer == "full" else ""))
    print("=" * 76)
    print(f"{'intent':<16}{'样本':>6}{'正确':>6}{'召回':>9}")
    for it in INTENTS:
        tot = gold_cnt.get(it, 0)
        okn = per_intent_correct.get(it, 0)
        rec = (okn / tot) if tot else float("nan")
        rec_txt = f"{rec:>8.1%}" if tot else "    —  "
        print(f"{it:<16}{tot:>6}{okn:>6}{rec_txt}")
    print("-" * 76)
    print(f"总样本 {n} | 正确 {correct} | 未命中 {n - correct}（其中无证据未覆盖 {no_pred}）")
    print(f"总准确率 = {acc:.4f} ({acc:.2%})   门槛 = {args.min_accuracy:.2%}")
    print("-" * 76)
    print("[分意图混淆（gold → top 预测计数）]")
    for it in INTENTS:
        if not gold_cnt.get(it):
            print(f"  {it}: 夹具无样本")
            continue
        row = ", ".join(f"{k}×{v}" for k, v in confusion[it].most_common())
        print(f"  {it:<16}: {row}")
    if miss_list:
        print("-" * 76)
        print(f"[未命中样本（前 30 / 共 {len(miss_list)}）]")
        for sid, gold, pred, conf, q in miss_list[:30]:
            print(f"  {sid} gold={gold} pred={pred} conf={conf:.2f} | {q[:44]}")

    verdict = "PASS" if acc >= args.min_accuracy else "FAIL"
    print("-" * 76)
    print(f"结论: {verdict}（accuracy {acc:.4f} vs threshold {args.min_accuracy:.4f}）")
    if verdict == "FAIL":
        print("说明: 本脚本如实测量，不做门禁伪装；是否达成 IT-2.02 DoD 由主会话按真实数值判定。")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
