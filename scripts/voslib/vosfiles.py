"""VOSviewer 原生文件的读写。

格式规范来自官方手册第 4 章 + 官方示例文件（data/ 目录）实测：
  - 文件编码：UTF-8 with BOM
  - 分隔符：制表符
  - 首行为表头

支持的文件类型：
  map.txt            节点表：id, label, x, y, cluster, weight<...>, score<...>
  network.txt        连线表（稀疏）：id1, id2, strength
  thesaurus.txt      同义词表：label, replace by
  cluster_colors.txt 聚类配色：cluster, red, green, blue
  overlay_colors.txt 叠加配色：color value, red, green, blue
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict

from .network import Network


# --------------------------------------------------------------------------
# 写 map 文件
# --------------------------------------------------------------------------

def write_map(path: str, net: Network,
              with_layout: bool = True,
              with_cluster: bool = True,
              include_year: bool = True,
              include_citations: bool = True) -> None:
    """写出 VOSviewer map 文件。

    同时写入多个 weight<...> 与 score<...> 列，这样在 VOSviewer 里
    切换 Overlay 时可以直接选"平均发表年""平均被引"，做趋势分析。
    """
    header = ["id", "label", "description"]
    if with_layout:
        header += ["x", "y"]
    if with_cluster:
        header += ["cluster"]
    header += ["weight<Occurrences>", "weight<Total link strength>"]
    if include_year:
        header += ["score<Avg. pub. year>"]
    if include_citations:
        header += ["score<Avg. citations>"]

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\t".join(header) + "\n")
        for nid in sorted(net.nodes):
            n = net.nodes[nid]
            row: list[str] = [str(n.id), n.label, ""]
            if with_layout:
                row += [f"{n.x:.4f}", f"{n.y:.4f}"]
            if with_cluster:
                row += [str(n.cluster if n.cluster else 1)]
            row += [str(n.occurrences), f"{n.total_link_strength:.0f}"]
            if include_year:
                row += [f"{n.avg_year:.1f}" if n.avg_year else ""]
            if include_citations:
                row += [f"{n.avg_citations:.2f}" if n.avg_citations else ""]
            fh.write("\t".join(row) + "\n")


# --------------------------------------------------------------------------
# 写 network 文件
# --------------------------------------------------------------------------

def write_network(path: str, net: Network, sparse: bool = True) -> None:
    """写出 VOSviewer network 文件。

    稀疏格式每行一条连线：id1 <TAB> id2 <TAB> 强度
    这里写的是原始共现次数，归一化交给 VOSviewer 内部处理（更权威）。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        if sparse:
            for (a, b), s in sorted(net.edges.items()):
                if s <= 0:
                    continue
                strength = f"{s:.0f}" if float(s).is_integer() else f"{s:.4f}"
                fh.write(f"{a}\t{b}\t{strength}\n")
        else:
            ids = sorted(net.nodes)
            fh.write("\t".join(str(i) for i in ids) + "\n")
            mat = defaultdict(float)
            for (a, b), s in net.edges.items():
                mat[(a, b)] = s
                mat[(b, a)] = s
            for i in ids:
                row = [str(i)] + [
                    (f"{mat[(i, j)]:.0f}" if float(mat[(i, j)]).is_integer()
                     else f"{mat[(i, j)]:.4f}")
                    for j in ids
                ]
                fh.write("\t".join(row) + "\n")


# --------------------------------------------------------------------------
# 写配色文件
# --------------------------------------------------------------------------

def write_cluster_colors(path: str, colors: dict[int, tuple[int, int, int]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("cluster\tred\tgreen\tblue\n")
        for cid in sorted(colors):
            r, g, b = colors[cid]
            fh.write(f"{cid}\t{r}\t{g}\t{b}\n")


def write_overlay_colors(path: str,
                         stops: list[tuple[float, tuple[int, int, int]]]) -> None:
    """stops: [(0.0,(r,g,b)), (0.5,...), (1.0,...)]，做蓝-绿-黄的年份渐变。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("color value\tred\tgreen\tblue\n")
        for v, (r, g, b) in stops:
            fh.write(f"{v}\t{r}\t{g}\t{b}\n")


def default_cluster_colors(n: int) -> dict[int, tuple[int, int, int]]:
    """内置一套高对比度聚类配色（尽量接近 VOSviewer 观感）。"""
    palette = [
        (225, 131, 47), (162, 152, 223), (204, 65, 192), (3, 60, 96),
        (229, 255, 67), (0, 128, 128), (220, 20, 60), (60, 179, 113),
        (255, 165, 0), (106, 90, 205), (70, 130, 180), (178, 34, 34),
    ]
    return {i: palette[(i - 1) % len(palette)] for i in range(1, n + 1)}


# --------------------------------------------------------------------------
# 读回 VOSviewer 计算后的 map 文件
# --------------------------------------------------------------------------

def read_map(path: str) -> Network:
    """读回 VOSviewer 保存的 map 文件（含 x / y / cluster）。"""
    net = Network()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            return net

        cols = {h.strip().lower(): i for i, h in enumerate(header)}

        def idx(*names: str) -> int | None:
            for n in names:
                if n in cols:
                    return cols[n]
            return None

        i_id = idx("id")
        i_label = idx("label")
        i_x = idx("x")
        i_y = idx("y")
        i_cluster = idx("cluster")

        # weight<...> 列：优先取 total link strength，其次 occurrences
        i_w = None
        i_tls = None
        i_year = None
        i_cit = None
        for name, i in cols.items():
            if name.startswith("weight<"):
                if "total link" in name:
                    i_tls = i
                elif i_w is None:
                    i_w = i
            if name.startswith("score<"):
                if "year" in name:
                    i_year = i
                elif "cit" in name and i_cit is None:
                    i_cit = i

        for row in reader:
            if not row or not any(c.strip() for c in row):
                continue

            def get(i: int | None, default: str = "") -> str:
                if i is None or i >= len(row):
                    return default
                return (row[i] or "").strip()

            label = get(i_label)
            raw_id = get(i_id)
            if not label and not raw_id:
                continue

            try:
                nid = int(raw_id) if raw_id else hash(label) % 10**6
            except ValueError:
                nid = hash(label) % 10**6

            from .network import Node  # 延迟导入避免循环
            node = Node(id=nid, label=label or raw_id)

            def num(s: str, default: float = 0.0) -> float:
                try:
                    return float(s)
                except (TypeError, ValueError):
                    return default

            node.x = num(get(i_x))
            node.y = num(get(i_y))
            node.cluster = int(num(get(i_cluster), 1))
            node.occurrences = int(num(get(i_w), 0))
            node.total_link_strength = int(num(get(i_tls), 0))
            node.avg_year = num(get(i_year))
            node.avg_citations = num(get(i_cit))
            net.nodes[nid] = node

    return net


def read_network(path: str) -> dict[tuple[int, int], float]:
    """读回 network 文件（稀疏格式）。"""
    edges: dict[tuple[int, int], float] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        for row in reader:
            if len(row) < 2:
                continue
            try:
                a = int(row[0])
                b = int(row[1])
                s = float(row[2]) if len(row) > 2 and row[2].strip() else 1.0
            except (ValueError, IndexError):
                continue
            key = (a, b) if a < b else (b, a)
            edges[key] = edges.get(key, 0.0) + s
    return edges
