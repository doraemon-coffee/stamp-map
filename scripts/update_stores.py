#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
スタバ全国店舗データを取得して index.html を更新するスクリプト

- スタバ公式の店舗検索 API (AWS CloudSearch) から全店舗データを取得
- 既存の index.html 内の STORES 配列・NEWS 配列を抽出
- 新規開店・閉店を検出
- index.html を更新して書き戻す
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
PAGE_SIZE = 100  # 1リクエストあたりの取得件数 (最大100)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja",
    "Origin": "https://store.starbucks.co.jp",
    "Referer": "https://store.starbucks.co.jp/",
}
INDEX_HTML = Path("index.html")
NEWS_RETENTION_DAYS = 90  # お知らせを残す日数


def fetch_page(start: int) -> dict:
    """API を叩いて 1 ページ分の店舗データを取得する"""
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
    """全店舗データをページネーションで取得する"""
    print("📡 スタバ API から全店舗データを取得中...")
    first = fetch_page(0)
    total = first["hits"]["found"]
    print(f"   全店舗数: {total}")

    all_hits = list(first["hits"]["hit"])
    start = PAGE_SIZE
    while start < total:
        time.sleep(0.5)  # サーバー負荷軽減
        page = fetch_page(start)
        all_hits.extend(page["hits"]["hit"])
        print(f"   取得中... {len(all_hits)}/{total}")
        start += PAGE_SIZE

    print(f"✅ 全 {len(all_hits)} 件取得完了")
    return all_hits


def normalize_store(hit: dict) -> dict:
    """API レスポンスの 1 店舗を、index.html で使う形式に変換する"""
    f = hit["fields"]

    def get(k, default=""):
        v = f.get(k, default)
        if isinstance(v, list):
            v = v[0] if v else default
        return v

    # 緯度経度
    try:
        lat = float(get("location_jp_latitude") or get("latitude") or 0)
        lng = float(get("location_jp_longitude") or get("longitude") or 0)
    except (TypeError, ValueError):
        lat = lng = 0.0

    return {
        "id": str(hit.get("id", "")),
        "jp": get("name", "").strip(),
        "en": get("name_en", "").strip(),
        "pref": get("pref_name_jp") or get("address_1", "").strip(),
        "city": get("address_2", "").strip(),
        "lat": round(lat, 6),
        "lng": round(lng, 6),
    }


def extract_array(html: str, var_name: str) -> tuple[str, int, int]:
    """
    HTML の中の `const VAR=[...]` または `const VAR = [\n...\n]` を抽出する。
    戻り値: (array_text, start_index, end_index)
    """
    # `const STORES=[` または `const NEWS = [` のような開始パターンを検索
    pattern = re.compile(r"const\s+" + re.escape(var_name) + r"\s*=\s*\[")
    m = pattern.search(html)
    if not m:
        raise ValueError(f"配列 {var_name} が見つかりません")

    # マッチ後ろから対応する `]` を探す (ネストしたカッコを考慮)
    start = m.end() - 1  # `[` の位置
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


def parse_stores_from_html(html: str) -> list[dict]:
    """index.html から STORES 配列を抽出してパースする"""
    arr_text, _, _ = extract_array(html, "STORES")
    # JS の Array リテラルは JSON とほぼ同じ書き方をしているはず
    return json.loads(arr_text)


def parse_news_from_html(html: str) -> list[dict]:
    """index.html から NEWS 配列を抽出してパースする (空 or 既存)"""
    try:
        arr_text, _, _ = extract_array(html, "NEWS")
    except ValueError:
        return []
    # NEWS 配列はコメントが入っているので、JSON.parse は使えない
    # オブジェクトリテラルだけ拾う簡易パース
    items = []
    # `{ ... }` の塊を抽出する正規表現
    obj_pattern = re.compile(r"\{[^{}]*\}", re.DOTALL)
    for obj_match in obj_pattern.finditer(arr_text):
        obj_text = obj_match.group(0)
        # JS の {key: value} を JSON にざっくり変換
        try:
            # キーをダブルクォートで囲む (date:, type: などを "date":, "type": にする)
            json_text = re.sub(r"(\w+):", r'"\1":', obj_text)
            # シングルクォートをダブルクォートに
            json_text = json_text.replace("'", '"')
            # 末尾の不要なカンマを削除
            json_text = re.sub(r",\s*}", "}", json_text)
            items.append(json.loads(json_text))
        except json.JSONDecodeError:
            continue
    return items


def detect_changes(old_stores: list[dict], new_stores: list[dict]) -> tuple[list[dict], list[dict]]:
    """新旧店舗を比較して、新規開店・閉店を返す"""
    old_ids = {s["id"]: s for s in old_stores if s.get("id")}
    new_ids = {s["id"]: s for s in new_stores if s.get("id")}

    opened = [new_ids[i] for i in new_ids if i not in old_ids]
    closed = [old_ids[i] for i in old_ids if i not in new_ids]
    return opened, closed


def build_news(opened: list[dict], closed: list[dict], new_stores: list[dict], old_news: list[dict]) -> list[dict]:
    """新しい NEWS 配列を構築する"""
    today = date.today().isoformat()
    news = []

    # 新規開店
    for s in opened:
        # 新ストア配列での位置 (storeIdx) を求める
        idx = next((i for i, t in enumerate(new_stores) if t["id"] == s["id"]), None)
        entry = {
            "date": today,
            "type": "open",
            "store": s["jp"],
            "pref": s.get("pref", ""),
        }
        if idx is not None:
            entry["storeIdx"] = idx
        news.append(entry)

    # 閉店
    for s in closed:
        news.append({
            "date": today,
            "type": "close",
            "store": s["jp"],
            "pref": s.get("pref", ""),
        })

    # 既存ニュースを保持しつつ古すぎるものを除外
    cutoff = (date.today().toordinal() - NEWS_RETENTION_DAYS)
    for n in old_news:
        try:
            d = date.fromisoformat(n.get("date", "")).toordinal()
        except ValueError:
            continue
        if d >= cutoff:
            news.append(n)

    # 新規ストアの storeIdx を最新配列でも更新する (既存ニュースの storeIdx は店舗順が変わっていると壊れる)
    id_to_idx = {s["id"]: i for i, s in enumerate(new_stores)}
    for n in news:
        if n.get("type") == "open":
            store_name = n.get("store", "")
            for i, s in enumerate(new_stores):
                if s["jp"] == store_name:
                    n["storeIdx"] = i
                    break

    return news


def render_stores_array(stores: list[dict]) -> str:
    """STORES 配列を JS の文字列に変換する (1行1店舗で読みやすく)"""
    lines = ["["]
    for i, s in enumerate(stores):
        comma = "," if i < len(stores) - 1 else ""
        # JSON で出力するが、key を quote しない (元のフォーマット維持)
        item_json = json.dumps(s, ensure_ascii=False)
        lines.append(item_json + comma)
    lines.append("]")
    return "\n".join(lines)


def render_news_array(news: list[dict]) -> str:
    """NEWS 配列を JS の文字列に変換する"""
    if not news:
        return (
            "[\n"
            "  // 例: {date:'2026-05-01', type:'open', store:'スターバックス 渋谷○○店', pref:'東京都', storeIdx:2118},\n"
            "  // 例: {date:'2026-04-30', type:'close', store:'スターバックス △△店', pref:'大阪府'},\n"
            "]"
        )
    lines = ["["]
    for n in news:
        item_json = json.dumps(n, ensure_ascii=False)
        lines.append("  " + item_json + ",")
    lines.append("]")
    return "\n".join(lines)


def update_html(html: str, new_stores: list[dict], new_news: list[dict]) -> str:
    """HTML 内の STORES 配列と NEWS 配列を置換する"""
    # NEWS を先に置換 (位置がずれない方の小さい変更から)
    _, news_start, news_end = extract_array(html, "NEWS")
    html = html[:news_start] + render_news_array(new_news) + html[news_end:]

    # 次に STORES (再抽出が必要)
    _, stores_start, stores_end = extract_array(html, "STORES")
    html = html[:stores_start] + render_stores_array(new_stores) + html[stores_end:]

    # 店舗数バッジも更新 ("2,119店舗" の数字部分)
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

    # 1. 既存 HTML 読み込み
    print(f"📄 {INDEX_HTML} を読み込み中...")
    html = INDEX_HTML.read_text(encoding="utf-8")
    old_stores = parse_stores_from_html(html)
    old_news = parse_news_from_html(html)
    print(f"   現在の店舗数: {len(old_stores)}")
    print(f"   現在のお知らせ件数: {len(old_news)}")

    # 2. 最新データ取得
    raw = fetch_all_stores()
    new_stores = [normalize_store(h) for h in raw]
    # 不正データ (緯度経度0や空名) を除外
    new_stores = [s for s in new_stores if s["jp"] and s["lat"] and s["lng"]]
    print(f"   有効な店舗数: {len(new_stores)}")

    # 3. 差分検出
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

    # 4. NEWS 配列を構築
    new_news = build_news(opened, closed, new_stores, old_news)

    # 5. HTML 更新
    print("📝 index.html を更新中...")
    new_html = update_html(html, new_stores, new_news)
    INDEX_HTML.write_text(new_html, encoding="utf-8")
    print("✅ 完了")


if __name__ == "__main__":
    main()
