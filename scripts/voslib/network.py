"""文献共现网络构建与分析。

职责：
  1. 从关键词列表构建共现矩阵（支持 binary / full 两种计数方式）
  2. 计算节点指标：出现次数、总连接强度、平均发表年、平均被引、中介中心性
  3. 计算聚类质量：模块度 Q、各聚类规模
  4. 输出可直接喂给 VOSviewer 的 map / network 数据结构

设计取舍：
  - 布局与聚类默认交给 VOSviewer 做（学术上更权威、可复现）
  - 本模块自算一套 Louvain 聚类，用于：(a) 报告指标 (b) VOSviewer 不可用时的降级
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Node:
    id: int
    label: str
    occurrences: int = 0          # 出现次数（对应 VOSviewer 的 weight）
    total_link_strength: int = 0  # 与其他词的共现次数总和
    avg_year: float = 0.0         # 平均发表年（overlay 用）
    avg_citations: float = 0.0    # 平均被引（overlay 用）
    cluster: int = 0
    x: float = 0.0
    y: float = 0.0
    betweenness: float = 0.0
    docs: list[int] = field(default_factory=list)


@dataclass
class Network:
    nodes: dict[int, Node] = field(default_factory=dict)
    edges: dict[tuple[int, int], float] = field(default_factory=dict)
    n_docs: int = 0
    counting: str = "full"
    metrics: dict = field(default_factory=dict)

    def label_to_id(self) -> dict[str, int]:
        return {n.label: n.id for n in self.nodes.values()}

    def degree(self, nid: int) -> int:
        return sum(1 for (a, b) in self.edges if a == nid or b == nid)

    def neighbors(self, nid: int) -> set[int]:
        out: set[int] = set()
        for a, b in self.edges:
            if a == nid:
                out.add(b)
            elif b == nid:
                out.add(a)
        return out


# --------------------------------------------------------------------------
# 构建共现网络
# --------------------------------------------------------------------------

def build_network(docs_terms: list[list[str]],
                  records: list | None = None,
                  counting: str = "full",
                  min_occurrences: int = 2,
                  max_terms: int | None = None,
                  drop_terms: set[str] | None = None) -> Network:
    """从每篇文献的关键词列表构建共现网络。

    counting:
      - "full"   : 两个词在一篇文献中同时出现就计 1 次（最常用）
      - "binary" : 每篇文献内每个词对最多计 1 次（与 full 在词表已去重时等价）
      - "fractional": 按 1/(k-1) 加权，k 为文献关键词数，抑制关键词多的文献
    """
    drop_terms = drop_terms or set()
    n_docs = len(docs_terms)

    # 1) 词频统计
    freq: Counter = Counter()
    for terms in docs_terms:
        for t in set(terms):
            if t not in drop_terms:
                freq[t] += 1

    # 2) 阈值筛选
    keep = {t for t, c in freq.items() if c >= min_occurrences}
    if max_terms:
        top = [t for t, _ in freq.most_common() if t in keep][:max_terms]
        keep = set(top)

    # 3) 节点初始化
    net = Network(n_docs=n_docs, counting=counting)
    for i, t in enumerate(sorted(keep, key=lambda x: (-freq[x], x)), start=1):
        net.nodes[i] = Node(id=i, label=t, occurrences=freq[t])

    label2id = net.label_to_id()

    # 4) 共现计数
    for di, terms in enumerate(docs_terms):
        present = sorted({t for t in terms if t in keep})
        if len(present) < 2:
            if len(present) == 1:
                net.nodes[label2id[present[0]]].docs.append(di)
            continue
        if counting == "fractional":
            w = 1.0 / (len(present) - 1)
        else:
            w = 1.0
        for a, b in combinations(present, 2):
            ia, ib = label2id[a], label2id[b]
            key = (ia, ib) if ia < ib else (ib, ia)
            net.edges[key] = net.edges.get(key, 0.0) + w
        for t in present:
            net.nodes[label2id[t]].docs.append(di)

    # 5) 连接强度与年度/被引指标
    for (a, b), s in net.edges.items():
        net.nodes[a].total_link_strength += s
        net.nodes[b].total_link_strength += s

    if records:
        _attach_temporal_metrics(net, docs_terms, records, keep, label2id)

    return net


def _attach_temporal_metrics(net: Network, docs_terms: list[list[str]],
                             records: list, keep: set[str],
                             label2id: dict[str, int]) -> None:
    """计算每个关键词的平均发表年与平均被引（overlay 可视化用）。"""
    years: dict[int, list[float]] = defaultdict(list)
    cits: dict[int, list[float]] = defaultdict(list)

    for di, terms in enumerate(docs_terms):
        if di >= len(records):
            break
        rec = records[di]
        try:
            y = float(rec.year) if rec.year else None
        except (TypeError, ValueError):
            y = None
        c = float(rec.citations) if getattr(rec, "citations", None) is not None else None

        for t in set(terms):
            if t not in keep:
                continue
            nid = label2id[t]
            if y:
                years[nid].append(y)
            if c is not None:
                cits[nid].append(c)

    for nid, node in net.nodes.items():
        ys = years.get(nid, [])
        cs = cits.get(nid, [])
        node.avg_year = sum(ys) / len(ys) if ys else 0.0
        node.avg_citations = sum(cs) / len(cs) if cs else 0.0


# --------------------------------------------------------------------------
# 关联强度（VOSviewer 默认归一化方式，用于报告）
# --------------------------------------------------------------------------

def association_strength(net: Network, i: int, j: int) -> float:
    """关联强度（proximity index）：a_ij = c_ij / (w_i * w_j)。

    VOSviewer 内部即以此归一化后再做布局与聚类。
    """
    key = (i, j) if i < j else (j, i)
    c = net.edges.get(key, 0.0)
    if c == 0:
        return 0.0
    wi = net.nodes[i].occurrences
    wj = net.nodes[j].occurrences
    if wi == 0 or wj == 0:
        return 0.0
    return c / (wi * wj)


# --------------------------------------------------------------------------
# 聚类与质量指标
# --------------------------------------------------------------------------

def louvain_cluster(net: Network, resolution: float = 1.0,
                    seed: int = 42) -> tuple[dict[int, int], float]:
    """用 Louvain 做社区发现，返回 ({节点id: 聚类号}, 模块度Q)。

    优先用 networkx 内置实现；不可用时退化为连通分量。
    """
    try:
        import networkx as nx
    except ImportError:
        return _fallback_cluster(net), 0.0

    g = nx.Graph()
    for nid in net.nodes:
        g.add_node(nid)
    for (a, b), s in net.edges.items():
        if s > 0:
            g.add_edge(a, b, weight=s)

    if g.number_of_edges() == 0:
        return {nid: 1 for nid in net.nodes}, 0.0

    try:
        communities = nx.algorithms.community.louvain_communities(
            g, weight="weight", resolution=resolution, seed=seed
        )
    except Exception:
        communities = list(nx.algorithms.community.greedy_modularity_communities(g, weight="weight"))

    assignment: dict[int, int] = {}
    # 大聚类在前，编号从 1 开始
    ordered = sorted(communities, key=lambda c: -len(c))
    for idx, comm in enumerate(ordered, start=1):
        for nid in comm:
            assignment[nid] = idx

    q = nx.algorithms.community.modularity(g, communities, weight="weight")
    return assignment, float(q)


def _fallback_cluster(net: Network) -> dict[int, int]:
    """networkx 不可用时的降级：按连通分量分组。"""
    seen: set[int] = set()
    assignment: dict[int, int] = {}
    cid = 0
    for nid in net.nodes:
        if nid in seen:
            continue
        cid += 1
        stack = [nid]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            assignment[cur] = cid
            stack.extend(net.neighbors(cur) - seen)
    return assignment


def betweenness(net: Network) -> dict[int, float]:
    """中介中心性：识别"桥接主题"。"""
    try:
        import networkx as nx
    except ImportError:
        return {nid: 0.0 for nid in net.nodes}

    g = nx.Graph()
    g.add_nodes_from(net.nodes)
    for (a, b), s in net.edges.items():
        if s > 0:
            g.add_edge(a, b, weight=1.0 / s if s else 1.0)

    if g.number_of_edges() == 0:
        return {nid: 0.0 for nid in net.nodes}
    return nx.betweenness_centrality(g, weight="weight", normalized=True)


def cluster_profile(net: Network, assignment: dict[int, int]) -> list[dict]:
    """每个聚类的画像：规模、代表词、平均年份、平均被引。"""
    groups: dict[int, list[Node]] = defaultdict(list)
    for nid, cid in assignment.items():
        if nid in net.nodes:
            groups[cid].append(net.nodes[nid])

    profile: list[dict] = []
    for cid, members in sorted(groups.items(), key=lambda x: -len(x[1])):
        members_sorted = sorted(members, key=lambda n: -n.total_link_strength)
        years = [m.avg_year for m in members if m.avg_year > 0]
        cits = [m.avg_citations for m in members if m.avg_citations > 0]
        profile.append({
            "cluster": cid,
            "size": len(members),
            "top_terms": [m.label for m in members_sorted[:8]],
            "total_occurrences": sum(m.occurrences for m in members),
            "avg_year": round(sum(years) / len(years), 1) if years else None,
            "avg_citations": round(sum(cits) / len(cits), 2) if cits else None,
        })
    return profile


# --------------------------------------------------------------------------
# 研究空白探测
# --------------------------------------------------------------------------

def detect_gaps(net: Network, assignment: dict[int, int],
                min_occ: int = 2, top_n: int = 15,
                alpha: float = 0.05) -> dict:
    """找出"该连但没连"的主题对，作为研究空白的客观证据。

    判据：
      - 跨聚类（属于不同主题群）
      - 两者都不算冷门
      - 实际共现次数极低（或为 0）
      - **且这个"低"在统计上显著**（见下）

    期望共现次数用两词频次的乘积除以文献总量估计。

    ⚠️ 为什么要做显著性检验：
    真实数据实测发现，光看「实际/期望」比值会把大量噪声当成研究空白。
    例如 120 篇文献里，"智能家居×居家养老"实际 0 次、期望 1.9 次，
    比值 0 看着很惊人，但超几何检验 p=0.11，**在统计上完全可能是偶然**。
    若把这类结果写进论文，审稿人一算就穿帮。

    检验方法：若两词独立，则「同时出现的文献数」服从超几何分布
    Hypergeometric(N, n_a, n_b)。单侧 p 值 = P(X ≤ 实际观测值)。
    """
    if net.n_docs == 0:
        return {"pairs": [], "isolated": [], "n_tested": 0}

    labels = {nid: n.label for nid, n in net.nodes.items()}
    candidates: list[dict] = []
    n_tested = 0

    ids = [nid for nid, n in net.nodes.items() if n.occurrences >= min_occ]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            ca, cb = assignment.get(a, 0), assignment.get(b, 0)
            if ca == cb:
                continue  # 同聚类不算空白
            wa = net.nodes[a].occurrences
            wb = net.nodes[b].occurrences
            expected = wa * wb / net.n_docs
            if expected < 1.0:
                continue  # 期望共现太低，说明本就无关
            key = (a, b) if a < b else (b, a)
            actual = net.edges.get(key, 0.0)
            ratio = actual / expected if expected else 0.0
            if ratio >= 0.35:
                continue

            n_tested += 1
            p = _hypergeom_tail(net.n_docs, wa, wb, int(actual))
            candidates.append({
                "term_a": labels[a],
                "term_b": labels[b],
                "cluster_a": ca,
                "cluster_b": cb,
                "actual_cooccurrence": round(actual, 2),
                "expected_cooccurrence": round(expected, 2),
                "ratio": round(ratio, 3),
                "p_value": round(p, 4),
                "significant": p < alpha,
                "occ_a": wa,
                "occ_b": wb,
            })

    # 显著的排前面；同为显著时按 p 值升序
    candidates.sort(key=lambda x: (not x["significant"], x["p_value"]))
    sig = [c for c in candidates if c["significant"]]

    return {
        "pairs": candidates[:top_n],
        "significant_pairs": sig[:top_n],
        "n_tested": n_tested,
        "n_significant": len(sig),
        "alpha": alpha,
        "isolated": [],
    }


def _hypergeom_tail(N: int, na: int, nb: int, observed: int) -> float:
    """P(X ≤ observed)，X ~ Hypergeometric(N, na, nb)。

    X = 同时包含两个词的文献数。用于判断「零共现」是否只是偶然。
    """
    from math import comb

    if N <= 0 or na <= 0 or nb <= 0:
        return 1.0
    if observed < 0:
        observed = 0

    lo = max(0, na + nb - N)
    hi = min(na, nb)
    if observed >= hi:
        return 1.0
    if observed < lo:
        return 0.0

    denom = comb(N, nb)
    if denom == 0:
        return 1.0
    total = 0.0
    for x in range(lo, observed + 1):
        # P(X=x) = C(na,x) * C(N-na, nb-x) / C(N, nb)
        total += comb(na, x) * comb(N - na, nb - x) / denom
    return min(1.0, total)


def network_metrics(net: Network, assignment: dict[int, int],
                    q: float, centrality: dict[int, float]) -> dict:
    """汇总可写进论文的网络指标。"""
    n_edges = len([1 for s in net.edges.values() if s > 0])
    densities = []
    for (a, b), s in net.edges.items():
        if s > 0:
            densities.append(association_strength(net, a, b))

    clusters = Counter(assignment.values())
    strong = sorted(net.edges.items(), key=lambda x: -x[1])[:10]

    return {
        "文献总数": net.n_docs,
        "关键词总数": len(net.nodes),
        "连线总数": n_edges,
        "聚类数": len(clusters),
        "聚类规模分布": dict(sorted(clusters.items())),
        "模块度Q": round(q, 4),
        "Q评价": _q_verdict(q),
        "计数方式": net.counting,
        "平均关联强度": round(sum(densities) / len(densities), 5) if densities else 0,
        "最强共现对": [
            {
                "词A": net.nodes[a].label,
                "词B": net.nodes[b].label,
                "共现次数": round(s, 1),
                "关联强度": round(association_strength(net, a, b), 5),
            }
            for (a, b), s in strong
        ],
        "中介中心性Top10": [
            {"关键词": net.nodes[nid].label, "中介中心性": round(v, 4)}
            for nid, v in sorted(centrality.items(), key=lambda x: -x[1])[:10]
        ],
    }


def _q_verdict(q: float) -> str:
    if q >= 0.5:
        return "聚类结构非常清晰（Q>0.5）"
    if q >= 0.3:
        return "聚类结构可接受（0.3≤Q<0.5），适合做主题划分"
    if q > 0:
        return "聚类结构较弱（Q<0.3），主题边界模糊，慎下结论"
    return "无法计算"
