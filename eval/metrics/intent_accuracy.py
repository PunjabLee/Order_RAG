#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""intent_accuracy.py — 意图准确率评测（IT-2.02 DoD：夹具评测 ≥92%，PRD 基线）。

**真实测量原则**：对 eval/golden-set-v1/*.jsonl 仅取 `query`（输入）、
`turn_context`/`effective_context`（输入侧上下文证据）与 `intent`
（标注答案，只作评分参照），预测由 nlu 层独立产生——不存在"读标注答案反推
预测"的假评测（任务卡红线）。

层选择（--layer）：
  rule  规则/词表层（RuleLayer，默认；本轮交付的可运行评测）
  full  分层全管线（规则→小模型→LLM）。--provider <dotted.path> 传入小模型
        provider 类/工厂（零参可调用，或可调用类）；不传则用启发式基线
        src.nlu.providers.HeuristicBigramSmallModel。真实模型接入位（IT-2.05/G-B）。

上下文注入（合入前评审 R4）：full 层**必须**把夹具上下文映射为
Pipeline.understand(query, context) 的入参，否则 full 退化为 rule（逐字节相同）：
  - turn_context 末条 slots_snapshot → ctx["slots"]；
  - turn_context 末条 options 与 effective_context.focus_ref → ctx["focus_entity"]；
  - len(turn_context) → ctx["turn_count"]（真实轮数，供 DD-3.1『多轮复杂』判定）。
输出打印"带上下文样本数/使用上下文的预测数"以便核对注入确实生效。
**红线**：只做输入侧映射，标注 intent / expected_* 一律不得进入预测路径。

门槛 --min-accuracy 默认 0.92（PRD-4.1 基线，命令行可调、源码不写死判定值）。
--expect-samples 默认 200（夹具规模护栏，样本数不符直接退出，防静默漏读文件）。
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


# ----------------------------------------------------------------------
# 上下文映射（评审 R4）：只读输入侧字段，绝不触碰 intent / expected_*
# ----------------------------------------------------------------------
def build_context(sample: dict) -> dict | None:
    """把夹具上下文映射为 router 的 context 入参（无上下文 → None）。

    映射口径（DD-3.1 行 1/2/8 的消解证据；消解算法本体属 IT-2.03）：
    - turn_context 末条 slots_snapshot → ctx["slots"]（剔除 null/空值，
      不猜测：null 槽位等于"未落槽"）；
    - turn_context 末条 options + effective_context.focus_ref →
      ctx["focus_entity"]["ref"]（列表）：focus_ref 为具体 SKU → 单值（可继承）；
      "focus_entity.options[0]" → 候选集首项单值；"focus_entity.options" →
      整个候选集（多值 → 管线按 1→N 实体歧义走点选澄清，不猜测）；
      其余形态无法解析 → 不注入；
    - len(turn_context) → ctx["turn_count"]：夹具窗口内的**真实轮数**
      （不用 turn_id 序号——那是全会话编号，非窗口内轮数，会虚高『多轮复杂』判定）。
    """
    tc = [t for t in (sample.get("turn_context") or []) if isinstance(t, dict)]
    ec = sample.get("effective_context") or {}
    ctx: dict = {}

    slots: dict = {}
    options: list = []
    if tc:
        last = tc[-1]
        for k, v in (last.get("slots_snapshot") or {}).items():
            if v is not None and v != "":
                slots[k] = v
        options = [o for o in (last.get("options") or []) if o]

    ref = ec.get("focus_ref")
    refs: list = []
    if isinstance(ref, str) and ref.strip():
        token = ref.strip()
        if token == "focus_entity.options":
            refs = list(options)
        elif token.endswith(".options[0]") or token.endswith("options[0]"):
            refs = list(options[:1])
        elif token.upper().startswith("SKU"):
            refs = [token]
    elif isinstance(ref, list):
        refs = [r for r in ref if r]

    if slots:
        ctx["slots"] = slots
    if refs:
        ctx["focus_entity"] = {"ref": refs}
    if tc:
        ctx["turn_count"] = len(tc)
    return ctx or None


def used_context(ctx: dict | None, dec) -> bool:
    """判定该次预测是否实际消费了上下文（口径保守上界，仅用于自检打印）。

    命中条件：管线声明"上下文继承"，或 ctx 注入的槽位/焦点值出现在决策槽位中。
    注：本轮字面与上下文值重合时会计为"使用"，故为上界估计（不影响准确率测量）。
    """
    if not ctx:
        return False
    if "上下文继承" in (getattr(dec, "notes", "") or ""):
        return True
    slots = getattr(dec, "slots", None) or {}
    for k, v in (ctx.get("slots") or {}).items():
        if slots.get(k) == v:
            return True
    for r in ((ctx.get("focus_entity") or {}).get("ref") or [])[:1]:
        if slots.get("sku") == r or slots.get("product") == r:
            return True
    return False


def predict(rule: RuleLayer, pipe: Pipeline | None, query: str, ctx: dict | None = None):
    """返回 (预测 intent | None, 置信, 决策对象 | None)。None＝该层无证据（评测计未覆盖）。

    R4：full 层把夹具上下文传入 understand(query, context)。
    """
    if pipe is not None:
        dec = pipe.understand(query, ctx)
        return dec.intent, dec.confidence, dec
    labels = rule.classify(query)
    if not labels:
        return None, 0.0, None
    return labels[0].intent, labels[0].confidence, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IT-2.02 意图准确率评测")
    ap.add_argument("--dir", default=os.path.join(REPO_ROOT, "eval", "golden-set-v1"))
    ap.add_argument("--layer", choices=("rule", "full"), default="rule",
                    help="rule=规则/词表层实测（默认）；full=分层全管线（需注入 provider）")
    ap.add_argument("--provider", default="",
                    help="full 层小模型 provider 的 dotted.path（留真实模型接入口）")
    ap.add_argument("--min-accuracy", type=float, default=0.92,
                    help="门槛（默认 0.92＝PRD 基线；判定值由命令行给出，源码不写死）")
    ap.add_argument("--expect-samples", type=int, default=200,
                    help="夹具样本数护栏（默认 200＝golden-set-v1 规模）；不符退出码 2")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[ERR] 配置加载失败（致命，禁兜底）: {exc}", file=sys.stderr)
        return 2
    samples = load_samples(args.dir)
    if not samples:
        return 2
    if len(samples) != args.expect_samples:
        print(f"[ERR] 样本数 {len(samples)} ≠ 期望 {args.expect_samples}"
              f"（夹具被增删/文件漏读，拒绝在残缺输入上出数）", file=sys.stderr)
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
    ctx_samples = 0        # 带上下文（映射后 ctx 非空）的样本数
    ctx_used = 0           # 预测实际消费上下文的数量（R4 自检）
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
        ctx = build_context(s) if pipe is not None else None
        if ctx:
            ctx_samples += 1
        pred, conf, dec = predict(rule, pipe, q, ctx)
        if dec is not None and used_context(ctx, dec):
            ctx_used += 1
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
    print(f"带上下文样本数 = {ctx_samples} | 使用上下文的预测数 = {ctx_used}"
          + ("" if pipe is not None else "（rule 层不消费上下文，恒 0）"))
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
