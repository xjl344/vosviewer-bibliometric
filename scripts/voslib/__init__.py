"""voslib —— 文献计量网络分析与 VOSviewer 自动出图工具包。

模块划分：
  parsers    题录解析（WoS / Scopus / CNKI / RIS / BibTeX / CSV / Excel）
  thesaurus  同义词归并（自动 + 相似度候选 + 人工复核清单）
  network    共现矩阵、节点指标、Louvain 聚类、研究空白探测
  vosfiles   VOSviewer 原生文件读写（map / network / colors / thesaurus）
  render     驱动 VOSviewer 命令行出图（两趟：算布局聚类 → 导图）
"""

__version__ = "1.0.0"

from .parsers import Record, load_records, parse_file  # noqa: F401
from .network import (  # noqa: F401
    Network, Node, build_network, louvain_cluster,
    betweenness, cluster_profile, detect_gaps, network_metrics,
)
from .render import detect_env, VosEnv  # noqa: F401

__all__ = [
    "Record", "load_records", "parse_file",
    "Network", "Node", "build_network", "louvain_cluster",
    "betweenness", "cluster_profile", "detect_gaps", "network_metrics",
    "detect_env", "VosEnv",
]
