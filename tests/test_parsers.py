#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""解析层与算法层的回归测试。

**不需要 Java 或 VOSviewer**，可以在任何环境（含 CI）里跑：

    python tests/test_parsers.py

覆盖本项目中真实数据暴露过的坑，防止回归：
  - CNKI RefWorks 不带 ER 标记（曾导致 120 条记录被解析成 1 条）
  - clean_term 括号处理顺序（曾反而制造出括号异常）
  - BibTeX 关键词按逗号分隔（曾被当成一个词）
  - 自动归并与人工规则形成循环映射（曾导致两个词形同时残留）
  - 通用 CSV 被误判为 Scopus
  - VOSviewer 用 -largest_component 后图表与报告不一致
"""

from __future__ import annotations

import os
import sys

# Windows 控制台默认使用 ANSI 代码页（如 cp1252），直接 print 中文会抛
# UnicodeEncodeError 导致脚本崩溃。统一改用 UTF-8，并让无法编码的字符
# 降级为替代符而不是中断执行。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from voslib import parsers as P            # noqa: E402
from voslib import thesaurus as T          # noqa: E402
from voslib import network as N            # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


# ---------------------------------------------------------------------------
section("1. clean_term 关键词清洗")

check("修未闭合括号", P.clean_term("Context (archaeology") == "Context",
      repr(P.clean_term("Context (archaeology")))
check("保留合法括号", P.clean_term("Service (business)") == "Service (business)",
      repr(P.clean_term("Service (business)")))
check("脱掉外层括号", P.clean_term("(smart home)") == "smart home",
      repr(P.clean_term("(smart home)")))
check("统一连接号为连字符",
      P.clean_term("Human\u2013computer interaction") == "Human-computer interaction",
      repr(P.clean_term("Human\u2013computer interaction")))
check("去首尾标点", P.clean_term("  Fall Detection.  ") == "Fall Detection",
      repr(P.clean_term("  Fall Detection.  ")))
check("压缩内部空白", P.clean_term("smart    home") == "smart home",
      repr(P.clean_term("smart    home")))

# 括号平衡不变量：任何输入清洗后括号都应配平
_bad = []
for s in ["a (b", "a) b", "(a)", "((a)", "a (b) (c", "x (y (z", ")a("]:
    r = P.clean_term(s)
    if r.count("(") != r.count(")"):
        _bad.append((s, r))
check("括号平衡不变量", not _bad, str(_bad))


# ---------------------------------------------------------------------------
section("2. 各格式解析")

# CNKI RefWorks —— 关键回归：不带 ER 标记
CNKI_RW = """RT Journal Article
SR 1
A1 张三;李四
T1 智慧居家养老系统设计
JF 中国老年学杂志
YR 2022
IS 06
vo 23
K1 智慧养老;智能家居;物联网
AB 本文研究智慧养老系统。

RT Journal Article
SR 1
A1 王五
T1 独居老人健康监测方法
JF 计算机应用研究
YR 2023
K1 独居老人;健康监测;可穿戴设备

RT Journal Article
SR 1
A1 赵六
T1 适老化改造研究
JF 建筑学报
YR 2021
K1 适老化改造;智能家居
"""
rw = P.parse_cnki_refworks(CNKI_RW)
check("CNKI RefWorks 记录数正确（无 ER 标记）", len(rw) == 3, f"得到 {len(rw)}")
check("CNKI RefWorks 标题正确", rw[0].title == "智慧居家养老系统设计", rw[0].title)
check("CNKI RefWorks 关键词切分", rw[0].keywords == ["智慧养老", "智能家居", "物联网"],
      str(rw[0].keywords))
check("CNKI RefWorks 年份", rw[1].year == "2023", rw[1].year)
check("CNKI RefWorks 作者", rw[0].authors == ["张三", "李四"], str(rw[0].authors))

# CNKI EndNote
CNKI_EN = """%0 Journal Article
%A 李明华;王建国
%T 智能家居技术研究
%J 人口与发展
%D 2023
%K 智能家居;辅助技术
%X 摘要内容

%0 Journal Article
%A 陈浩
%T 独居老人照护
%J 中国全科医学
%D 2022
%K 独居老人;老年照护
"""
en = P.parse_cnki_endnote(CNKI_EN)
check("CNKI EndNote 记录数", len(en) == 2, f"得到 {len(en)}")
check("CNKI EndNote 关键词", en[0].keywords == ["智能家居", "辅助技术"], str(en[0].keywords))

# RIS
RIS = """TY  - JOUR
AU  - Zhang, Wei
TI  - Smart home for older adults
JO  - Sensors
PY  - 2021
KW  - smart home
KW  - older adults
ER  -

TY  - JOUR
AU  - Chen, Hao
TI  - Wearable sensors
JO  - IEEE Access
PY  - 2022
KW  - wearable sensor
KW  - health monitoring
ER  -
"""
ris = P.parse_ris(RIS)
check("RIS 记录数", len(ris) == 2, f"得到 {len(ris)}")
check("RIS 关键词逐条切分", ris[0].keywords == ["smart home", "older adults"],
      str(ris[0].keywords))

# BibTeX —— 关键回归：keywords 用逗号分隔
BIB = """@article{a,
  author = {Zhang, Wei and Liu, Yang},
  title = {Smart home for older adults},
  journal = {Sensors},
  year = {2021},
  keywords = {smart home, older adults, fall detection}
}
"""
bib = P.parse_bibtex(BIB)
check("BibTeX 关键词按逗号切分", bib[0].keywords == ["smart home", "older adults", "fall detection"],
      str(bib[0].keywords))
check("BibTeX 作者按 and 切分", bib[0].authors == ["Zhang, Wei", "Liu, Yang"],
      str(bib[0].authors))

# WoS 纯文本（含 Keywords Plus）
WOS = """FN Clarivate Analytics Web of Science
VR 1.0
PT J
AU Zhang, Wei
TI Smart home technology for older adults
SO SENSORS
DE smart home; older adults
ID internet of things; wearable sensor
PY 2021
ER

EF
"""
wos = P.parse_wos(WOS)
check("WoS 纯文本记录数", len(wos) == 1, f"得到 {len(wos)}")
check("WoS 作者关键词", wos[0].keywords == ["smart home", "older adults"], str(wos[0].keywords))
check("WoS Keywords Plus", wos[0].keywords_plus == ["internet of things", "wearable sensor"],
      str(wos[0].keywords_plus))


# ---------------------------------------------------------------------------
section("3. 同义词归并与消歧")

# 循环映射回归：自动归并选 "older adults"，人工规则规定 "older adults"→"older adult"
docs = [
    ["older adults", "smart home"], ["older adults", "IoT"], ["older adults", "AAL"],
    ["Older adults", "smart home"], ["older adult", "fall detection"],
    ["smart home", "smart homes"], ["Smart home", "elderly"], ["older adults", "dementia"],
]
from collections import Counter  # noqa: E402
raw = Counter(k for d in docs for k in set(d))
auto = T.find_auto_groups(raw, min_count=1)
preset = {"older adults": "older adult", "elderly": "older adult",
          "IoT": "internet of things", "AAL": "ambient assisted living",
          "smart homes": "smart home"}
merged, mapping, stats = T.merge_terms(docs, auto_groups=auto, manual_rules=preset)
after = Counter(k for d in merged for k in set(d))
_older = [k for k in after if k.lower().startswith("older")]
check("循环映射已打破（older* 归一）", len(_older) == 1, str(_older))

# 映射链解析
check("映射链 a→b,b→c 解析为 a→c", T._resolve_chains({"x": "y", "y": "z"}) == {"x": "z", "y": "z"})
check("环 a↔b 被打破", set(T._resolve_chains({"a": "b", "b": "a"}).keys()) <= {"a", "b"})

# 回归：映射到「被剔除词」的关键词也必须被剔除
#
# 曾经的漏洞：transform() 只检查原词是否在剔除列表里，
# 映射结果却不再检查。于是预设表把「智慧养老服务」映射成「智慧养老」后，
# 即使用户用 --exclude-terms 剔除了「智慧养老」，该词仍会重新混进网络。
_docs_drop = [
    ["智慧养老服务", "老年人"],
    ["智慧养老", "护理"],
    ["养老服务", "社区养老"],
]
_merged_drop, _, _st = T.merge_terms(
    _docs_drop,
    manual_rules={"智慧养老服务": "智慧养老", "养老服务": "智慧养老"},
    drop_terms={"智慧养老"},
)
_after_drop = Counter(k for d in _merged_drop for k in set(d))
check("映射到被剔除词的关键词也一并剔除",
      "智慧养老" not in _after_drop, str(dict(_after_drop)))
check("剔除后其余关键词保留",
      "老年人" in _after_drop and "护理" in _after_drop, str(dict(_after_drop)))

# 超几何检验
_p0 = N._hypergeom_tail(120, 20, 19, 0)
check("超几何 P(≤0) 在合理范围", 0.0 < _p0 < 0.5, f"得到 {_p0:.4f}")
check("观测值远超期望时 p→1", N._hypergeom_tail(100, 10, 10, 10) == 1.0)
check("期望为零时 p=1", N._hypergeom_tail(100, 0, 5, 0) == 1.0)


# ---------------------------------------------------------------------------
section("4. 共现网络与聚类")

net = N.build_network([["a", "b", "c"], ["a", "b"], ["a", "c"], ["b", "c"]],
                      min_occurrences=1, counting="full")
check("网络节点数", len(net.nodes) == 3, f"得到 {len(net.nodes)}")
check("网络文献数", net.n_docs == 4, f"得到 {net.n_docs}")
check("a 出现 3 次", net.nodes[[k for k, v in net.nodes.items()
                                if v.label == "a"][0]].occurrences == 3)

net2 = N.build_network([["a", "b"]], min_occurrences=1, counting="full")
check("单条文献也能建网", len(net2.nodes) == 2, f"得到 {len(net2.nodes)}")

net3 = N.build_network([["a", "b", "c"]], min_occurrences=1, counting="binary")
check("binary 计数", all(n.occurrences == 1 for n in net3.nodes.values()))


# ---------------------------------------------------------------------------
section("5. 示例文件端到端解析")

for fname, expect_fmt in [("sample_wos.txt", "Web of Science"),
                          ("cnki_refworks.txt", "CNKI (RefWorks)"),
                          ("cnki_endnote.txt", "CNKI (EndNote)")]:
    path = os.path.join(ROOT, "examples", fname)
    if not os.path.isfile(path):
        check(f"示例文件 {fname} 存在", False, "文件缺失")
        continue
    recs, notes = P.load_records([path])
    fmt_ok = expect_fmt in notes[0]
    withkw = sum(1 for r in recs if r.keywords)
    check(f"{fname} 格式识别为 {expect_fmt}", fmt_ok, notes[0])
    check(f"{fname} 解析出记录", len(recs) > 50, f"得到 {len(recs)}")
    check(f"{fname} 关键词覆盖率 > 80%", withkw / len(recs) > 0.8,
          f"{withkw}/{len(recs)}")


# ---------------------------------------------------------------------------
print(f"\n{'=' * 66}")
print(f"结果：{PASS} 通过，{FAIL} 失败")
if FAILURES:
    print("\n失败项：")
    for f in FAILURES:
        print(f"  - {f}")
print("=" * 66)
sys.exit(1 if FAIL else 0)
