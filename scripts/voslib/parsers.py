"""文献题录解析器：把各种数据库导出文件统一成 Record 列表。

支持格式：
  - Web of Science 制表符分隔文件（savedrecs.txt）
  - Web of Science 纯文本全记录
  - Scopus CSV 导出
  - CNKI RefWorks 格式（K1 关键词）
  - CNKI EndNote 格式（%K 关键词）
  - RIS 格式
  - BibTeX 格式
  - 通用 CSV / Excel（自动识别关键词列）
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from typing import Iterable


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Record:
    """一条文献题录。"""
    title: str = ""
    year: str = ""
    source: str = ""
    doi: str = ""
    authors: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)        # 作者关键词
    keywords_plus: list[str] = field(default_factory=list)   # WoS 扩展关键词
    abstract: str = ""
    citations: int | None = None
    references: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def terms(self, source: str = "keywords") -> list[str]:
        """按配置取出用于共现分析的关键词列表。"""
        if source == "keywords":
            return list(self.keywords)
        if source == "keywords_plus":
            return list(self.keywords_plus)
        if source == "both":
            return list(self.keywords) + list(self.keywords_plus)
        return list(self.keywords)


# --------------------------------------------------------------------------
# 编码探测
# --------------------------------------------------------------------------

def read_text(path: str) -> str:
    """读取文本文件，自动尝试常见中文编码。"""
    with open(path, "rb") as fh:
        raw = fh.read()

    # BOM 优先
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le").lstrip("\ufeff")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be").lstrip("\ufeff")

    for enc in ("utf-8", "gb18030", "gbk", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

_MULTI_SEP = re.compile(r"\s*[;；]\s*")


def split_multi(value: str) -> list[str]:
    """切分多值字段（作者、关键词等），支持中英文分号。"""
    if not value:
        return []
    parts = _MULTI_SEP.split(value)
    return [p.strip() for p in parts if p and p.strip()]


def _balance_parens(s: str) -> str:
    """保证括号配平：丢弃无法配对的右括号，从后往前删掉未闭合的左括号。

    单靠正则处理嵌套场景会失效（如 "((a)"）。
    """
    out: list[str] = []
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
            out.append(ch)
        elif ch == ")":
            if depth > 0:
                depth -= 1
                out.append(ch)
            # 多余的右括号直接丢弃
        else:
            out.append(ch)
    if depth == 0:
        return "".join(out)

    # 还有 depth 个未闭合的左括号：从后往前删
    res: list[str] = []
    to_drop = depth
    for ch in reversed(out):
        if ch == "(" and to_drop > 0:
            to_drop -= 1
            continue
        res.append(ch)
    return "".join(reversed(res))


def clean_term(term: str) -> str:
    """关键词清洗：去首尾空白、压缩内部空白、修括号、去结尾标点。

    真实数据里常见的脏东西：
      - "Context (archaeology)" 这类带限定语的概念名（OpenAlex 常见）
      - "Context (archaeology" 括号未闭合（截断 bug）
      - "Human–computer interaction" 用了连接号而非连字符
      - 结尾多余标点

    处理顺序很关键：**先修括号平衡，最后才去首尾标点**。
    反过来做的话，"Service (business)" 的右括号会被当成尾部标点删掉，
    变成 "Service (business"——反而制造出括号异常。
    """
    if not term:
        return ""
    t = term.strip()

    # 1) 统一各种连接号/破折号为普通连字符
    t = t.replace("\u2010", "-").replace("\u2011", "-").replace("\u2012", "-")
    t = t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")

    # 2) 先去掉首尾的纯标点与空白（**不含括号**）
    t = t.strip(" \t\r\n\"'.,;:!?，。；：！？、")

    # 3) 整体被括号包裹时脱掉外层括号，如 "(smart home)"
    if len(t) > 2 and t.startswith("(") and t.endswith(")"):
        inner = t[1:-1].strip()
        if inner.count("(") == inner.count(")"):
            t = inner

    # 4) 修括号平衡
    if t.count("(") != t.count(")"):
        # 优先尝试「去掉末尾未闭合的限定语」，这最符合 OpenAlex 的截断形态
        stripped = re.sub(r"\s*\([^)]*$", "", t).strip()
        if stripped and stripped.count("(") == stripped.count(")"):
            t = stripped
        else:
            # 兜底：通用配平（处理嵌套等复杂情况）
            t = _balance_parens(t)

    # 5) 最后再清一次首尾标点与空白
    t = t.strip(" \t\r\n\"'.,;:!?，。；：！？、")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# --------------------------------------------------------------------------
# Web of Science 制表符分隔 / 纯文本
# --------------------------------------------------------------------------

def parse_wos(text: str) -> list[Record]:
    """解析 WoS 导出文件（制表符分隔或纯文本全记录）。

    两种形态的区别只在字段的分隔方式，字段代码是一致的（AU/TI/DE/ID/...）。
    """
    records: list[Record] = []

    # 形态一：制表符分隔（FN/VR 头 + 每个字段一行，代码与内容以制表符分隔）
    is_tabbed = bool(re.search(r"^FN\s+\S", text, re.M)) or "\t" in text.split("\n")[0]
    if is_tabbed and re.search(r"^PT\s", text, re.M):
        return _parse_wos_tabbed(text)

    # 形态二：纯文本，字段代码 + 空格，续行以 3 空格缩进
    return _parse_wos_plain(text)


def _finalize_wos(fields: dict, records: list[Record]) -> None:
    """把累积的字段字典转成 Record。"""
    def get(*keys: str) -> str:
        for k in keys:
            if k in fields and fields[k]:
                return fields[k]
        return ""

    year = ""
    m = re.search(r"\b(19|20)\d{2}\b", get("PY", "PD", "Y1"))
    if m:
        year = m.group(0)

    tc = get("TC", "Z9")
    citations = int(re.sub(r"\D", "", tc)) if re.search(r"\d", tc) else None

    records.append(Record(
        title=get("TI", "T1").strip(),
        year=year,
        source=get("SO", "J9", "JO", "T2").strip(),
        doi=get("DI", "DO").strip(),
        authors=split_multi(get("AU", "A1")),
        keywords=[clean_term(k) for k in split_multi(get("DE", "K1")) if clean_term(k)],
        keywords_plus=[clean_term(k) for k in split_multi(get("ID")) if clean_term(k)],
        abstract=get("AB", "N2").strip(),
        citations=citations,
        references=split_multi(get("CR")),
        raw=dict(fields),
    ))


def _parse_wos_tabbed(text: str) -> list[Record]:
    records: list[Record] = []
    fields: dict = {}
    lines = text.splitlines()

    # 跳过 FN / VR 文件头
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("PT "):
            start = i
            break

    for line in lines[start:]:
        if not line.strip():
            continue
        if line.startswith("ER"):
            if fields:
                _finalize_wos(fields, records)
            fields = {}
            continue
        if line.startswith("EF"):
            break
        if "\t" in line:
            code, _, value = line.partition("\t")
        else:
            code, _, value = line.partition(" ")
        code = code.strip()
        value = value.strip()
        if not code or len(code) > 3:
            continue
        # 多值字段：同一代码重复出现时累加
        if code in fields:
            fields[code] = fields[code] + "; " + value
        else:
            fields[code] = value

    if fields:
        _finalize_wos(fields, records)
    return records


def _parse_wos_plain(text: str) -> list[Record]:
    records: list[Record] = []
    fields: dict = {}
    current: str | None = None

    for line in text.splitlines():
        if not line.strip():
            continue
        if re.match(r"^EF\b", line):
            break
        if re.match(r"^ER\b", line):
            if fields:
                _finalize_wos(fields, records)
            fields = {}
            current = None
            continue

        m = re.match(r"^([A-Z][A-Z0-9])\s(.*)$", line)
        if m:
            code, value = m.group(1), m.group(2).strip()
            current = code
            fields[code] = (fields[code] + "; " + value) if code in fields else value
        elif line.startswith("   ") and current:
            fields[current] = fields[current] + " " + line.strip()

    if fields:
        _finalize_wos(fields, records)
    return records


# --------------------------------------------------------------------------
# Scopus CSV
# --------------------------------------------------------------------------

def parse_scopus(text: str) -> list[Record]:
    records: list[Record] = []
    reader = csv.DictReader(io.StringIO(text))

    def pick(row: dict, *names: str) -> str:
        for n in names:
            for k in row:
                if k and k.strip().lower() == n.lower():
                    return (row[k] or "").strip()
        return ""

    for row in reader:
        kw = split_multi(pick(row, "Author Keywords"))
        kp = split_multi(pick(row, "Index Keywords"))
        year_raw = pick(row, "Year")
        m = re.search(r"\b(19|20)\d{2}\b", year_raw)
        cit = pick(row, "Cited by")
        records.append(Record(
            title=pick(row, "Title"),
            year=m.group(0) if m else "",
            source=pick(row, "Source title", "Source"),
            doi=pick(row, "DOI"),
            authors=split_multi(pick(row, "Authors")),
            keywords=[clean_term(k) for k in kw if clean_term(k)],
            keywords_plus=[clean_term(k) for k in kp if clean_term(k)],
            abstract=pick(row, "Abstract"),
            citations=int(re.sub(r"\D", "", cit)) if re.search(r"\d", cit) else None,
            raw=dict(row),
        ))
    return records


# --------------------------------------------------------------------------
# CNKI：RefWorks / EndNote
# --------------------------------------------------------------------------

_CNKI_RW_MAP = {
    "T1": "title", "TI": "title",
    "YR": "year", "PY": "year", "DA": "year",
    "JF": "source", "JO": "source", "T2": "source",
    "K1": "keywords", "DE": "keywords",
    "AB": "abstract", "N2": "abstract",
    "A1": "authors", "AU": "authors",
}


def parse_cnki_refworks(text: str) -> list[Record]:
    """CNKI 的 RefWorks 导出格式（字段代码 + 空格，如 K1 关键词1;关键词2）。

    ⚠️ 实测要点：**CNKI 的 RefWorks 导出不带 `ER` 记录结束标记**，
    记录之间靠「空行 + 新的 `RT`」分隔。早期版本只等 `ER`，
    结果把几百条记录全部并成一条（真实数据实测踩到，见工作日志 bug #7）。
    所以这里必须同时支持两种切分方式：
      1. 遇到 `RT` 且已有累积字段 → 切分（CNKI 实际格式）
      2. 遇到 `ER` → 切分（兼容其他工具导出的 RefWorks 变体）
    """
    records: list[Record] = []
    fields: dict = {}
    current: str | None = None

    def flush() -> None:
        if not fields:
            return
        def g(key: str) -> str:
            return fields.get(key, "")

        year = ""
        m = re.search(r"\b(19|20)\d{2}\b", g("year"))
        if m:
            year = m.group(0)
        records.append(Record(
            title=g("title"),
            year=year,
            source=g("source"),
            authors=split_multi(g("authors")),
            keywords=[clean_term(k) for k in split_multi(g("keywords")) if clean_term(k)],
            abstract=g("abstract"),
            raw=dict(fields),
        ))

    for line in text.splitlines():
        if not line.strip():
            continue

        m = re.match(r"^([A-Za-z][A-Za-z0-9])\s+(.*)$", line)
        if not m:
            # 缩进续行，接到上一个字段
            if current and (line.startswith("  ") or line.startswith("\t")):
                fields[current] = fields[current] + " " + line.strip()
            continue

        tag = m.group(1).upper()
        value = m.group(2).strip()

        # 记录边界：新 RT 开始，或遇到 ER
        if tag == "RT":
            if fields:
                flush()
            fields = {}
            current = None
            continue
        if tag == "ER":
            flush()
            fields = {}
            current = None
            continue

        if tag in _CNKI_RW_MAP:
            code = _CNKI_RW_MAP[tag]
            current = code
            fields[code] = (fields[code] + "; " + value) if code in fields else value
        # 其他字段（SN/CN/LA/DS/LK/DO/IS/vo/OP 等）忽略，不影响分析

    flush()
    return records


_CNKI_EN_MAP = {
    "%T": "title", "%K": "keywords", "%X": "abstract",
    "%D": "year", "%J": "source", "%A": "authors", "%U": "doi",
}


def parse_cnki_endnote(text: str) -> list[Record]:
    """CNKI 的 EndNote 导出格式（%T 标题 / %K 关键词）。"""
    records: list[Record] = []
    fields: dict = {}

    def flush() -> None:
        if not fields:
            return
        def g(key: str) -> str:
            return fields.get(key, "")
        year = ""
        m = re.search(r"\b(19|20)\d{2}\b", g("year"))
        if m:
            year = m.group(0)
        records.append(Record(
            title=g("title"),
            year=year,
            source=g("source"),
            authors=split_multi(g("authors")),
            keywords=[clean_term(k) for k in split_multi(g("keywords")) if clean_term(k)],
            abstract=g("abstract"),
            raw=dict(fields),
        ))

    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("%0"):
            flush()
            fields = {}
            continue
        if len(line) >= 3 and line[:2] in _CNKI_EN_MAP:
            code = _CNKI_EN_MAP[line[:2]]
            value = line[3:].strip()
            fields[code] = (fields[code] + "; " + value) if code in fields else value

    flush()
    return records


# --------------------------------------------------------------------------
# RIS
# --------------------------------------------------------------------------

def parse_ris(text: str) -> list[Record]:
    records: list[Record] = []
    fields: dict[str, list[str]] = {}

    def flush() -> None:
        if not fields:
            return
        def one(tag: str) -> str:
            return (fields.get(tag) or [""])[0]
        def many(tag: str) -> list[str]:
            return fields.get(tag, [])

        year = ""
        m = re.search(r"\b(19|20)\d{2}\b", one("PY") or one("Y1") or one("DA"))
        if m:
            year = m.group(0)
        kw = [clean_term(k) for k in many("KW") if clean_term(k)]
        records.append(Record(
            title=one("TI") or one("T1"),
            year=year,
            source=one("JO") or one("JF") or one("T2"),
            doi=one("DO"),
            authors=many("AU"),
            keywords=kw,
            abstract=one("AB") or one("N2"),
            raw={k: v for k, v in fields.items()},
        ))

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Z][A-Z0-9])\s{2}-\s?(.*)$", line)
        if not m:
            continue
        tag, value = m.group(1), m.group(2).strip()
        if tag == "ER":
            flush()
            fields = {}
            continue
        fields.setdefault(tag, []).append(value)

    flush()
    return records


# --------------------------------------------------------------------------
# BibTeX
# --------------------------------------------------------------------------

def parse_bibtex(text: str) -> list[Record]:
    records: list[Record] = []
    for chunk in re.split(r"@\w+\s*\{", text)[1:]:
        body = chunk.rsplit("}", 1)[0] if "}" in chunk else chunk
        fields: dict[str, str] = {}
        for m in re.finditer(r"(\w+)\s*=\s*[\{\"](.*?)[\}\"]\s*,?\s*(?=\w+\s*=|$)", body, re.S):
            fields[m.group(1).lower()] = re.sub(r"\s+", " ", m.group(2)).strip()

        # BibTeX 的 keywords 用**逗号**分隔，不能用通用的分号切分
        kw = [k.strip() for k in re.split(r"[,;]", fields.get("keywords", "")) if k.strip()]

        # BibTeX 的作者用 " and " 分隔
        authors = [a.strip() for a in re.split(r"\s+and\s+", fields.get("author", ""),
                                              flags=re.I) if a.strip()]

        year = ""
        m = re.search(r"\b(19|20)\d{2}\b", fields.get("year", ""))
        if m:
            year = m.group(0)
        records.append(Record(
            title=fields.get("title", ""),
            year=year,
            source=fields.get("journal", "") or fields.get("booktitle", ""),
            doi=fields.get("doi", ""),
            authors=authors,
            keywords=[clean_term(k) for k in kw if clean_term(k)],
            abstract=fields.get("abstract", ""),
            raw=fields,
        ))
    return records


# --------------------------------------------------------------------------
# 通用 CSV / Excel（自动找关键词列）
# --------------------------------------------------------------------------

_KW_COL_CANDIDATES = [
    "关键词", "关键字", "作者关键词", "keywords", "author keywords",
    "keyword", "k1", "de", "keyword list",
]


def _rows_to_records(rows: list[dict]) -> list[Record]:
    if not rows:
        return []
    cols = list(rows[0].keys())
    lowered = {c: (c or "").strip().lower() for c in cols}

    def find_col(*cands: str) -> str | None:
        for c in cols:
            if lowered[c] in cands:
                return c
        for c in cols:
            if any(cand in lowered[c] for cand in cands):
                return c
        return None

    kw_col = find_col(*_KW_COL_CANDIDATES)
    title_col = find_col("题名", "标题", "title", "ti", "篇名", "article title")
    year_col = find_col("年", "年份", "year", "py", "发表年度", "date")
    src_col = find_col("来源", "期刊", "source", "journal", "so", "刊名", "source title")
    abs_col = find_col("摘要", "abstract", "ab")
    au_col = find_col("作者", "author", "au", "authors")
    doi_col = find_col("doi")

    records: list[Record] = []
    for row in rows:
        kw = split_multi(str(row.get(kw_col) or "")) if kw_col else []
        year = ""
        yv = str(row.get(year_col) or "") if year_col else ""
        m = re.search(r"\b(19|20)\d{2}\b", yv)
        if m:
            year = m.group(0)
        records.append(Record(
            title=str(row.get(title_col) or "").strip() if title_col else "",
            year=year,
            source=str(row.get(src_col) or "").strip() if src_col else "",
            doi=str(row.get(doi_col) or "").strip() if doi_col else "",
            authors=split_multi(str(row.get(au_col) or "")) if au_col else [],
            keywords=[clean_term(k) for k in kw if clean_term(k)],
            abstract=str(row.get(abs_col) or "").strip() if abs_col else "",
            raw={k: str(v) for k, v in row.items()},
        ))
    return records


def parse_csv(text: str) -> list[Record]:
    for delim in (",", "\t", ";"):
        try:
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            rows = [r for r in reader]
        except csv.Error:
            continue
        if rows and len(rows[0]) > 1:
            return _rows_to_records(rows)
    return []


def parse_excel(path: str) -> list[Record]:
    try:
        import pandas as pd
    except ImportError:
        raise RuntimeError("解析 Excel 需要 pandas：pip install pandas openpyxl")
    df = pd.read_excel(path, dtype=str).fillna("")
    return _rows_to_records(df.to_dict(orient="records"))


# --------------------------------------------------------------------------
# 自动识别入口
# --------------------------------------------------------------------------

def parse_file(path: str) -> tuple[list[Record], str]:
    """解析任意支持格式的文献文件，返回 (记录列表, 识别出的格式名)。"""
    ext = os.path.splitext(path)[1].lower()

    if ext in (".xlsx", ".xls", ".xlsm"):
        return parse_excel(path), "Excel"

    text = read_text(path)
    head = text[:4000]

    # Web of Science
    if re.search(r"^FN\s+\S", head, re.M) and re.search(r"^VR\s", head, re.M):
        recs = parse_wos(text)
        if recs:
            return recs, "Web of Science"

    # RIS
    if re.search(r"^TY\s{2}-\s", head, re.M):
        return parse_ris(text), "RIS"

    # BibTeX
    if re.search(r"^@\w+\s*\{", head, re.M):
        return parse_bibtex(text), "BibTeX"

    # CNKI EndNote
    if re.search(r"^%0\s", head, re.M):
        return parse_cnki_endnote(text), "CNKI (EndNote)"

    # CNKI RefWorks
    if re.search(r"^RT\s+\S", head, re.M) and re.search(r"^(K1|T1)\s", head, re.M):
        return parse_cnki_refworks(text), "CNKI (RefWorks)"

    # Scopus CSV：需要 Scopus 特有列，否则只是恰好有同名关键词列的普通表格
    scopus_markers = ["Author full names", "EID", "Abbreviated Source Title",
                      "Source title", "Index Keywords", "Correspondence Address"]
    if re.search(r"Author Keywords|Index Keywords", head, re.I):
        hits = sum(1 for m in scopus_markers if m.lower() in head.lower())
        if hits >= 2:
            return parse_scopus(text), "Scopus"
        recs = parse_csv(text)
        if recs:
            return recs, "CSV / 表格（含关键词列）"

    # 兜底：CSV / 制表符
    if ext in (".csv", ".tsv", ".txt") or "," in head.split("\n")[0]:
        recs = parse_csv(text)
        if recs:
            return recs, "CSV / 表格"

    # 最后再试一次 WoS 纯文本
    recs = parse_wos(text)
    if recs:
        return recs, "Web of Science (纯文本)"

    return [], "无法识别"


def load_records(paths: Iterable[str]) -> tuple[list[Record], list[str]]:
    """加载一个或多个文件并合并，返回 (记录, 格式说明列表)。"""
    all_records: list[Record] = []
    notes: list[str] = []
    for p in paths:
        recs, fmt = parse_file(p)
        all_records.extend(recs)
        notes.append(f"{os.path.basename(p)} → {fmt}，{len(recs)} 条记录")
    return all_records, notes
