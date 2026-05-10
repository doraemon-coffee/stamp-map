#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
スタバ全国店舗データを取得して index.html を更新するスクリプト (v3)

v3 の変更点:
- 緯度経度フィールドを「location_jp」(カンマ区切り文字列) から正しくパース
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

# ===== 設定 =====
API_BASE = "https://hn8madehag.execute-api.ap-northeast-1.amazonaws.com/prd-2019-08-21/storesearch"
PAGE_SIZE = 100
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja",
    "Origin": "https://store.starbucks.co.jp",
    "Referer": "https://store.starbucks.co.jp/",
}
INDEX_HTML = Path("index.html")
NEWS_RETENTION_DAYS = 90


def fetch_page(start: int) -> dict:
    params = {
        "size": PAGE_SIZE,
        "start": start,
        "q.parser": "structured",
        "q": "(and ver:10000 record_type:1)",
        "fq": "(and data_type:'prd')",
        "sort": "store_id asc",
    }
    url = API_BASE + "?" + urllib.parse.urlencode(params, safe="(): ")
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def fetch_all_stores() -> list[dict]:
    print("📡 スタバ API から全店舗データを取得中...")
    first = fetch_page(0)
    total = first["hits"]["found"]
    print(f"   全店舗数: {total}")
    all_hits = list(first["hits"]["hit"])
    start = PAGE_SIZE
    while start < total:
        time.sleep(0.4)
        page = fetch_page(start)
        all_hits.extend(page["hits"]["hit"])
        if start % 500 == 0 or start + PAGE_SIZE >= total:
            print(f"   取得中... {len(all_hits)}/{total}")
        start += PAGE_SIZE
    print(f"✅ 全 {len(all_hits)} 件取得完了")
    return all_hits


def get_first(fields: dict, key: str, default=""):
    """フィールド値を取得 (リストなら先頭要素)"""
    v = fields.get(key, default)
    if isinstance(v, list):
        return v[0] if v else default
    return v


def parse_location(loc_str) -> tuple[float, float]:
    """カンマ区切りの '緯度,経度' 文字列をパース"""
    if not loc_str:
        return 0.0, 0.0
    if isinstance(loc_str, list):
        loc_str = loc_str[0] if loc_str else ""
    parts = str(loc_str).split(",")
    if len(parts) >= 2:
        try:
            return float(parts[0].strip()), float(parts[1].strip())
        except (ValueError, TypeError):
            return 0.0, 0.0
    return 0.0, 0.0


def normalize_store(hit: dict) -> dict:
    f = hit.get("fields", {})

    # 緯度経度: location_jp を優先 (より正確)、なければ location
    loc = get_first(f, "location_jp") or get_first(f, "location") or ""
    lat, lng = parse_location(loc)

    return {
        "id": str(hit.get("id", "")),
        "jp": str(get_first(f, "name", "")).strip(),
        "en": str(get_first(f, "en_name", "")).strip(),
        "pref": str(get_first(f, "address_1", "")).strip(),
        "city": str(get_first(f, "address_2", "")).strip(),
        "lat": round(lat, 6),
        "lng": round(lng, 6),
    }


def extract_array(html: str, var_name: str) -> tuple[str, int, int]:
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
                    return html[start:i + 1], start, i + 1
        i += 1
    raise ValueError(f"配列 {var_name} の終端が見つかりません")


def strip_js_comments(text: str) -> str:
    out = []
    i = 0
    in_string = False
    string_char = None
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\" and i + 1 < len(text):
                out.append(text[i:i + 2])
                i += 2
                continue
            if c == string_char:
                in_string = False
            out.append(c)
            i += 1
        else:
            if c in ("'", '"', "`"):
                in_string = True
                string_char = c
                out.append(c)
                i += 1
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "/":
                while i < len(text) and text[i] != "\n":
                    i += 1
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "*":
                i += 2
                while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
            else:
                out.append(c)
                i += 1
    return "".join(out)


def parse_stores_from_html(html: str) -> list[dict]:
    arr_text, _, _ = extract_array(html, "STORES")
    return json.loads(arr_text)


def parse_news_from_html(html: str) -> list[dict]:
    try:
        arr_text, _, _ = extract_array(html, "NEWS")
    except ValueError:
        return []
    cleaned = strip_js_comments(arr_text)
    items = []
    obj_pattern = re.compile(r"\{[^{}]*\}", re.DOTALL)
    for obj_match in obj_pattern.finditer(cleaned):
        obj_text = obj_match.group(0)
        try:
            json_text = re.sub(r"(\w+)\s*:", r'"\1":', obj_text)
            json_text = json_text.replace("'", '"')
            json_text = re.sub(r",\s*}", "}", json_text)
            items.append(json.loads(json_text))
        except json.JSONDecodeError:
            continue
    return items


def detect_changes(old_stores, new_stores):
    old_ids = {s["id"]: s for s in old_stores if s.get("id")}
    new_ids = {s["id"]: s for s in new_stores if s.get("id")}
    opened = [new_ids[i] for i in new_ids if i not in old_ids]
    closed = [old_ids[i] for i in old_ids if i not in new_ids]
    return opened, closed


def build_news(opened, closed, new_stores, old_news):
    today = date.today().isoformat()
    news = []
    for s in opened:
        idx = next((i for i, t in enumerate(new_stores) if t["id"] == s["id"]), None)
        entry = {"date": today, "type": "open", "store": s["jp"], "pref": s.get("pref", "")}
        if idx is not None:
            entry["storeIdx"] = idx
        news.append(entry)
    for s in closed:
        news.append({"date": today, "type": "close", "store": s["jp"], "pref": s.get("pref", "")})
    cutoff = date.today().toordinal() - NEWS_RETENTION_DAYS
    for n in old_news:
        try:
            d = date.fromisoformat(n.get("date", "")).toordinal()
        except ValueError:
            continue
        if d >= cutoff:
            news.append(n)
    for n in news:
        if n.get("type") == "open":
            store_name = n.get("store", "")
            for i, s in enumerate(new_stores):
                if s["jp"] == store_name:
                    n["storeIdx"] = i
                    break
    return news


def render_stores_array(stores):
    lines = ["["]
    for i, s in enumerate(stores):
        comma = "," if i < len(stores) - 1 else ""
        lines.append(json.dumps(s, ensure_ascii=False) + comma)
    lines.append("]")
    return "\n".join(lines)


def render_news_array(news):
    if not news:
        return (
            "[\n"
            "  // 例: {date:'2026-05-01', type:'open', store:'スターバックス 渋谷○○店', pref:'東京都', storeIdx:2118},\n"
            "  // 例: {date:'2026-04-30', type:'close', store:'スターバックス △△店', pref:'大阪府'},\n"
            "]"
        )
    lines = ["["]
    for n in news:
        lines.append("  " + json.dumps(n, ensure_ascii=False) + ",")
    lines.append("]")
    return "\n".join(lines)


def update_html(html, new_stores, new_news):
    _, news_start, news_end = extract_array(html, "NEWS")
    html = html[:news_start] + render_news_array(new_news) + html[news_end:]
    _, stores_start, stores_end = extract_array(html, "STORES")
    html = html[:stores_start] + render_stores_array(new_stores) + html[stores_end:]
    count_str = f"{len(new_stores):,}"
    html = re.sub(
        r'(id="count-badge"[^>]*>)[\d,]+店舗(</span>)',
        rf"\g<1>{count_str}店舗\g<2>",
        html,
    )
    return html


def main():
    if not INDEX_HTML.exists():
        print(f"❌ {INDEX_HTML} が見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"📄 {INDEX_HTML} を読み込み中...")
    html = INDEX_HTML.read_text(encoding="utf-8")
    old_stores = parse_stores_from_html(html)
    old_news = parse_news_from_html(html)
    print(f"   現在の店舗数: {len(old_stores)}")
    print(f"   現在のお知らせ件数: {len(old_news)}")

    raw = fetch_all_stores()
    new_stores = [normalize_store(h) for h in raw]
    valid = [s for s in new_stores if s["jp"] and s["lat"] and s["lng"]]
    print(f"   変換後の店舗数: {len(new_stores)}")
    print(f"   有効な店舗数 (名前+座標あり): {len(valid)}")

    if new_stores:
        print(f"   サンプル: {new_stores[0]}")

    if len(valid) < len(new_stores) * 0.9:
        print("⚠️ 有効な店舗が極端に少ないため、HTML 更新を中止します。")
        sys.exit(0)

    new_stores = valid
    opened, closed = detect_changes(old_stores, new_stores)
    print(f"🆕 新規開店: {len(opened)} 店")
    for s in opened[:10]:
        print(f"   + {s['jp']} ({s.get('pref', '')})")
    print(f"🔚 閉店:     {len(closed)} 店")
    for s in closed[:10]:
        print(f"   - {s['jp']} ({s.get('pref', '')})")

    if not opened and not closed:
        print("✨ 変更なし。HTML 更新をスキップします。")
        return

    new_news = build_news(opened, closed, new_stores, old_news)
    print("📝 index.html を更新中...")
    new_html = update_html(html, new_stores, new_news)
    INDEX_HTML.write_text(new_html, encoding="utf-8")
    print("✅ 完了")


if __name__ == "__main__":
    main()
