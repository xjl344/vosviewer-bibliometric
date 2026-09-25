#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成中文（CNKI 格式）合成测试数据，用于验证中文关键词解析与共现分析。

重要：这是**合成数据**，仅用于测试程序，不具备学术意义。
真实分析请替换为你从 CNKI 导出的真实文件。

支持两种 CNKI 导出格式：
    refworks  CNKI「导出与分析 → RefWorks」格式（K1 关键词）
    endnote   CNKI「导出与分析 → EndNote」格式（%K 关键词）

数据设计：模拟「智慧养老 / 独居老人」领域 180 篇中文文献，
四个主题群落刻意呈现不同连接强度，其中「主动健康 / 无感交互」群落
与其余群落连接稀疏——用来验证研究空白探测能否命中。
"""

import os
import random
import sys

random.seed(20260923)

# 四个中文主题群落
# 每个群落里混入同义词变体（空巢老人/独居长者、智能穿戴/可穿戴设备 等），
# 用来验证预设同义词表能否正确归并。
CLUSTER_A = ["独居老人", "空巢老人", "独居长者", "居家养老", "家庭养老",
             "人口老龄化", "老龄化社会", "老年照护", "养老需求", "社区养老"]
CLUSTER_B = ["智能家居", "智慧家庭", "物联网", "物联网技术", "智能穿戴",
             "可穿戴设备", "传感器网络", "无线传感器网络", "适老化改造",
             "辅助技术", "智能终端"]
CLUSTER_C = ["健康监测", "健康监护", "跌倒检测", "摔倒检测", "生命体征",
             "远程监护", "远程监控", "行为识别", "人体行为识别",
             "异常行为预警"]
CLUSTER_D = ["主动健康", "无感交互", "无接触交互", "可供性", "示能性",
             "隐形智能", "环境智能", "情境感知", "人机交互"]

# 群落间连接倾向
AFFINITY = {
    ("A", "B"): 0.72, ("B", "C"): 0.68, ("A", "C"): 0.62,
    ("A", "D"): 0.09, ("B", "D"): 0.13, ("C", "D"): 0.07,
}

JOURNALS = [
    "中国老年学杂志", "人口与发展", "中国全科医学", "计算机应用研究",
    "中国数字医学", "现代情报", "包装工程", "建筑学报",
    "中国公共卫生", "情报杂志", "医学信息学杂志", "智能系统学报",
]
INSTITUTIONS = [
    "清华大学", "北京大学", "浙江大学", "同济大学", "东南大学",
    "北京邮电大学", "华中科技大学", "上海交通大学", "中国人民大学", "华南理工大学",
]
AUTHORS = [
    "王建国", "李明华", "张伟", "刘洋", "陈浩", "赵敏", "孙丽",
    "周强", "吴静", "郑凯", "冯雪", "蒋涛", "韩磊", "曹颖",
]


def make_record(idx: int) -> dict:
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
    if random.random() < 0.16:
        combo = combo + ["D"]

    pool = {"A": CLUSTER_A, "B": CLUSTER_B, "C": CLUSTER_C, "D": CLUSTER_D}
    kws: list[str] = []
    for c in combo:
        k = 2 if c == "D" else random.randint(2, 4)
        kws += random.sample(pool[c], min(k, len(pool[c])))
    kws = list(dict.fromkeys(kws))
    random.shuffle(kws)
    kws = kws[:random.randint(3, 7)]

    year = random.choices(
        [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024],
        weights=[3, 4, 6, 8, 11, 14, 16, 18, 20],
    )[0]

    n_authors = random.randint(1, 3)
    authors = ";".join(random.sample(AUTHORS, n_authors))
    journal = random.choice(JOURNALS)
    inst = random.choice(INSTITUTIONS)

    return {
        "authors": authors,
        "title": f"面向{'与'.join(kws[:2])}的智慧养老研究",
        "journal": journal,
        "year": str(year),
        "volume": str(random.randint(1, 24)),
        "issue": str(random.randint(1, 18)),
        "keywords": ";".join(kws),
        "abstract": f"针对{','.join(kws)}等问题，本文提出了一种研究方法。",
        "institution": inst,
        "idx": idx,
    }


def write_refworks(path: str, n: int) -> None:
    """CNKI RefWorks 格式。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        for i in range(1, n + 1):
            r = make_record(i)
            fh.write("RT Journal Article\n")
            fh.write(f"SR {i}\n")
            fh.write(f"A1 {r['authors']}\n")
            fh.write(f"AD {r['institution']}\n")
            fh.write(f"T1 {r['title']}\n")
            fh.write(f"JF {r['journal']}\n")
            fh.write(f"YR {r['year']}\n")
            fh.write(f"VO {r['volume']}\n")
            fh.write(f"IS {r['issue']}\n")
            fh.write(f"K1 {r['keywords']}\n")
            fh.write(f"AB {r['abstract']}\n")
            fh.write("ER\n\n")


def write_endnote(path: str, n: int) -> None:
    """CNKI EndNote 格式。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        for i in range(1, n + 1):
            r = make_record(i)
            fh.write("%0 Journal Article\n")
            fh.write(f"%A {r['authors']}\n")
            fh.write(f"%T {r['title']}\n")
            fh.write(f"%J {r['journal']}\n")
            fh.write(f"%D {r['year']}\n")
            fh.write(f"%V {r['volume']}\n")
            fh.write(f"%N {r['issue']}\n")
            fh.write(f"%K {r['keywords']}\n")
            fh.write(f"%X {r['abstract']}\n")
            fh.write(f"%I {r['institution']}\n")
            fh.write("\n")


def main() -> int:
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    os.makedirs(outdir, exist_ok=True)

    rw = os.path.join(outdir, "cnki_refworks.txt")
    en = os.path.join(outdir, "cnki_endnote.txt")
    write_refworks(rw, n)
    write_endnote(en, n)
    print(f"已生成 CNKI 合成测试数据：")
    print(f"  {rw}（{n} 条，RefWorks 格式）")
    print(f"  {en}（{n} 条，EndNote 格式）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
