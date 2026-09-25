#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成一份合成测试数据（WoS 制表符分隔格式），用于端到端验证管线。

重要：这是**合成数据**，仅用于测试程序是否跑通，不具备任何学术意义。
真实分析请替换为你从 Web of Science / CNKI 导出的真实文件。

数据设计：模拟"智慧养老"领域 200 篇文献，故意让四个主题群落呈现不同连接强度：
  群落 A 独居老人 / 居家养老      —— 与 B、C 连接密集
  群落 B 智能家居 / 物联网        —— 与 A、C 连接密集
  群落 C 健康监测 / 可穿戴        —— 与 A、B 连接密集
  群落 D 无感交互 / 可供性        —— 与前三个群落连接稀疏（模拟研究空白）
"""

import os
import random
import sys

random.seed(20260923)

# 四个主题群落的关键词池
CLUSTER_A = ["solitary living", "older adult", "aging in place",
             "independent living", "home care", "elderly care"]
CLUSTER_B = ["smart home", "internet of things", "ambient assisted living",
             "sensor network", "home automation", "assistive technology"]
CLUSTER_C = ["health monitoring", "wearable sensor", "fall detection",
             "vital signs", "remote monitoring", "activity recognition"]
CLUSTER_D = ["unobtrusive sensing", "affordance", "invisible intelligence",
             "implicit interaction", "calm technology", "ambient intelligence"]

# 连接权重：A-B、B-C、A-C 密集；D 与它们都稀疏
AFFINITY = {
    ("A", "B"): 0.75, ("B", "C"): 0.70, ("A", "C"): 0.65,
    ("A", "D"): 0.10, ("B", "D"): 0.12, ("C", "D"): 0.08,
}

SOURCES = [
    "SENSORS", "IEEE ACCESS", "J AMB INTEL SMART ENVIR",
    "INT J ENV RES PUB HE", "J MED INTERNET RES", "GERONTOLOGIST",
    "COMPUT HUM BEHAV", "PERVASIVE MOB COMPUT", "JMIR MHEALTH UHEALTH",
    "ARCH GERONTOL GERIAT", "IEEE INTERNET THINGS J", "APPL SCI-BASEL",
]
COUNTRIES = ["Peoples R China", "USA", "England", "Netherlands", "Japan",
             "Germany", "South Korea", "Canada", "Australia", "Sweden"]


def pick_cluster_terms(cluster: str, n: int, pool: dict) -> list[str]:
    """从某个群落抽 n 个关键词。"""
    terms = pool[cluster]
    return random.sample(terms, min(n, len(terms)))


def make_record(idx: int) -> dict:
    """造一条文献记录。"""
    # 决定这篇文献主要属于哪个群落组合
    r = random.random()
    if r < 0.30:
        combo = ["A", "B", "C"]
    elif r < 0.50:
        combo = ["A", "B"]
    elif r < 0.68:
        combo = ["B", "C"]
    elif r < 0.82:
        combo = ["A", "C"]
    elif r < 0.90:
        combo = ["A"]
    elif r < 0.95:
        combo = ["B"]
    else:
        combo = ["C"]

    # 少数文献涉及 D（研究空白主题）
    if random.random() < 0.18:
        combo = combo + ["D"]

    pool = {"A": CLUSTER_A, "B": CLUSTER_B, "C": CLUSTER_C, "D": CLUSTER_D}
    keywords: list[str] = []
    for c in combo:
        k = 2 if c == "D" else random.randint(2, 4)
        keywords += pick_cluster_terms(c, k, pool)

    # 去重、限制总数（模拟真实关键词数量 3-8 个）
    keywords = list(dict.fromkeys(keywords))
    random.shuffle(keywords)
    keywords = keywords[:random.randint(4, 8)]

    year = random.choices(
        [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024],
        weights=[3, 4, 5, 7, 9, 12, 14, 16, 15, 15],
    )[0]

    # 年份越新被引越少
    base_cit = max(1, int(random.gauss(28 - (year - 2015) * 2.4, 12)))

    src = random.choice(SOURCES)
    return {
        "PT": "J",
        "AU": "Zhang, Wei; Liu, Yang; Chen, Hao",
        "TI": f"Smart eldercare study on {' '.join(keywords[:3])} ({idx})",
        "SO": src,
        "DE": "; ".join(keywords),
        "ID": "; ".join(random.sample(
            [k for c in pool.values() for k in c], 5)),
        "AB": "This study investigates " + ", ".join(keywords) +
              " in the context of ageing in place.",
        "PY": str(year),
        "TC": str(base_cit),
        "DI": f"10.1000/test.{idx:04d}",
        "C1": f"[Zhang, Wei] Univ Test, {random.choice(COUNTRIES)}",
        "CR": "; ".join(f"Ref, A {i}" for i in range(random.randint(10, 40))),
    }


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "testdata_wos.txt"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 200

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("FN Clarivate Analytics Web of Science\n")
        fh.write("VR 1.0\n")
        for i in range(1, n + 1):
            rec = make_record(i)
            for code in ["PT", "AU", "TI", "SO", "DE", "ID", "AB",
                         "PY", "TC", "DI", "C1", "CR"]:
                fh.write(f"{code} {rec[code]}\n")
            fh.write("ER\n\n")
        fh.write("EF\n")

    print(f"已生成合成测试数据：{out}（{n} 条记录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
