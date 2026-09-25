"""同义词归并：把"独居老人 / 空巢老人 / elderly living alone"这类词合并成一个。

这是文献计量分析里最费人工、也最容易出错的一步。
策略分三层：
  1. 自动归并（大小写、单复数、连字符、全半角、通用缩写）
  2. 相似度候选（找出疑似同义词，生成待确认清单）
  3. 人工复核（用户过一遍清单即可，不用从零整理）

归并结果输出为 VOSviewer 官方 thesaurus 文件格式：
    label<TAB>replace by
空白的 replace by 表示删除该词。
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------

def normalize(term: str) -> str:
    """生成用于比对的标准形式（不改动原词）。"""
    t = term.strip().lower()
    t = unicodedata.normalize("NFKC", t)                 # 全角转半角
    t = t.replace("\u2010", "-").replace("\u2013", "-").replace("\u2014", "-")
    t = re.sub(r"[\s\-_]+", " ", t)                      # 连字符/下划线/空白统一
    return re.sub(r"\s+", " ", t).strip()


def singularize(word: str) -> str:
    """极简单数化，只处理英文常见复数形态。"""
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def key_form(term: str) -> str:
    """比对用的键：归一化 + 去标点 + 逐词单数化。"""
    t = normalize(term)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    words = [singularize(w) for w in t.split() if w]
    return " ".join(words)


# --------------------------------------------------------------------------
# 内置等价规则（可按学科扩充）
# --------------------------------------------------------------------------

BUILTIN_EQUIV: list[tuple[str, str]] = [
    # 拼写变体
    (r"\bmodelling\b", "modeling"),
    (r"\bbehaviour\b", "behavior"),
    (r"\bprogramme\b", "program"),
    (r"\boptimisation\b", "optimization"),
    (r"\borganisation\b", "organization"),
    (r"\banalyse\b", "analyze"),
    # 老龄化 / 智慧养老（贴合用户研究主题）
    (r"\bthe elderly\b", "older adult"),
    (r"\belderly people\b", "older adult"),
    (r"\belderly\b", "older adult"),
    (r"\bsenior citizen\b", "older adult"),
    (r"\baged people\b", "older adult"),
    (r"\bliving alone\b", "solitary living"),
    (r"\bsolo living\b", "solitary living"),
    (r"\blive alone\b", "solitary living"),
    (r"\bhome automation\b", "smart home"),
    (r"\bsmart homes\b", "smart home"),
    (r"\baal\b", "ambient assisted living"),
    (r"\bambient assistive living\b", "ambient assisted living"),
    (r"\bhealthcare monitoring\b", "health monitoring"),
    (r"\bhealth care monitoring\b", "health monitoring"),
    (r"\btelemonitoring\b", "remote monitoring"),
    (r"\bremote health monitoring\b", "remote monitoring"),
    (r"\biot\b", "internet of things"),
    (r"\bict\b", "information and communication technology"),
    (r"\bwelfare technology\b", "assistive technology"),
    (r"\bassistive technologies\b", "assistive technology"),
    (r"\bhuman computer interaction\b", "human-computer interaction"),
    (r"\bhci\b", "human-computer interaction"),
    (r"\buser experience\b", "user experience"),
    (r"\bux\b", "user experience"),
    # 无意义词
    (r"^et al\.?$", ""),
    (r"^review$", ""),
]


def apply_builtin_rules(term: str) -> str:
    """套用内置规则后返回新词形，规则命中不了则返回原词。"""
    t = term
    for pat, rep in BUILTIN_EQUIV:
        t = re.sub(pat, rep, t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    return t or term


# --------------------------------------------------------------------------
# 自动归并
# --------------------------------------------------------------------------

def find_auto_groups(terms: Counter, min_count: int = 1) -> list[dict]:
    """找出可自动合并的词组。

    canonical 取出现次数最多的写法，其余作为成员被合并过去。
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for term, cnt in terms.items():
        if cnt < min_count:
            continue
        buckets[key_form(apply_builtin_rules(term))].append(term)

    groups: list[dict] = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda t: (-terms[t], t))
        groups.append({
            "canonical": ordered[0],
            "members": ordered[1:],
            "reason": "形态等价（大小写 / 单复数 / 连字符 / 通用缩写）",
            "confidence": "high",
        })
    groups.sort(key=lambda g: -terms[g["canonical"]])
    return groups


def find_fuzzy_candidates(terms: Counter, threshold: float = 0.88,
                          top_n: int = 300,
                          docs_terms: list[list[str]] | None = None) -> list[dict]:
    """找出疑似同义词，交给人工确认。

    两类候选：
      1. **编辑相似**：字符串相似度 ≥ threshold（如「数字化技术」vs「数字技术」）
      2. **包含关系**：一个词是另一个的子串（如「青少年」⊂「儿童青少年」）

    第 2 类是中英文混合语料的必需项——中文靠加修饰词构成新词，
    包含关系的相似度往往只有 0.7 左右，单靠相似度阈值会漏掉。

    ⚠️ 但包含关系噪声很大：「健康」是「健康中国」「健康促进」「健康素养」……
    十几个词的子串，但它显然不该把那些词全吸收掉。

    **用共现率来消歧**（关键设计）：
      真正的同义词**很少同时出现在同一篇文献里**——作者会选其中一个写法。
      相关但不同的概念则**经常共现**——作者会同时用它们。
    所以共现率高 = 很可能是不同概念（不该合并）；
    共现率低 = 很可能是同义写法（该合并）。
    """
    items = sorted(terms.items(), key=lambda x: -x[1])[:top_n]
    out: list[dict] = []

    # 预计算每个词出现在哪些文献里，用于算共现率
    doc_sets: dict[str, set[int]] = {}
    if docs_terms:
        for di, ts in enumerate(docs_terms):
            for t in set(ts):
                doc_sets.setdefault(t, set()).add(di)

    def cooccur_rate(a: str, b: str) -> float | None:
        """两词共现篇数 / 较少一方出现篇数。None 表示无法计算。"""
        sa, sb = doc_sets.get(a), doc_sets.get(b)
        if not sa or not sb:
            return None
        denom = min(len(sa), len(sb))
        if denom == 0:
            return None
        return len(sa & sb) / denom

    def add(a: str, b: str, ca: int, cb: int, reason: str,
            kind: str, similarity: float) -> None:
        if ca != cb:
            canonical, member = (a, b) if ca > cb else (b, a)
        else:
            canonical, member = (a, b) if len(a) <= len(b) else (b, a)
        rate = cooccur_rate(canonical, member)
        out.append({
            "canonical": canonical,
            "members": [member],
            "reason": reason,
            "confidence": "review",
            "similarity": similarity,
            "kind": kind,
            "cooccur_rate": round(rate, 3) if rate is not None else None,
        })

    for i in range(len(items)):
        a, ca = items[i]
        ka = key_form(a)
        for j in range(i + 1, len(items)):
            b, cb = items[j]
            kb = key_form(b)
            if ka == kb:
                continue

            # 规则一：包含关系。要求较短词 ≥3 个字符，
            # 避免「健康」「促进」这类通用词把所有派生词都吸进来。
            if len(ka) >= 3 and len(kb) >= 3 and (ka in kb or kb in ka):
                add(a, b, ca, cb, "包含关系（需人工确认）",
                    "containment", 0.0)
                continue

            # 规则二：编辑相似度
            if abs(len(ka) - len(kb)) > max(5, int(len(ka) * 0.4)):
                continue
            if ka[:1] != kb[:1] and not (ka.startswith(kb) or kb.startswith(ka)):
                continue
            ratio = SequenceMatcher(None, ka, kb).ratio()
            if ratio >= threshold:
                add(a, b, ca, cb,
                    f"字符串相似度 {ratio:.2f}（需人工确认）",
                    "similarity", round(ratio, 3))

    # 同一 canonical 只保留一条
    best: dict[str, dict] = {}
    for c in out:
        k = c["canonical"]
        if k not in best:
            best[k] = c
        else:
            # 优先保留共现率更低的（更像同义词）
            old = best[k].get("cooccur_rate")
            new = c.get("cooccur_rate")
            if new is not None and (old is None or new < old):
                best[k] = c

    # 排序：共现率低的排前面（最可能是真同义词），无法计算的排最后
    def sort_key(x: dict):
        r = x.get("cooccur_rate")
        return (r is None, r if r is not None else 1.0, x["kind"] != "containment")

    return sorted(best.values(), key=sort_key)


# --------------------------------------------------------------------------
# 合并执行
# --------------------------------------------------------------------------

def _resolve_chains(mapping: dict[str, str]) -> dict[str, str]:
    """把映射链解析到最终目标，并打破环。

    例如 a→b、b→c 解析成 a→c、b→c。
    遇到环（a→b→a）时取字典序最小的词作终点，保证结果确定、可复现。
    """
    resolved: dict[str, str] = {}
    for start in list(mapping):
        chain = [start]
        cur = mapping.get(start)
        while cur is not None and cur not in chain:
            chain.append(cur)
            cur = mapping.get(cur)

        if cur is not None and cur in chain:
            # 成环：环内取字典序最小的作终点
            cycle = chain[chain.index(cur):]
            end = sorted(cycle)[0]
        else:
            end = chain[-1]

        if end and end != start:
            resolved[start] = end
    return resolved


def merge_terms(term_lists: list[list[str]],
                auto_groups: list[dict] | None = None,
                manual_rules: dict[str, str] | None = None,
                drop_terms: set[str] | None = None,
                case_insensitive: bool = True) -> tuple[list[list[str]], dict, dict]:
    """按归并规则重写每篇文献的关键词列表。

    返回 (新的关键词列表, 映射表, 统计信息)

    规则优先级：**人工/预设规则 > 自动归并规则**。

    这里有个真实数据踩过的坑：自动归并按频次挑规范词（如 "older adults"），
    而预设表规定 "older adults" → "older adult"，两者叠加会形成环
    "older adult" ⇄ "older adults"，导致两个词形同时残留在结果里。
    解决办法是先把人工规则的「目标词」从自动映射的键里摘掉，再解析映射链。
    """
    auto_groups = auto_groups or []
    manual_rules = manual_rules or {}
    drop_terms = drop_terms or set()

    # 1) 铺开自动归并规则
    mapping: dict[str, str] = {}
    for g in auto_groups:
        for m in g["members"]:
            mapping[m] = g["canonical"]

    # 2) 人工规则优先：摘掉以人工规则目标词为键的自动映射，避免成环
    for target in set(manual_rules.values()):
        mapping.pop(target, None)
    mapping.update(manual_rules)

    # 3) 解析映射链，清掉残余的多跳与环
    mapping = _resolve_chains(mapping)

    if case_insensitive:
        ci_map: dict[str, str] = {}
        for k, v in mapping.items():
            ci_map[normalize(k)] = v
        for k, v in list(ci_map.items()):
            if k not in mapping:
                mapping[k] = v
        mapping = _resolve_chains(mapping)

    drop_norm = {normalize(d) for d in drop_terms}
    stats = {"merged": 0, "dropped": 0, "kept": 0}

    def transform(term: str) -> str | None:
        t = term.strip()
        if not t:
            return None
        if normalize(t) in drop_norm:
            stats["dropped"] += 1
            return None
        target = mapping.get(t)
        if target is None and case_insensitive:
            target = mapping.get(normalize(t))
        if target and target != t:
            # 关键：映射后的目标词也要过一遍剔除检查。
            #
            # 否则会出现这种漏洞：预设表把「智慧养老服务」映射成「智慧养老」，
            # 而调用方用 --exclude-terms 剔除了「智慧养老」——
            # 原词躲过了第一道检查，映射结果却没人管，
            # 被剔除的词就通过这条路径重新混进网络。
            # 实测真实数据踩到过：剔除「智慧养老」后图上仍有该节点。
            if normalize(target) in drop_norm:
                stats["dropped"] += 1
                return None
            stats["merged"] += 1
            return target
        stats["kept"] += 1
        return t

    new_lists: list[list[str]] = []
    for terms in term_lists:
        seen: list[str] = []
        for t in terms:
            r = transform(t)
            if r and r not in seen:
                seen.append(r)
        new_lists.append(seen)

    return new_lists, mapping, stats


# --------------------------------------------------------------------------
# 通用学科词过滤
# --------------------------------------------------------------------------
#
# 真实数据里常混进一批「学科标签」而非「研究主题」的词。
# 典型来源：
#   1. OpenAlex 的 keywords 字段（算法抽取的概念，含大量学科名）
#   2. WoS 的 Keywords Plus（数据库自动生成，含宽泛词）
#   3. 作者自己写的过宽泛词
#
# 这些词频次极高但毫无主题区分度，会把聚类拉平。默认不删，
# 用 --drop-generic 显式开启（因为不同学科该删的词不同）。
#
# 注意：这里**故意不包含**有实际含义的词，如
#   artificial intelligence / machine learning / human-computer interaction
#   internet of things / ambient intelligence / deep learning
# 这些是研究主题，不是学科标签，误删会损失信息。

GENERIC_TERMS_EN = [
    # 一级学科 / 大类
    "computer science", "medicine", "engineering", "psychology", "business",
    "gerontology", "sociology", "mathematics", "physics", "chemistry",
    "biology", "materials science", "nursing", "political science",
    "economics", "geography", "environmental science", "education", "law",
    "history", "philosophy", "statistics", "archaeology", "geology",
    "art", "literature", "linguistics", "anthropology", "neuroscience",
    "biochemistry", "genetics", "ecology", "astronomy", "optics",
    "acoustics", "thermodynamics", "quantum mechanics", "electromagnetism",
    # 工程子类
    "electrical engineering", "mechanical engineering", "civil engineering",
    "aerospace engineering", "systems engineering", "control engineering",
    "control theory", "manufacturing engineering", "structural engineering",
    # 计算机通用子领域（无主题区分度）
    "computer network", "operating system", "embedded system",
    "real-time computing", "data science", "world wide web",
    "telecommunications", "internet privacy", "computer security",
    "distributed computing", "parallel computing", "cloud computing",
    "software engineering", "programming language", "database",
    "computer graphics", "image processing", "signal processing",
    "information technology", "information science", "library science",
    "knowledge management", "simulation", "visualization", "algorithm",
    "wireless", "software", "hardware", "computing",
    # OpenAlex 常见的结构性噪声
    "population", "architecture", "context", "value (mathematics",
    "phase (matter", "bridging (networking", "set (mathematics",
    "quality (philosophy", "structure (mathematical",
    # 研究方法套话
    "questionnaire", "interview", "case study", "empirical research",
    "quantitative research", "qualitative research", "literature review",
    "systematic review", "meta-analysis", "survey",
    # 通用结果词
    "quality of life", "well-being", "satisfaction", "usability",
    "performance", "efficiency", "effectiveness", "implementation",
    "framework", "model", "system", "method", "approach", "analysis",
    "evaluation", "assessment", "design", "development", "management",
]

GENERIC_TERMS_ZH = [
    # 中文一级学科 / 大类
    "计算机科学", "医学", "工程", "心理学", "管理学", "经济学",
    "社会学", "数学", "物理学", "化学", "生物学", "材料科学",
    "护理学", "政治学", "地理学", "环境科学", "教育学", "法学",
    "历史学", "哲学", "统计学", "情报学", "图书馆学",
    # 中文通用套话
    "影响因素", "研究进展", "研究综述", "文献综述", "研究现状",
    "对策建议", "启示", "综述", "述评", "研究展望", "应用现状",
    "研究热点", "可视化分析", "文献计量分析", "知识图谱", "研究趋势",
    "案例分析", "实证研究", "问卷调查", "深度访谈",
    "生活质量", "满意度", "可用性", "绩效", "效率",
    "框架", "模型", "系统", "方法", "分析", "评价", "设计",
    "开发", "管理", "应用", "技术",
]


def generic_terms() -> set[str]:
    """返回通用学科词集合（含中英文）。"""
    return set(GENERIC_TERMS_EN) | set(GENERIC_TERMS_ZH)


def drop_generic_from(term_lists: list[list[str]],
                      extra: set[str] | None = None
                      ) -> tuple[list[list[str]], list[str]]:
    """从关键词列表里剔除通用学科词，返回 (新列表, 被剔除的词)。"""
    blocked = {normalize(t) for t in generic_terms()}
    if extra:
        blocked |= {normalize(t) for t in extra}

    dropped: set[str] = set()
    out: list[list[str]] = []
    for terms in term_lists:
        kept: list[str] = []
        for t in terms:
            if normalize(t) in blocked:
                dropped.add(t)
            else:
                kept.append(t)
        out.append(kept)
    return out, sorted(dropped)


# --------------------------------------------------------------------------
# 读写 VOSviewer thesaurus 文件
# --------------------------------------------------------------------------

def write_thesaurus(path: str, mapping: dict[str, str],
                    drop: set[str] | None = None) -> None:
    """写出 VOSviewer thesaurus 文件（label / replace by）。"""
    drop = drop or set()
    rows = [(k, v) for k, v in sorted(mapping.items()) if k != v]
    rows += [(d, "") for d in sorted(drop)]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("label\treplace by\n")
        for k, v in rows:
            fh.write(f"{k}\t{v}\n")


def read_thesaurus(path: str) -> tuple[dict[str, str], set[str]]:
    """读取 VOSviewer thesaurus 文件，返回 (映射, 待删除集合)。"""
    mapping: dict[str, str] = {}
    drop: set[str] = set()
    with open(path, encoding="utf-8-sig") as fh:
        reader = csv.reader(fh, delimiter="\t")
        next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            label, replace = row[0].strip(), row[1].strip()
            if not label:
                continue
            if replace:
                mapping[label] = replace
            else:
                drop.add(label)
    return mapping, drop


# --------------------------------------------------------------------------
# 复核清单
# --------------------------------------------------------------------------

def build_review_report(terms: Counter, auto_groups: list[dict],
                        fuzzy: list[dict], top_n: int = 60) -> str:
    """生成人读的归并复核清单（Markdown）。"""
    lines: list[str] = []
    lines.append("# 同义词归并复核清单\n")
    lines.append(f"关键词总数：{len(terms)}　总出现次数：{sum(terms.values())}\n")

    lines.append("\n## 一、已自动归并（高置信，一般无需改动）\n")
    lines.append("| 保留词 | 合并进来的写法 | 依据 |")
    lines.append("|---|---|---|")
    for g in auto_groups[:top_n]:
        members = "、".join(g["members"])
        lines.append(f"| {g['canonical']} | {members} | {g['reason']} |")
    if len(auto_groups) > top_n:
        lines.append(f"| …… 另有 {len(auto_groups) - top_n} 组 | | |")

    lines.append("\n## 二、待人工确认（可能是同义词）\n")
    if fuzzy:
        lines.append("**怎么读这张表**：")
        lines.append("")
        lines.append("- **共现率** = 两词同时出现在同一篇文献的比例（取较小一方为分母）")
        lines.append("- **共现率低 → 很可能是同义写法**（作者会选其中一个，很少两个都用）→ 建议合并")
        lines.append("- **共现率高 → 很可能是不同概念**（作者会同时用它们）→ **不要合并**")
        lines.append("")
        lines.append("| 疑似保留词 | 疑似被合并词 | 共现率 | 判定依据 |")
        lines.append("|---|---|---|---|")
        for c in fuzzy[:top_n]:
            r = c.get("cooccur_rate")
            rs = f"{r:.2f}" if r is not None else "—"
            lines.append(f"| {c['canonical']} | {c['members'][0]} | {rs} | {c['reason']} |")
        lines.append("")
        lines.append("> **重点看共现率 ≤ 0.30 的行**，那些最可能是真同义词。")
        lines.append("> 共现率高的行请谨慎——例如「主动健康」与「主动健康行为」"
                     "虽然字面包含，但共现率很高，说明是**两个不同的概念**，不该合并。")
    else:
        lines.append("（无）")

    lines.append("\n## 三、高频关键词 Top 40（供人工检查是否有漏合并）\n")
    lines.append("| 关键词 | 出现次数 |")
    lines.append("|---|---|")
    for t, c in terms.most_common(40):
        lines.append(f"| {t} | {c} |")

    lines.append("\n> 确认方式：把要保留的写法放在「保留词」列，"
                 "把要合并掉的写法放进 thesaurus 文件即可，无需手工重排全部关键词。\n")
    return "\n".join(lines)
