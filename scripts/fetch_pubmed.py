#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 PubMed 抓取真实文献数据（含**作者关键词**）。

为什么用 PubMed：
  OpenAlex 的 keywords 是算法抽取的概念（混学科标签，不适合共现分析），
  PubMed 的 KeywordList 是**作者亲自标注的关键词**，还有 MeSH 主题词，
  两者都可用于文献计量分析。免密钥、免注册。

用法：
    python fetch_pubmed.py --query "ambient assisted living" --limit 300 \
                           --source author --out pubmed.csv
    python fetch_pubmed.py --query "dementia AND smart home" --limit 200 \
                           --source both --out pubmed.csv

--source 可选：
    author  只用作者关键词（推荐，等同 WoS 的 DE 字段）
    mesh    只用 MeSH 主题词（医学主题词表，人工标引）
    both    两者合并
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TOOL = "vosviewer-bibliometric"
EMAIL = "bibliometric-tool@example.com"


def _get(url: str, retries: int = 3) -> bytes:
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": f"{TOOL}/1.0"})
            with urllib.request.urlopen(req, timeout=90, context=ctx) as resp:
                return resp.read()
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"请求失败：{url[:120]}… → {last}")


def esearch(query: str, limit: int) -> list[str]:
    """检索并返回 PMID 列表。"""
    params = {
        "db": "pubmed", "term": query, "retmax": str(limit),
        "retmode": "json", "tool": TOOL, "email": EMAIL,
    }
    raw = _get(f"{ESEARCH}?{urllib.parse.urlencode(params)}")
    data = json.loads(raw.decode("utf-8"))
    return (data.get("esearchresult") or {}).get("idlist") or []


def efetch(pmids: list[str]) -> str:
    """批量取回 XML。"""
    params = {
        "db": "pubmed", "id": ",".join(pmids), "retmode": "xml",
        "tool": TOOL, "email": EMAIL,
    }
    return _get(f"{EFETCH}?{urllib.parse.urlencode(params)}").decode(
        "utf-8", errors="replace")


def _text(node, path: str) -> str:
    el = node.find(path)
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def parse_articles(xml_text: str, source: str) -> list[dict]:
    """解析 PubMed XML，抽出题录与关键词。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  XML 解析失败：{e}")
        return []

    rows: list[dict] = []
    for art in root.iter("PubmedArticle"):
        cit = art.find("MedlineCitation")
        if cit is None:
            continue

        pmid = _text(cit, "PMID")
        article = cit.find("Article")
        if article is None:
            continue

        title = _text(article, "ArticleTitle")
        journal = _text(article, "Journal/Title")

        # 年份：优先 Year，退化到 MedlineDate 里抠
        year = _text(article, "Journal/JournalIssue/PubDate/Year")
        if not year:
            md = _text(article, "Journal/JournalIssue/PubDate/MedlineDate")
            m = re.search(r"\b(19|20)\d{2}\b", md)
            year = m.group(0) if m else ""

        # 摘要
        abstract = " ".join(
            "".join(a.itertext()).strip()
            for a in article.findall("Abstract/AbstractText")
        ).strip()

        # 作者
        authors: list[str] = []
        for a in article.findall("AuthorList/Author"):
            ln = _text(a, "LastName")
            fn = _text(a, "ForeName")
            cn = _text(a, "CollectiveName")
            if ln:
                authors.append(f"{ln} {fn}".strip())
            elif cn:
                authors.append(cn)

        # 作者关键词
        author_kw = [
            "".join(k.itertext()).strip()
            for k in cit.findall("KeywordList/Keyword")
        ]

        # MeSH 主题词
        mesh_kw = [
            "".join(d.itertext()).strip()
            for d in cit.findall("MeshHeadingList/MeshHeading/DescriptorName")
        ]

        if source == "author":
            kws = author_kw
        elif source == "mesh":
            kws = mesh_kw
        else:
            kws = author_kw + mesh_kw

        # DOI
        doi = ""
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = "".join(aid.itertext()).strip()
                break

        rows.append({
            "Title": title,
            "Authors": "; ".join(authors),
            "Source title": journal,
            "Year": year,
            "DOI": doi,
            "Author Keywords": "; ".join(k for k in kws if k),
            "Abstract": abstract,
            "PMID": pmid,
        })
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description="从 PubMed 抓取真实文献（含作者关键词）")
    p.add_argument("--query", "-q", required=True, help="PubMed 检索式")
    p.add_argument("--limit", "-n", type=int, default=300, help="最多抓多少条")
    p.add_argument("--out", "-o", required=True, help="输出 CSV 路径")
    p.add_argument("--source", default="author",
                   choices=["author", "mesh", "both"],
                   help="关键词来源（默认 author=作者关键词）")
    p.add_argument("--batch", type=int, default=200, help="每批抓取数量")
    args = p.parse_args()

    print(f"检索式  ：{args.query}")
    print(f"关键词源：{args.source}")
    print(f"目标数量：{args.limit}")
    print()

    pmids = esearch(args.query, args.limit)
    if not pmids:
        print("没检索到文献。换个检索式试试。")
        return 1
    print(f"检索到 {len(pmids)} 条，开始抓取详情……")

    all_rows: list[dict] = []
    for i in range(0, len(pmids), args.batch):
        batch = pmids[i:i + args.batch]
        xml_text = efetch(batch)
        rows = parse_articles(xml_text, args.source)
        all_rows.extend(rows)
        print(f"  已解析 {len(all_rows)} 条……", flush=True)
        time.sleep(0.4)   # 对公共 API 友好

    if not all_rows:
        print("抓到了 PMID 但没解析出记录。")
        return 1

    with_kw = sum(1 for r in all_rows if r["Author Keywords"])
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
        writer.writeheader()
        writer.writerows(all_rows)

    years = [r["Year"] for r in all_rows if r["Year"]]
    print()
    print(f"已写出：{args.out}")
    print(f"  文献总数：{len(all_rows)}")
    print(f"  带关键词：{with_kw}（{with_kw / len(all_rows) * 100:.1f}%）")
    if years:
        print(f"  年份范围：{min(years)} – {max(years)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
