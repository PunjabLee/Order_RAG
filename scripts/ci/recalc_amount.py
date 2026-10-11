#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IT-1.11 · DD-8.1 示例金额独立复算 + 设计文档数值一致性比对。

复算口径（全部整数分运算，DD-8 金额以分为单位；禁用浮点，可复算）：
    默认参数（scripts/ci/pricing_baseline.json，假设值挂 Q1/Q7，禁硬编码 DD-16 R-07）：
      list_total = unit_price_cents × quantity             = 2,999,900 × 3 = 8,999,700 分
      先折：discount = round_half_up(list_total × 1000bp / 10000) = 899,970 分；
             按 DD-7.4 breakdown（bulk_discount_10pct = -900,000）与 DD-8.1 尾差修正
             （80,497.30→80,497.00，H-11/K-6）口径，折扣金额规整到整元 → 900,000 分
             （规整步长是配置项 discount_round_step_cents，联调后随 Q1/Q7 关闭替换）
      后减：满减券 50,000 分（门槛：折后应付 8,099,700 ≥ 8,000,000 分 ✓）
      total = 8,999,700 − 900,000 − 50,000 = 8,049,700 分 = ¥80,497.00   ← 应得值
    备选顺序 coupon_then_discount（文档对照值 80,547.30，DD-8.1）：
      (8,999,700 − 50,000) × 0.9 = 8,054,730 分 = ¥80,547.30

文档比对（grep 设计文档.md，不一致即报）：
  A. 所有 `"total_cents":N` 必须等于复算 total（当前全文唯一金额基线即 DD-8.1 三台示例）；
  B. breakdown 分项之和 == 同对象 total_cents（DD-8.2 尾差计入最后一项 → 严格相等）；
  C. 展示值 `80,497.xx` 只允许 `.00`；旧尾差值仅可出现在历史叙述行（旧版/尾差/修正/收敛/残留等）；
  D. 整数减法链 `a−b−c=d`（〇.3 表：8999700−900000−50000=8049700）须算术成立且与复算分项一致；
  E. 先减后折对照值 `80,547.xx`（若文档引用）须等于备选顺序复算结果。

退出码：0 全部一致；1 存在不一致或复算≠期望；2 参数/IO 错误。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

BP = 10000  # 万分比分母（basis points）
SEQ_DISCOUNT_FIRST = "discount_then_coupon"   # 先折后减（DD-8.1 假设，Q7）
SEQ_COUPON_FIRST = "coupon_then_discount"     # 先减后折（文档对照：80,547.30）

# 历史叙述行标记：允许其中出现旧值（如 80,497.30），其余位置出现即报
HIST_MARKERS = re.compile(r"旧版|尾差|修正|收敛|残留|统一为|处理依据|虚构")


def round_half_up_div(numerator: int, denominator: int) -> int:
    """正整数四舍五入除法，一次到位（DD-8.2 舍入规则）。"""
    return (2 * numerator + denominator) // (2 * denominator)


def round_to_step(cents: int, step: int) -> int:
    """把分数量规整到 step 的最近倍数（四舍五入）。step<=1 时不动。"""
    if step <= 1:
        return cents
    return round_half_up_div(cents, step) * step


@dataclass
class PricingResult:
    total_cents: int
    list_total_cents: int
    discount_cents: int
    coupon_cents: int
    sequence: str
    breakdown: list = field(default_factory=list)  # [(item, cents)]，cents 优惠为负

    @property
    def display(self) -> str:
        return f"{self.total_cents / 100:,.2f}"  # 仅展示用；判定全部走整数分


def recalc(cfg: dict, *, sequence: str | None = None) -> PricingResult:
    """按 DD-8.1 口径独立复算，返回含 breakdown 的结果（纯整数分）。"""
    seq = sequence or cfg["sequence"]
    unit = int(cfg["unit_price_cents"])
    qty = int(cfg["quantity"])
    dbp = int(cfg["discount_bp"])
    coupon = int(cfg["coupon_cents"])
    threshold = int(cfg["coupon_threshold_cents"])
    step = int(cfg.get("discount_round_step_cents", 1))
    min_qty = int(cfg.get("discount_qualify_min_quantity", 0))

    list_total = unit * qty
    discount_applied_bp = dbp if qty >= min_qty else 0  # 批量折扣门槛（DD-8.1）

    if seq == SEQ_DISCOUNT_FIRST:
        raw = round_half_up_div(list_total * discount_applied_bp, BP)
        discount = round_to_step(raw, step)  # 尾差规整口径：见 pricing_baseline.json 注释（Q1/Q7）
        after_discount = list_total - discount
        coupon_applied = coupon if after_discount >= threshold else 0
        total = after_discount - coupon_applied
        breakdown = [("list_price", list_total),
                     ("bulk_discount", -discount),
                     ("threshold_minus", -coupon_applied)]
    elif seq == SEQ_COUPON_FIRST:
        coupon_applied = coupon if list_total >= threshold else 0
        after_coupon = list_total - coupon_applied
        total = round_half_up_div(after_coupon * (BP - discount_applied_bp), BP)
        discount = after_coupon - total
        breakdown = [("list_price", list_total),
                     ("threshold_minus", -coupon_applied),
                     ("bulk_discount", -discount)]
    else:
        raise ValueError(f"未知 pricing_sequence：{seq}（配置项，Q7）")

    assert sum(v for _, v in breakdown) == total, "分项和必须等于总额（DD-8.2）"
    return PricingResult(total, list_total, discount, coupon_applied, seq, breakdown)


def check_doc(doc_text: str, res: PricingResult, cfg: dict | None = None,
              hits: list[str] | None = None) -> list[str]:
    """grep 设计文档并与复算结果比对。返回不一致列表；hits 收集一致命中（供打印）。"""
    issues: list[str] = []
    hits = hits if hits is not None else []

    def line_of(pos: int) -> int:
        return doc_text[:pos].count("\n") + 1

    def line_text(no: int) -> str:
        return doc_text.splitlines()[no - 1]

    # A. total_cents 字段 == 复算总额
    for m in re.finditer(r'"total_cents"\s*:\s*(\d+)', doc_text):
        n, no = int(m.group(1)), line_of(m.start())
        if n != res.total_cents:
            issues.append(f"L{no}: `\"total_cents\":{n}` ≠ 复算总额 {res.total_cents}")
        else:
            hits.append(f"A/total_cents L{no} = {n}")

    # B. breakdown 分项和 == 同对象 total_cents（DD-8.2 尾差计入最后一项）
    for m in re.finditer(r'"breakdown":\[([^\]]*)\]', doc_text):
        no = line_of(m.start())
        cents = [int(x) for x in re.findall(r'"cents":\s*(-?\d+)', m.group(1))]
        preceding = doc_text[max(0, m.start() - 400):m.start()]
        tot = re.findall(r'"total_cents"\s*:\s*(\d+)', preceding)
        if not cents:
            issues.append(f"L{no}: breakdown 解析不到 cents 项")
            continue
        s = sum(cents)
        if tot and s != int(tot[-1]):
            issues.append(f"L{no}: breakdown 分项和 {s} ≠ total_cents {tot[-1]}（违背 DD-8.2）")
        else:
            hits.append(f"B/breakdown L{no} sum={s}")

    # C. 展示值 80,497.xx：非 .00 只允许出现在历史叙述行
    for m in re.finditer(r"\b80,497\.(\d{2})\b", doc_text):
        no, suffix = line_of(m.start()), m.group(1)
        canonical = res.display  # 默认基线为 80,497.00
        if f"80,497.{suffix}" == canonical:
            hits.append(f"C/display L{no} = ¥80,497.{suffix}")
        elif HIST_MARKERS.search(line_text(no)):
            hits.append(f"C/历史叙述 L{no}（80,497.{suffix}，尾差修正记录，允许）")
        else:
            issues.append(f"L{no}: 展示值 80,497.{suffix} ≠ 复算 ¥{canonical}，且非历史叙述行")

    # C2. 旧尾差分单位字面量（8049730）不得出现在非历史行
    old_cents = f"{res.total_cents + 30}"  # 8,049,730：文档修正前的旧尾差值
    for m in re.finditer(rf"\b{old_cents}\b", doc_text):
        no = line_of(m.start())
        if not HIST_MARKERS.search(line_text(no)):
            issues.append(f"L{no}: 旧尾差值 {old_cents} 出现在非历史叙述行")

    # D. 整数减法链 a−b−c=d（〇.3 交叉验证行），须算术成立且与复算分项一致
    for m in re.finditer(r"(\d{5,})\s*[−-]\s*(\d{4,})\s*[−-]\s*(\d{3,})\s*=\s*(\d{5,})", doc_text):
        a, b, c, d = (int(g) for g in m.groups())
        no = line_of(m.start())
        if a - b - c != d:
            issues.append(f"L{no}: 减法链算术不成立 {a}−{b}−{c}={d}")
        elif (a, b, c, d) != (res.list_total_cents, res.discount_cents, res.coupon_cents, res.total_cents):
            issues.append(f"L{no}: 减法链 {a}−{b}−{c}={d} 与复算分项 "
                          f"{res.list_total_cents}−{res.discount_cents}−{res.coupon_cents}={res.total_cents} 不一致")
        else:
            hits.append(f"D/chain L{no} {a}−{b}−{c}={d}")

    # E. 先减后折对照值（DD-8.1 备选顺序）须等于备选复算
    if cfg is not None:
        for m in re.finditer(r"\b80,547\.(\d{2})\b", doc_text):
            no = line_of(m.start())
            alt = recalc(cfg, sequence=SEQ_COUPON_FIRST)
            if f"80,547.{m.group(1)}" != alt.display:
                issues.append(f"L{no}: 先减后折对照值 80,547.{m.group(1)} ≠ 备选复算 {alt.display}")
            else:
                hits.append(f"E/alt L{no} = {alt.display}")

    return issues


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="DD-8.1 金额独立复算 + 设计文档比对（IT-1.11）")
    ap.add_argument("--config", default=str(here / "pricing_baseline.json"),
                    help="基线参数配置（假设值挂 Q1/Q7，禁止硬编码）")
    ap.add_argument("--doc", default=None, help="设计文档路径（默认仓库根 设计文档.md）")
    ap.add_argument("--no-doc", action="store_true", help="只复算，不比对文档")
    ap.add_argument("--unit-price-cents", type=int, help="覆盖参数（任一覆盖将跳过文档断言）")
    ap.add_argument("--quantity", type=int)
    ap.add_argument("--discount-bp", type=int)
    ap.add_argument("--coupon-cents", type=int)
    ap.add_argument("--sequence", choices=[SEQ_DISCOUNT_FIRST, SEQ_COUPON_FIRST])
    args = ap.parse_args(argv)

    try:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[recalc-amount] ✗ 配置读取失败：{exc}", file=sys.stderr)
        return 2
    overridden = False
    for key, val in (("unit_price_cents", args.unit_price_cents), ("quantity", args.quantity),
                     ("discount_bp", args.discount_bp), ("coupon_cents", args.coupon_cents),
                     ("sequence", args.sequence)):
        if val is not None:
            cfg[key] = val
            overridden = True

    try:
        res = recalc(cfg)
    except ValueError as exc:
        print(f"[recalc-amount] ✗ {exc}", file=sys.stderr)
        return 2

    print("[recalc-amount] 复算（整数分，DD-8；参数=配置，假设值挂 Q1/Q7）：")
    print(f"  sequence={res.sequence}  unit={cfg['unit_price_cents']:,} 分 × qty={cfg['quantity']}")
    for name, cents in res.breakdown:
        print(f"  {name:>15}: {cents:+,}")
    print(f"  {'total':>15}: {res.total_cents:,} 分 → ¥{res.display}")

    issues: list[str] = []
    if not overridden:
        expected = int(cfg.get("expected_total_cents", 0))
        if expected and res.total_cents != expected:
            issues.append(f"默认参数复算 {res.total_cents} ≠ 期望 {expected}（DD-8.1 应得 8049700）")

        if not args.no_doc:
            doc_path = Path(args.doc) if args.doc else here.parent.parent / "设计文档.md"
            if not doc_path.is_file():
                print(f"[recalc-amount] ✗ 文档不存在：{doc_path}", file=sys.stderr)
                return 2
            hits: list[str] = []
            issues += check_doc(doc_path.read_text(encoding="utf-8"), res, cfg, hits)
            print(f"[recalc-amount] 文档比对：{doc_path.name}，一致命中 {len(hits)} 处")
            for h in hits:
                print(f"  ✓ {h}")
    else:
        print("[recalc-amount] ⚠ 参数被覆盖：仅输出复算值，跳过期望与文档断言"
              "（文档 grep 仅对默认基线有意义）")

    for x in issues:
        print(f"[recalc-amount] ✗ {x}", file=sys.stderr)
    if issues:
        print("[recalc-amount] 结论：不一致，拒绝（依据 DD-8.1 / 计划 IT-1.01 门禁）", file=sys.stderr)
        return 1
    print("[recalc-amount] 结论：通过（复算与文档基线一致；Q7 关闭前本值仅为假设基线）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
