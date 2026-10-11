#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IT-1.11 · mermaid 围栏/语法头静态检查器（附录 D "CI 渲染校验 L-3" 的最小可跑实现）。

检查项（对每个 *.md）：
1. 围栏配对统计：``` 围栏必须成对闭合，出现奇数即报错；
2. ```mermaid 围栏基础语法头：围栏首行（去空后）必须以
   flowchart / graph / stateDiagram / sequenceDiagram 开头（附录 A/DD-2.2/DD-6 现用四类）；
3. 换行标记统一：mermaid 块内出现字面 `\\n` 判违规（整改 L-3 要求统一 `<br/>`，附录 D）。

输出：违规文件 + 围栏号（按文件中 ``` 围栏出现次序全局编号，奇偶配对）+ 行号。
退出码：0 = 全部通过；1 = 存在违规；2 = 参数/IO 错误。

说明：本脚本是"渲染前置一致性检查"（围栏/语法头/换行标记），不是完整 mermaid 渲染器；
完整 headless 渲染（mermaid-cli）留给流水线 TODO 槽位（见 .github/workflows/ci.yaml）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FENCE_RE = re.compile(r"^[ \t]*(```+|~~~+)[ \t]*(\S*)[ \t]*$")
DIAGRAM_HEADERS = ("flowchart", "graph", "stateDiagram", "sequenceDiagram")

DEFAULT_EXCLUDES = {".git", ".idea", "node_modules", ".venv", "venv", "__pycache__", "tmp", ".cache"}


class Violation:
    def __init__(self, file: str, fence_no: int, line_no: int, kind: str, detail: str):
        self.file, self.fence_no, self.line_no, self.kind, self.detail = file, fence_no, line_no, kind, detail

    def __str__(self) -> str:
        loc = f"{self.file}:围栏#{self.fence_no}(行{self.line_no})" if self.fence_no else f"{self.file}:行{self.line_no}"
        return f"✗ [{self.kind}] {loc} {self.detail}"


def scan_text(path: str, text: str) -> list[Violation]:
    v: list[Violation] = []
    fence_no = 0            # 当前围栏号（每遇到开围栏 +1）
    open_fence: tuple | None = None   # (围栏标记, info 串)：当前未闭合围栏
    open_line = 0
    open_no = 0

    def close_matches(marker: str, line: str) -> bool:
        m = FENCE_RE.match(line)
        return bool(m) and m.group(2) == "" and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker)

    lines = text.splitlines()
    for i, line in enumerate(lines, start=1):
        m = FENCE_RE.match(line)
        if open_fence is None:
            if m:
                fence_no += 1
                open_fence = (m.group(1), m.group(2))
                open_line, open_no = i, fence_no
        else:
            marker, info = open_fence
            if close_matches(marker, line):
                if info.lower() == "mermaid":
                    v.extend(_check_mermaid_block(path, open_no, open_line, lines[open_line:i - 1]))
                open_fence = None
            # 块内行不驱动围栏状态（内容中的缩进 ``` 已在 close_matches 里按同字符同长度判定）
    if open_fence is not None:
        v.append(Violation(path, open_no, open_line, "fence-unclosed",
                           f"围栏开启后直到文件结束未闭合（``` 计数为奇数）：info='{open_fence[1]}'"))
    return v


def _check_mermaid_block(path: str, fence_no: int, open_line: int, body: list[str]) -> list[Violation]:
    v: list[Violation] = []
    # body[0] 是 ```mermaid 行本身之后的内容行
    first = next((k for k, ln in enumerate(body) if ln.strip()), None)
    if first is None:
        return [Violation(path, fence_no, open_line, "mermaid-empty", "mermaid 围栏为空")]
    header_line = body[first].strip()
    head_word = header_line.split(None, 1)[0] if header_line else ""
    if not any(head_word == h or head_word.startswith(h + "-") or head_word.startswith(h + " ")
               for h in DIAGRAM_HEADERS):
        # stateDiagram-v2 亦属合法（startswith("stateDiagram-")）；此处兜底再查一次前缀
        if not any(header_line.startswith(h) for h in DIAGRAM_HEADERS):
            v.append(Violation(path, fence_no, open_line + 1 + first, "mermaid-header",
                               f"围栏首行 '{header_line[:60]}' 不以基础语法头开头 {DIAGRAM_HEADERS}"))
    for k, ln in enumerate(body):
        # 字面反斜杠-n（非 <br/>）：附录 D 换行统一检查
        if re.search(r"(?<!\\)\\n", ln):
            v.append(Violation(path, fence_no, open_line + 1 + k, "mermaid-br",
                               f"出现字面 \\n，应统一 <br/>（整改 L-3/附录 D）：'{ln.strip()[:60]}'"))
    return v


def iter_md_files(root: Path, excludes: set[str]) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob("*.md")):
        if any(part in excludes for part in p.parts):
            continue
        out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="mermaid 围栏/语法头检查（IT-1.11，附录 D/L-3）")
    ap.add_argument("paths", nargs="*", help="指定文件或目录（默认：仓库根递归 *.md）")
    ap.add_argument("--root", default=".", help="扫描根目录（默认当前目录）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    files: list[Path]
    if args.paths:
        files = []
        for token in args.paths:
            p = (root / token) if not Path(token).is_absolute() else Path(token)
            if p.is_dir():
                files += iter_md_files(p, DEFAULT_EXCLUDES)
            elif p.is_file():
                files.append(p)
            else:
                print(f"[check-mermaid] ✗ 路径不存在：{token}", file=sys.stderr)
                return 2
    else:
        files = iter_md_files(root, DEFAULT_EXCLUDES)

    if not files:
        print("[check-mermaid] 未找到 *.md 文件（空扫描视为通过，请确认扫描根）")
        return 0

    total_v: list[Violation] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[check-mermaid] ✗ 读取失败 {f}: {exc}", file=sys.stderr)
            return 2
        rel = str(f)
        vs = scan_text(rel, text)
        n_mmd = len(re.findall(r"^[ \t]*```mermaid", text, re.M))
        status = "OK" if not vs else "FAIL"
        print(f"[check-mermaid] {status}: {rel}（mermaid 围栏 {n_mmd} 个）")
        total_v += vs

    for x in total_v:
        print(str(x), file=sys.stderr)
    print(f"[check-mermaid] 扫描 {len(files)} 个文件，违规 {len(total_v)} 处")
    return 1 if total_v else 0


if __name__ == "__main__":
    sys.exit(main())
