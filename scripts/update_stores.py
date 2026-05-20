#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
スタバ全国店舗データを取得して stores.json / news.json を更新するスクリプト (v5)

v5 の変更点 (2026年):
- 店舗データを index.html から外部ファイル (stores.json) に分離したため、HTML 直接書き換えを廃止
- NEWS も news.json に分離
- index.html は店舗数バッジ部分のみ更新
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
STORES_JSON = Path("stores.json")
NEWS_JSON = Path("news.json")
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
    v = fields.get(key, default)
    if isinstance(v, list):
        return v[0] if v else default
    return v


def parse_location(loc_str) -> tuple[float, float]:
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
    bd = str(get_first(f, "business_day_mon_thu", "")).strip()
    if bd:
        return bd.replace("〜", "～")
    mo = str(get_first(f, "mon_open", "")).strip()
    mc = str(get_first(f, "mon_close", "")).strip()
    if mo and mc:
        return f"{mo}～{mc}"
    return ""


def normalize_store(hit: dict) -> dict:
    f = hit.get("fields", {})
    loc = get_first(f, "location_jp") or get_first(f, "location") or ""
    lat, lng = parse_location(loc)
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
# 既存 JSON 読み込み
# ====================================================================
def load_stores() -> list[dict]:
    if not STORES_JSON.exists():
        print(f"⚠️ {STORES_JSON} が見つかりません。新規作成します")
        return []
    return json.loads(STORES_JSON.read_text(encoding="utf-8"))


def load_news() -> list[dict]:
    if not NEWS_JSON.exists():
        return []
    try:
        return json.loads(NEWS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return []


# ====================================================================
# 差分検出 & マージ
# ====================================================================
def detect_changes(old_stores, new_stores):
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
    for old_s in old_stores:
        jp = old_s.get("jp", "")
        if jp and jp in new_by_jp:
            updated = dict(new_by_jp[jp])
            if not updated.get("n") and old_s.get("n"):
                updated["n"] = old_s["n"]
            merged.append(updated)
    for new_s in new_stores:
        jp = new_s.get("jp", "")
        if jp and jp not in old_jps:
            merged.append(new_s)
    return merged


def build_news(opened, closed, merged_stores, old_news):
    today = date.today().isoformat()
    news = []
    for s in opened:
        idx = next((i for i, t in enumerate(merged_stores) if t.get("jp") == s["jp"]), None)
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
            for i, s in enumerate(merged_stores):
                if s.get("jp") == store_name:
                    n["storeIdx"] = i
                    break
    return news


# ====================================================================
# 出力
# ====================================================================
def write_stores(stores):
    """JSON配列を1店舗1行で出力 (Gitのdiffを見やすく)"""
    lines = ["["]
    for i, s in enumerate(stores):
        comma = "," if i < len(stores) - 1 else ""
        lines.append(json.dumps(s, ensure_ascii=False) + comma)
    lines.append("]")
    STORES_JSON.write_text("\n".join(lines), encoding="utf-8")


def write_news(news):
    if not news:
        NEWS_JSON.write_text("[]", encoding="utf-8")
        return
    lines = ["["]
    for i, n in enumerate(news):
        comma = "," if i < len(news) - 1 else ""
        lines.append("  " + json.dumps(n, ensure_ascii=False) + comma)
    lines.append("]")
    NEWS_JSON.write_text("\n".join(lines), encoding="utf-8")


def update_count_badge(count: int):
    """index.html の店舗数バッジだけ更新"""
    if not INDEX_HTML.exists():
        print(f"⚠️ {INDEX_HTML} が見つかりません。バッジ更新をスキップ")
        return
    html = INDEX_HTML.read_text(encoding="utf-8")
    count_str = f"{count:,}"
    new_html = re.sub(
        r'(id="count-badge"[^>]*>)[\d,]+店舗(</span>)',
        rf"\g<1>{count_str}店舗\g<2>",
        html,
    )
    if new_html != html:
        INDEX_HTML.write_text(new_html, encoding="utf-8")
        print(f"📛 count-badge を {count_str}店舗 に更新")


# ====================================================================
# メイン
# ====================================================================
def main():
    print(f"📄 既存データを読み込み中...")
    old_stores = load_stores()
    old_news = load_news()
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
        print("⚠️ 有効な店舗が極端に少ないため、更新を中止します。")
        sys.exit(0)

    new_stores = valid

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

    merged = merge_stores(old_stores, new_stores)
    print(f"📋 マージ後の店舗数: {len(merged)}")

    if not opened and not closed and merged == old_stores:
        print("✨ 変更なし。更新をスキップします。")
        return

    new_news = build_news(opened, closed, merged, old_news)

    print("📝 stores.json を更新中...")
    write_stores(merged)
    print("📝 news.json を更新中...")
    write_news(new_news)
    update_count_badge(len(merged))
    print("✅ 完了")


if __name__ == "__main__":
    main()
