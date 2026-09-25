#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 OpenAlex 抓取真实文献数据，用于验证管线在真实数据上的表现。

OpenAlex 是开放学术数据库，免密钥、免注册。
文档：https://docs.openalex.org/

用途：合成数据太干净，测不出真实数据的问题（同义词变体、中英混杂、字段缺失）。
本脚本拉真实文献，导出成管线可读的 CSV。

用法：
    python fetch_openalex.py --query "ambient assisted living older adults" \
                             --limit 300 --out realdata.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.openalex.org/works"
MAILTO = "bibliometric-tool@example.com"


def _get(url: str, retries: int = 3) -> dict:
    """带重试的 GET。"""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "vosviewer-bib/1.0"})
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"请求失败：{url} → {last}")


def abstract_from_inverted(idx: dict | None) -> str:
    """OpenAlex 用倒排索引存摘要，这里还原成正常文本。"""
    if not idx:
        return ""
    positions: list[tuple[int, str]] = []
    for word, pos_list in idx.items():
        for p in pos_list:
            positions.append((p, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def fetch(query: str, limit: int, per_page: int = 200,
          filter_field: str = "title_and_abstract.search") -> list[dict]:
    """分页抓取文献。"""
    works: list[dict] = []
    cursor = "*"
    fetched = 0

    while fetched < limit:
        n = min(per_page, limit - fetched)
        params = {
            "filter": f"{filter_field}:{query}",
            "per-page": str(n),
            "cursor": cursor,
            "mailto": MAILTO,
            "select": ("id,doi,title,publication_year,cited_by_count,"
                       "authorships,keywords,abstract_inverted_index,"
                       "primary_location,type"),
        }
        url = f"{API}?{urllib.parse.urlencode(params)}"
        data = _get(url)

        results = data.get("results") or []
        if not results:
            break

        works.extend(results)
        fetched += len(results)
        print(f"  已抓取 {fetched} 条……", flush=True)

        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.4)   # 对公共 API 友好一点

    return works


def to_rows(works: list[dict]) -> list[dict]:
    """把 OpenAlex 记录转成管线可读的行。"""
    rows: list[dict] = []
    for w in works:
        # 关键词：OpenAlex 的 keywords 是算法抽取的，用 display_name
        kws: list[str] = []
        for k in (w.get("keywords") or []):
            name = (k.get("display_name") or "").strip()
            if name:
                kws.append(name)

        authors = []
        for a in (w.get("authorships") or []):
            nm = ((a.get("author") or {}).get("display_name") or "").strip()
            if nm:
                authors.append(nm)

        loc = w.get("primary_location") or {}
        src = ((loc.get("source") or {}).get("display_name") or "").strip()

        rows.append({
            "Title": (w.get("title") or "").strip(),
            "Authors": "; ".join(authors),
            "Source title": src,
            "Year": w.get("publication_year") or "",
            "Cited by": w.get("cited_by_count") or 0,
            "DOI": (w.get("doi") or "").replace("https://doi.org/", ""),
            "Author Keywords": "; ".join(kws),
            "Abstract": abstract_from_inverted(w.get("abstract_inverted_index")),
            "OpenAlex ID": (w.get("id") or "").replace("https://openalex.org/", ""),
        })
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description="从 OpenAlex 抓取真实文献数据")
    p.add_argument("--query", "-q", required=True, help="检索式（英文）")
    p.add_argument("--limit", "-n", type=int, default=300, help="最多抓多少条")
    p.add_argument("--out", "-o", required=True, help="输出 CSV 路径")
    p.add_argument("--field", default="title_and_abstract.search",
                   choices=["title_and_abstract.search", "title.search",
                            "fulltext.search", "default.search"],
                   help="检索字段（默认标题+摘要）")
    args = p.parse_args()

    print(f"检索式：{args.query}")
    print(f"检索字段：{args.field}")
    print(f"目标数量：{args.limit}")
    print()

    works = fetch(args.query, args.limit, filter_field=args.field)
    if not works:
        print("没抓到任何文献。换个检索式试试。")
        return 1

    rows = to_rows(works)
    with_kw = sum(1 for r in rows if r["Author Keywords"])

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print()
    print(f"已写出：{args.out}")
    print(f"  文献总数：{len(rows)}")
    print(f"  带关键词：{with_kw}（{with_kw / len(rows) * 100:.1f}%）")
    print(f"  年份范围：{min((r['Year'] for r in rows if r['Year']), default='-')}"
          f" – {max((r['Year'] for r in rows if r['Year']), default='-')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
