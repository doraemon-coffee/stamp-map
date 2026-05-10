#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NEWS 配列を空にリセットする一回限りのスクリプト
v3 のバグ実行で大量の「新規開店」レコードが書き込まれた際の修復用
"""

import re
import sys
from pathlib import Path

INDEX_HTML = Path("index.html")


def extract_array_bounds(html: str, var_name: str) -> tuple[int, int]:
    pattern = re.compile(r"const\s+" + re.escape(var_name) + r"\s*=\s*\[")
    m = pattern.search(html)
    if not m:
        raise ValueError(f"配列 {var_name} が見つかりません")
    start = m.end() - 1
    depth = 0
    in_string = False
    string_char = None
    i = start
    while i < len(html):
        c = html[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == string_char:
                in_string = False
        else:
            if c in ("'", '"', "`"):
                in_string = True
                string_char = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    raise ValueError(f"配列 {var_name} の終端が見つかりません")


def main():
    if not INDEX_HTML.exists():
        print(f"❌ {INDEX_HTML} が見つかりません", file=sys.stderr)
        sys.exit(1)

    html = INDEX_HTML.read_text(encoding="utf-8")
    start, end = extract_array_bounds(html, "NEWS")

    old = html[start:end]
    # ざっくり件数を数える (オブジェクト {…} の個数)
    item_count = old.count('"date"')
    print(f"📋 既存の NEWS 件数 (推定): {item_count}")
    print(f"📏 既存配列の文字数: {len(old):,}")

    # 空の配列にリセット
    new = (
        "[\n"
        "  // 例: {date:'2026-05-01', type:'open', store:'スターバックス 渋谷○○店', pref:'東京都', storeIdx:2118},\n"
        "  // 例: {date:'2026-04-30', type:'close', store:'スターバックス △△店', pref:'大阪府'},\n"
        "]"
    )

    new_html = html[:start] + new + html[end:]
    INDEX_HTML.write_text(new_html, encoding="utf-8")
    print("✅ NEWS 配列を空にリセットしました")
    print(f"📉 削減サイズ: {len(old) - len(new):,} 文字")


if __name__ == "__main__":
    main()
