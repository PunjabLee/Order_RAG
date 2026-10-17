# -*- coding: utf-8 -*-
"""中文归一化（DD-3.1 整改 M-2：中文数量/型号归一化；用例集进 CI 回归）。

- 中文数字：\"三台\"→3（含 十/百/千/万 组合、\"两\"\"俩\"口语、阿拉伯数字混写）；
- 模糊量词：\"一批\"→触发数量澄清（决策表：数量不明确 → quantity 定向追问）；
- 模糊型号别名：\"M2 Ultra 64G 1T\"→经 model_alias 表映射 sku
  （别名表**配置化**：src/nlu/config/nlu.defaults.json#model_alias，
   真实别名随 Q1/商品服务联调回填，当前为 eval/fixtures SKU_A~D 占位，
   pending_question=Q7 —— 禁止在代码中硬编码别名，DD-16 R-07）。

纯函数、零网络、零金额（不做任何价格解析）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
    "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9,
}
_CN_UNIT = {"十": 10, "拾": 10, "百": 100, "千": 1000}
_CN_SECTION = {"万": 10000, "亿": 100000000}
_CN_CHARS = "".join(_CN_DIGIT) + "".join(_CN_UNIT) + "".join(_CN_SECTION)

#: 数量单位（台/个/件/套）；"俩"＝两个，口语数字单独处理
_QTY_UNIT_CLASS = "台个件套"
#: 小数/连号分隔符集（评审 R6）："3.5台"、"1.2.3台"、"29,999台" 整体捕获，
#: 守卫为 invalid（不猜尾数、不落槽），归入模糊/待校验澄清处置
_QTY_SEP_CLASS = ".,，。、"
_QTY_RE = re.compile(
    # 序数『第一个』≠数量（DD-3.2.4 指代属 IT-2.03）：第 之后不取数
    # R6：num 允许吞下 小数/连号 形态（含分隔符）→ 调用方守卫，禁止 \d+ 部分命中取错值
    r"(?P<pre>[-\-]?)\s*(?<!第)(?P<num>\d+(?:[" + _QTY_SEP_CLASS + r"]\s*\d+)*|[俩]|[" + _CN_CHARS + r"]+)\s*(?P<unit>[" + _QTY_UNIT_CLASS + r"])"
)


def normalize_text(s: str) -> str:
    """NFKC（全角→半角）、casefold、统一斜杠两侧空格、压缩空白。确定性、可复算。"""
    t = unicodedata.normalize("NFKC", s or "").casefold()
    t = t.replace("∕", "/").replace("／", "/")
    t = re.sub(r"\s*/\s*", "/", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def parse_chinese_number(text: str) -> int:
    """中文数字（≤亿）→ int。非法输入 ValueError（禁静默返回 None 兜底）。

    支持：三→3、十→10、十五→15、二十五→25、两百→200、一千→1000、
    三万二千→32000、俩→2、纯阿拉伯数字字符串。
    """
    t = (text or "").strip()
    if not t:
        raise ValueError("空字符串不是合法数字")
    if t == "俩":
        return 2
    if t.isdigit():
        return int(t)
    for ch in t:
        if ch not in _CN_CHARS:
            raise ValueError(f"非法中文字符 {ch!r}（不在中文数字表）: {text!r}")
    total = 0          # 已结算的节（万/亿 以上）
    section = 0        # 当前节内累计
    number = 0         # 当前个位
    seen = False
    for ch in t:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
            seen = True
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            # "十五" 前导十＝1×十；"二十五" 2×十
            section += (number if number else 1) * unit
            number = 0
            seen = True
        else:  # 万/亿 节权位
            sec_unit = _CN_SECTION[ch]
            total = (total + section + number) * sec_unit if seen else sec_unit
            section = 0
            number = 0
            seen = True
    if not seen:
        raise ValueError(f"无法解析: {text!r}")
    return total + section + number


@dataclass
class QuantityResult:
    """数量归一化结果。ambiguous=True 表示命中模糊量词 → 触发数量澄清（DD-3.1）。"""
    value: object = None            # int | None（多值/歧义时 None，见 values）
    values: list = field(default_factory=list)
    ambiguous: bool = False
    ambiguous_phrases: list = field(default_factory=list)
    negative: bool = False          # "-3台" 类非法输入（IT-2.04 校验拒绝）
    invalid: bool = False           # 评审 R6：小数/连号（"3.5台"）或 <1（"0台"/"零台"）
                                    # ——不猜测、不落槽，标记待校验（DD-3.3.1 "1≤q"）
    raw: str = ""

    @property
    def needs_clarification(self) -> bool:
        return self.ambiguous or self.value is None


def extract_quantity(text: str, cfg=None) -> QuantityResult:
    """中文数量/量词归一化（DD-3.1：\"三台\"→3、\"一批\"→触发数量澄清）。

    cfg 提供 signals.quantity_ambiguous 词表（配置化，禁硬编码）。
    """
    norm = normalize_text(text)
    res = QuantityResult(raw=norm)
    if cfg is not None:
        phrases = cfg.signals("quantity_ambiguous")
    else:  # 仅在独立调用调试时兜底读默认配置（正常链路必须传 cfg）
        from src.nlu.config import load_config
        phrases = load_config().signals("quantity_ambiguous")
    for p in phrases:
        pn = normalize_text(p)
        if pn and pn in norm:
            res.ambiguous = True
            res.ambiguous_phrases.append(p)
    # 口语"俩"＝2，允许无量词（"俩32G的要多少钱"）；DD-3.1 中文数字归一
    if "俩" in norm and 2 not in res.values:
        res.values.append(2)
    for m in _QTY_RE.finditer(norm):
        num_txt = m.group("num")
        frag = m.group(0).strip()
        if any(ch in num_txt for ch in _QTY_SEP_CLASS):
            # R6：小数/连号形态（"3.5台"、"1.2.3台"、"29,999台"）→ 不猜尾数，
            # 标记 invalid 并归入模糊澄清（原实现会静默取 "5" —— 评审实证）
            res.invalid = True
            if frag not in res.ambiguous_phrases:
                res.ambiguous_phrases.append(frag)
            continue
        if m.group("pre") in ("-", "－"):
            res.negative = True
        try:
            val = parse_chinese_number(num_txt)
        except ValueError:
            continue
        if val < 1:
            # R6：数量 <1（"0台"、"零台"）与 DD-3.3.1 "1≤q" 及本模块 LLM
            # Schema entities.quantity minimum:1 冲突 → invalid，不落槽
            res.invalid = True
            if frag not in res.ambiguous_phrases:
                res.ambiguous_phrases.append(frag)
            continue
        if val not in res.values:
            res.values.append(val)
    if res.invalid:
        # invalid 一律归入"模糊/待校验"处置（澄清而非路由；不猜测原值）
        res.ambiguous = True
    if res.values:
        res.value = res.values[0] if len(set(res.values)) == 1 else None
        if res.negative or res.invalid:
            res.value = None
    elif res.ambiguous:
        res.value = None
    return res


@dataclass
class ModelMatch:
    alias: str
    sku: str


def match_model_alias(text: str, cfg) -> list:
    """模糊型号 → sku 候选列表（经 model_alias 索引，DD-3.1 中文归一化行）。

    最长别名优先并占用字符区间：\"m2 ultra 64g 1t\" 只算一次命中、
    不再被 \"64g\"/\"m2 ultra\" 重复计数。返回 [(alias, sku)]；
    同一别名映射多个 sku 时列表长度 >1 → 调用方按\"实体歧义\"处置
    （决策表行 4：clarification + options[]）。
    真实别名表待 Q1/联调回填（pending_question=Q7）。
    """
    norm = normalize_text(text)
    # 同别名 → 多 sku 聚合（实体歧义），最长别名优先占用区间
    by_alias: dict = {}
    for e in cfg.alias_entries():
        key = normalize_text(e["alias"])
        if key:
            by_alias.setdefault((key, e["alias"]), set()).add(e["sku"])
    claimed: list = []   # (start, end) 已占用区间
    matches: list = []
    for (key, raw_alias), skus in sorted(by_alias.items(),
                                         key=lambda kv: (-len(kv[0][0]), kv[0][1])):
        start = 0
        while True:
            idx = norm.find(key, start)
            if idx < 0:
                break
            span = (idx, idx + len(key))
            if not any(s < span[1] and span[0] < e2 for s, e2 in claimed):
                claimed.append(span)
                for sku in sorted(skus):
                    matches.append(ModelMatch(alias=raw_alias, sku=sku))
            start = span[1]
    return matches


def sku_candidates(matches: list) -> list:
    """去重、稳定排序的 sku 候选（供实体歧义 options[]）。"""
    return sorted({m.sku for m in matches})
