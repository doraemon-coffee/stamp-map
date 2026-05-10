#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
スタバ全国店舗データを取得して index.html を更新するスクリプト (v4)

v4 の重要な変更点:
- 既存 STORES と完全互換のフィールド構成で出力 (n, jp, pref, lat, lng, hours, type, addr)
- 店舗の比較・突合は jp (日本語店名) で行う (API には id フィールドが従来データに無いため)
- 既存店舗の並び順を保持し、新規開店は末尾に追加、閉店した店舗は除外する
  → ユーザーの訪問記録 (配列 index ベース) が壊れないようにする
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


# ====================================================================
# API 呼び出し
# ====================================================================
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


# ====================================================================
# データ正規化
# ====================================================================
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


def build_hours(f: dict) -> str:
    """営業時間を '07:00～22:30' 形式で構築"""
    # business_day_mon_thu があれば優先
    bd = str(get_first(f, "business_day_mon_thu", "")).strip()
    if bd:
        return bd.replace("〜", "～")
    # フォールバック: mon_open / mon_close
    mo = str(get_first(f, "mon_open", "")).strip()
    mc = str(get_first(f, "mon_close", "")).strip()
    if mo and mc:
        return f"{mo}～{mc}"
    return ""


def normalize_store(hit: dict) -> dict:
    """API レスポンスを既存 STORES と互換のある形に変換"""
    f = hit.get("fields", {})

    # 緯度経度: location_jp を優先 (より正確)、なければ location
    loc = get_first(f, "location_jp") or get_first(f, "location") or ""
    lat, lng = parse_location(loc)

    # 店舗タイプ
    store_type = get_first(f, "store_type", 1)
    try:
        type_int = int(store_type)
    except (ValueError, TypeError):
        type_int = 1

    return {
        "n": str(get_first(f, "en_name", "")).strip(),
        "jp": str(get_first(f, "name", "")).strip(),
        "pref": str(get_first(f, "address_1", "")).strip(),
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "hours": build_hours(f),
        "type": type_int,
        "addr": str(get_first(f, "address_5", "")).strip(),
    }


# ====================================================================
# 既存 HTML パース
# ====================================================================
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


# ====================================================================
# 差分検出 & マージ
# ====================================================================
def detect_changes(old_stores, new_stores):
    """jp (日本語店名) ベースで差分を検出"""
    old_jps = {s["jp"]: s for s in old_stores if s.get("jp")}
    new_jps = {s["jp"]: s for s in new_stores if s.get("jp")}
    opened = [new_jps[k] for k in new_jps if k not in old_jps]
    closed = [old_jps[k] for k in old_jps if k not in new_jps]
    return opened, closed


def merge_stores(old_stores, new_stores):
    """既存店舗の順序を保持しつつ、APIの最新データで上書き、新規開店は末尾に追加。"""
    new_by_jp = {s["jp"]: s for s in new_stores if s.get("jp")}
    old_jps = set(s.get("jp", "") for s in old_stores)

    merged = []
    # 1. 既存店舗を順序通りに保持 (API最新データで内容を更新)
    for old_s in old_stores:
        jp = old_s.get("jp", "")
        if jp and jp in new_by_jp:
            updated = dict(new_by_jp[jp])
            # API から en_name が空で来た場合は古い n を残す
            if not updated.get("n") and old_s.get("n"):
                updated["n"] = old_s["n"]
            merged.append(updated)
        # else: APIに無い→閉店として除外
    # 2. 新規開店店舗を末尾に追加
    for new_s in new_stores:
        jp = new_s.get("jp", "")
        if jp and jp not in old_jps:
            merged.append(new_s)
    return merged


def build_news(opened, closed, merged_stores, old_news):
    """news エントリを構築。merged_stores 内の最新位置に basedづく storeIdx を付与"""
    today = date.today().isoformat()
    news = []
    # 新規開店
    for s in opened:
        idx = next((i for i, t in enumerate(merged_stores) if t.get("jp") == s["jp"]), None)
        entry = {"date": today, "type": "open", "store": s["jp"], "pref": s.get("pref", "")}
        if idx is not None:
            entry["storeIdx"] = idx
        news.append(entry)
    # 閉店
    for s in closed:
        news.append({"date": today, "type": "close", "store": s["jp"], "pref": s.get("pref", "")})
    # 過去のニュース (90日以内のもの)
    cutoff = date.today().toordinal() - NEWS_RETENTION_DAYS
    for n in old_news:
        try:
            d = date.fromisoformat(n.get("date", "")).toordinal()
        except ValueError:
            continue
        if d >= cutoff:
            news.append(n)
    # 過去ニュースの storeIdx を新しい配列に合わせて更新 (open のみ)
    for n in news:
        if n.get("type") == "open":
            store_name = n.get("store", "")
            for i, s in enumerate(merged_stores):
                if s.get("jp") == store_name:
                    n["storeIdx"] = i
                    break
    return news


# ====================================================================
# HTML 出力
# ====================================================================
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


def update_html(html, merged_stores, new_news):
    # NEWS を書き換え (先に書き換えると STORES の位置が変わるので NEWS から)
    _, news_start, news_end = extract_array(html, "NEWS")
    html = html[:news_start] + render_news_array(new_news) + html[news_end:]
    _, stores_start, stores_end = extract_array(html, "STORES")
    html = html[:stores_start] + render_stores_array(merged_stores) + html[stores_end:]
    count_str = f"{len(merged_stores):,}"
    html = re.sub(
        r'(id="count-badge"[^>]*>)[\d,]+店舗(</span>)',
        rf"\g<1>{count_str}店舗\g<2>",
        html,
    )
    return html


# ====================================================================
# メイン
# ====================================================================
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
        print(f"   サンプル(新): {new_stores[0]}")
    if old_stores:
        print(f"   サンプル(旧): {old_stores[0]}")

    if len(valid) < len(new_stores) * 0.9:
        print("⚠️ 有効な店舗が極端に少ないため、HTML 更新を中止します。")
        sys.exit(0)

    new_stores = valid

    # 差分検出
    opened, closed = detect_changes(old_stores, new_stores)
    print(f"🆕 新規開店: {len(opened)} 店")
    for s in opened[:10]:
        print(f"   + {s['jp']} ({s.get('pref', '')})")
    if len(opened) > 10:
        print(f"   ... 他 {len(opened) - 10} 店")
    print(f"🔚 閉店:     {len(closed)} 店")
    for s in closed[:10]:
        print(f"   - {s['jp']} ({s.get('pref', '')})")
    if len(closed) > 10:
        print(f"   ... 他 {len(closed) - 10} 店")

    # マージ (順序保持)
    merged = merge_stores(old_stores, new_stores)
    print(f"📋 マージ後の店舗数: {len(merged)}")

    if not opened and not closed:
        # 内容更新だけのケース (営業時間や住所の変更など) は HTML 更新する
        # ただし全件比較して何も変わってないなら省略
        if merged == old_stores:
            print("✨ 変更なし。HTML 更新をスキップします。")
            return
        else:
            print("📝 店舗情報の細かい更新を反映します。")

    new_news = build_news(opened, closed, merged, old_news)
    print("📝 index.html を更新中...")
    new_html = update_html(html, merged, new_news)
    INDEX_HTML.write_text(new_html, encoding="utf-8")
    print("✅ 完了")


if __name__ == "__main__":
    main()
