#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键跑通：文献题录 → 共现网络 → VOSviewer 出版级配图 + 分析报告

用法示例：
    python run_pipeline.py --input savedrecs.txt --outdir outputs
    python run_pipeline.py --input wos.txt cnki.txt --min-occurrences 5 --views network overlay density
    python run_pipeline.py --input data.csv --keyword-source both --resolution 1.2

分阶段执行（便于人工介入同义词复核）：
    --stage analyze   只做解析与同义词清单，产出 thesaurus 待确认文件
    --stage render    基于已有中间结果只重新出图
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

# Windows 控制台默认使用 ANSI 代码页（如 cp1252），直接 print 中文会抛
# UnicodeEncodeError 导致脚本崩溃。统一改用 UTF-8，并让无法编码的字符
# 降级为替代符而不是中断执行。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from voslib import (  # noqa: E402
    load_records, build_network, louvain_cluster, betweenness,
    cluster_profile, detect_gaps, network_metrics, detect_env,
)
from voslib import render as R  # noqa: E402
from voslib import thesaurus as T  # noqa: E402
from voslib import vosfiles as V  # noqa: E402

# --------------------------------------------------------------------------
# 进度输出（中文）
# --------------------------------------------------------------------------

_T0 = time.time()

# 预设同义词表所在目录（技能目录下的 presets/）
PRESET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "presets")


def resolve_preset(name: str) -> str | None:
    """把预设名解析成文件路径。

    支持三种写法：
        smart-eldercare_zh          → presets/thesaurus_smart-eldercare_zh.txt
        thesaurus_smart-eldercare_zh.txt  → 同上
        /abs/path/to/file.txt       → 直接用该路径
    """
    if os.path.isfile(name):
        return name
    candidates = [name]
    if not name.lower().endswith(".txt"):
        candidates += [f"{name}.txt", f"thesaurus_{name}.txt"]
    for c in candidates:
        p = os.path.join(PRESET_DIR, c)
        if os.path.isfile(p):
            return p
    return None


def suggest_min_occurrences(freq, target: int = 60) -> int:
    """根据关键词的词频分布，推荐一个大约能保留 target 个关键词的阈值。

    做法：把频次降序排列，取第 target 名的频次作阈值。
    这样用户不用反复试错猜参数——真实数据的词频分布差异很大，
    有的数据出现 5 次就算高频，有的出现 5 次连边都进不去。
    """
    counts = sorted((c for c in freq.values() if c > 0), reverse=True)
    if not counts:
        return 1
    if len(counts) <= target:
        return 1
    return max(1, int(counts[target - 1]))


def step(msg: str) -> None:
    print(f"[{time.time() - _T0:6.1f}s] {msg}", flush=True)


def sub(msg: str) -> None:
    print(f"          └─ {msg}", flush=True)


def banner(title: str) -> None:
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68, flush=True)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    workdir = os.path.join(outdir, "vosviewer_input")
    figdir = os.path.join(outdir, "figures")
    os.makedirs(workdir, exist_ok=True)
    os.makedirs(figdir, exist_ok=True)

    # 只重新出图：复用上一次 analyze / full 留下的中间结果，跳过解析与归并
    if args.stage == "render":
        return _run_render_only(args, outdir, workdir, figdir)

    banner("第一步 · 解析文献题录")
    records, notes = load_records(args.input)
    if not records:
        print("错误：没能从输入文件解析出任何文献记录。")
        print("请确认导出的是『全记录』而不是仅题录，或换用 CSV / RIS / BibTeX 格式。")
        return 2
    for n in notes:
        sub(n)
    with_kw = sum(1 for r in records if r.terms(args.keyword_source))
    sub(f"共 {len(records)} 条文献，其中 {with_kw} 条带关键词"
        f"（{with_kw / len(records) * 100:.1f}%）")
    if with_kw == 0:
        print("\n错误：所有记录都没有关键词。")
        print("WoS 导出请选择『全记录与引用的参考文献』；CNKI 请用 RefWorks/EndNote 格式。")
        return 2

    # 领域相关性过滤：剔除检索污染
    #
    # 真实数据实测教训：宽泛的检索式会抓进大量不相关文献。
    # 用户那份 120 篇的数据里，45 篇（38%）跟养老毫无关系
    # （轨道交通、消防报警、课程思政、旧房装修补贴……），
    # 结果「智能家居」这一簇几乎全是污染，任何基于它的「研究空白」结论都不成立。
    if args.filter_terms:
        keep: list = []
        dropped: list = []
        for r in records:
            haystack = " ".join([
                r.title or "", r.abstract or "",
                " ".join(r.keywords or []), " ".join(r.keywords_plus or []),
            ])
            if any(t in haystack for t in args.filter_terms):
                keep.append(r)
            else:
                dropped.append(r)
        sub(f"领域过滤（白名单：{'、'.join(args.filter_terms)}）")
        sub(f"保留 {len(keep)} 篇，剔除 {len(dropped)} 篇"
            f"（{len(dropped) / len(records) * 100:.0f}%）")
        if dropped:
            sub("剔除样本：" + "；".join(
                (d.title or "")[:24] for d in dropped[:3]))
        if len(keep) < 50:
            print(f"\n警告：过滤后只剩 {len(keep)} 篇文献，样本量偏少。")
            print("      文献计量分析建议 ≥100 篇。可考虑：")
            print("        · 放宽 --filter-terms 的白名单")
            print("        · 扩大检索时间范围后重新导出")
            print("        · 放宽检索式（当前可能过窄或过宽）")
        records = keep
        if not records:
            print("\n错误：过滤后没有剩余文献，请检查 --filter-terms 的设置。")
            return 2

    # ------------------------------------------------------------------
    banner("第二步 · 关键词清洗与同义词归并")
    docs_terms = [r.terms(args.keyword_source) for r in records]
    raw_freq: Counter = Counter()
    for terms in docs_terms:
        for t in set(terms):
            raw_freq[t] += 1
    sub(f"归并前独立关键词 {len(raw_freq)} 个")

    auto_groups = T.find_auto_groups(raw_freq, min_count=1)
    fuzzy = T.find_fuzzy_candidates(raw_freq, threshold=args.similarity,
                                    docs_terms=docs_terms)
    sub(f"自动识别出 {len(auto_groups)} 组形态等价词，"
        f"{len(fuzzy)} 组疑似同义词待确认")

    manual_map: dict[str, str] = {}
    drop: set[str] = set()

    # 领域预设同义词表（省掉大部分人工归并）
    for preset in (args.preset or []):
        ppath = resolve_preset(preset)
        if not ppath:
            print(f"  警告：找不到预设同义词表 {preset}，已跳过")
            continue
        pmap, pdrop = T.read_thesaurus(ppath)
        manual_map.update(pmap)
        drop |= pdrop
        sub(f"已载入预设 {os.path.basename(ppath)}："
            f"{len(pmap)} 条替换规则，{len(pdrop)} 条删除规则")

    if args.thesaurus and os.path.isfile(args.thesaurus):
        umap, udrop = T.read_thesaurus(args.thesaurus)
        manual_map.update(umap)   # 用户表优先级更高
        drop |= udrop
        sub(f"已载入人工 thesaurus：{len(umap)} 条替换规则，{len(udrop)} 条删除规则")

    # 剔除指定关键词（通常用于把「检索词本身」从网络里拿掉）
    #
    # 为什么需要这个：检索词往往出现在半数以上文献里，
    # 它会与几乎所有其他词共现，形成星形枢纽把真实结构压平。
    # 实测：248 篇「主动健康」文献里该词出现 124 次（50%），
    # 出图后呈放射状细长星形，看不出主题聚类。
    if args.exclude_terms:
        drop |= set(args.exclude_terms)
        sub(f"已从网络剔除关键词：{'、'.join(args.exclude_terms)}")

    merged_terms, mapping, mstats = T.merge_terms(
        docs_terms, auto_groups=auto_groups,
        manual_rules=manual_map, drop_terms=drop,
    )
    sub(f"归并后独立关键词 {len({t for ts in merged_terms for t in ts})} 个"
        f"（合并 {mstats['merged']} 次，删除 {mstats['dropped']} 次）")

    # 可选：剔除通用学科词（真实数据里频次极高但无主题区分度）
    if args.drop_generic:
        before = len({t for ts in merged_terms for t in ts})
        merged_terms, dropped_generic = T.drop_generic_from(merged_terms)
        after = len({t for ts in merged_terms for t in ts})
        sub(f"已剔除通用学科词 {len(dropped_generic)} 个"
            f"（关键词 {before} → {after}）")
        if dropped_generic:
            sub("剔除样本：" + "、".join(dropped_generic[:12]))

    # 写出复核清单与 thesaurus 文件，方便人工过一遍
    review_path = os.path.join(outdir, "同义词归并复核清单.md")
    with open(review_path, "w", encoding="utf-8") as fh:
        fh.write(T.build_review_report(raw_freq, auto_groups, fuzzy))

    merged_freq: Counter = Counter()
    for terms in merged_terms:
        for t in set(terms):
            merged_freq[t] += 1

    th_path = os.path.join(workdir, "thesaurus.txt")
    V_path_mapping = dict(mapping)
    T.write_thesaurus(th_path, V_path_mapping, drop)
    sub(f"已写出 thesaurus.txt（{len(V_path_mapping)} 条规则），可手工编辑后再跑一次")

    # ------------------------------------------------------------------
    banner("第三步 · 构建共现网络")
    net = build_network(
        merged_terms, records=records,
        counting=args.counting,
        min_occurrences=args.min_occurrences,
        max_terms=args.max_terms,
        drop_terms=drop,
    )
    sub(f"计数方式：{args.counting}　阈值：出现 ≥ {args.min_occurrences} 次")
    sub(f"入网关键词 {len(net.nodes)} 个，连线 {len(net.edges)} 条")

    if len(net.nodes) < 5:
        print(f"\n警告：当前阈值（出现 ≥ {args.min_occurrences} 次）下入网关键词只有 "
              f"{len(net.nodes)} 个，无法形成有意义的网络图。")
        hint = suggest_min_occurrences(merged_freq, target=60)
        if hint < args.min_occurrences:
            print(f"      建议改用：--min-occurrences {hint}"
                  f"（按你的词频分布，这个值大约能保留 60 个关键词）")
        else:
            print("      你的关键词频次普遍偏低，可能原因：")
            print("        · 文献量不足（文献计量分析建议 ≥100 篇）")
            print("        · 关键词写法过于分散（同一概念有多种表述）")
            print("      可先跑 --stage analyze 看看同义词复核清单，合并后再重跑。")
        return 3

    if len(net.nodes) > 220:
        hint = suggest_min_occurrences(merged_freq, target=80)
        print(f"\n注意：入网关键词 {len(net.nodes)} 个，图会比较拥挤。"
              f"如需精简可用 --min-occurrences {hint}"
              f"（约保留 80 个）或 --max-terms 80。")

    if net.n_docs < 50:
        print(f"\n注意：文献量仅 {net.n_docs} 篇，聚类结果可能不稳定。")
        print("      建议在论文中说明样本量限制，或把结论定位为探索性发现。")

    # 自算一套聚类，用于报告指标
    assignment, q = louvain_cluster(net, resolution=args.resolution)
    for nid, cid in assignment.items():
        net.nodes[nid].cluster = cid
    cent = betweenness(net)
    for nid, v in cent.items():
        net.nodes[nid].betweenness = v

    sub(f"Louvain 聚类得到 {len(set(assignment.values()))} 个聚类，模块度 Q = {q:.4f}")

    # ------------------------------------------------------------------
    banner("第四步 · 写出 VOSviewer 输入文件")
    map_in = os.path.join(workdir, "map.txt")
    net_in = os.path.join(workdir, "network.txt")
    V.write_map(map_in, net, with_layout=False, with_cluster=False)
    V.write_network(net_in, net, sparse=True)
    sub(f"map.txt（{len(net.nodes)} 行）、network.txt（{len(net.edges)} 行）已写出")

    # 只做解析与同义词清单：到此为止，不跑 VOSviewer（秒级完成）
    if args.stage == "analyze":
        report_path = _write_report(outdir, net, assignment, q, cent, raw_freq,
                                    merged_freq, mstats, args, None)
        banner("解析阶段完成")
        print(f"输出目录：{outdir}")
        print(f"  同义词复核清单：同义词归并复核清单.md   ← 请先过一遍这个")
        print(f"  可编辑的同义词表：vosviewer_input/thesaurus.txt")
        print(f"  分析报告：{os.path.basename(report_path)}")
        print()
        print("复核完成后，用以下命令出图（复用本次中间结果，无需重新解析）：")
        print(f'    python {os.path.basename(__file__)} --input <原文件> '
              f'--outdir "{args.outdir}" --stage render')
        return 0

    env = detect_env(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not env.ok():
        print("\n未找到 Java 或 VOSviewer.jar，跳过自动出图。")
        print("  Java  ：", env.java or "（未找到）")
        print("  VOSviewer：", env.jar or "（未找到）")
        print("中间文件已生成，可手工拖进 VOSviewer 出图。")
        _write_report(outdir, net, assignment, q, cent, raw_freq,
                      merged_freq, mstats, args, None)
        return 1

    sub(f"Java：{os.path.basename(os.path.dirname(os.path.dirname(env.java)))}")
    sub(f"VOSviewer：{os.path.basename(env.jar)}")

    # ------------------------------------------------------------------
    # VOSviewer 是 GUI 程序，每次启动都会弹出一个窗口，出图后不会自己退出。
    # 实测可以做两个合并来大幅减少弹窗次数：
    #   1) 「算布局聚类 + 回写结果 + 导出第一个视图」能在同一次启动里完成
    #   2) 一次启动能同时导出多种格式（png + svg + pdf）
    # 于是窗口弹出次数从 (1 + 视图数 × 格式数) 降到 (视图数)。
    views = list(args.views)
    formats = [args.format]
    if args.vector and "svg" not in formats:
        formats.append("svg")

    full_map = os.path.join(workdir, "map_final.txt")
    full_net = os.path.join(workdir, "network_final.txt")

    produced: dict[str, str] = {}
    first_done = False

    def _absorb_result() -> None:
        """读回 VOSviewer 算好的坐标与聚类，并用它重算 Q，保证报告与图一致。

        注意：若启用了 -largest_component，VOSviewer 只回写最大连通分量的节点。
        此时必须把其余节点从 net 中删掉，否则报告会包含图里没有的节点，
        导致「图和报告对不上」。
        """
        nonlocal assignment, q, net
        vnet = V.read_map(full_map)
        if not vnet.nodes:
            return

        kept = set(vnet.nodes)
        dropped = [nid for nid in net.nodes if nid not in kept]
        if dropped:
            for nid in dropped:
                del net.nodes[nid]
            # 同时清掉涉及已删节点的连线
            net.edges = {
                (a, b): s for (a, b), s in net.edges.items()
                if a in net.nodes and b in net.nodes
            }
            sub(f"按 VOSviewer 结果保留最大连通分量，"
                f"移出 {len(dropped)} 个孤立节点")

        for nid, n in vnet.nodes.items():
            if nid in net.nodes:
                net.nodes[nid].cluster = n.cluster
                net.nodes[nid].x = n.x
                net.nodes[nid].y = n.y
        assignment = {nid: n.cluster for nid, n in net.nodes.items()}
        try:
            import networkx as nx
            g = nx.Graph()
            g.add_nodes_from(net.nodes)
            for (a, b), s in net.edges.items():
                if s > 0:
                    g.add_edge(a, b, weight=s)
            groups: dict[int, set] = {}
            for nid, n in net.nodes.items():
                groups.setdefault(n.cluster, set()).add(nid)
            if groups:
                q = float(nx.algorithms.community.modularity(
                    g, list(groups.values()), weight="weight"))
        except Exception:
            pass
        sub(f"已读回 VOSviewer 结果：{len(net.nodes)} 个关键词，"
            f"{len(set(assignment.values()))} 个聚类，Q = {q:.4f}")

    if views:
        first = views[0]
        banner(f"第五步 · 算布局聚类并导出 {first} 视图（合并为一次启动）")
        first_outs = [os.path.join(figdir, f"vosviewer_{first}.{f}") for f in formats]
        res = R.pass_layout_clustering_and_render(
            env, map_in, net_in, first_outs, view=first,
            out_map=full_map, out_network=full_net,
            resolution=args.resolution,
            merge_small_clusters=True,
            largest_component=args.largest_component,
            white_background=args.white_background,
            zoom_level=args.zoom_level,
            min_line_strength=args.min_line_strength,
            scale=args.scale,
        )
        if res["ok"]:
            first_done = True
            produced[first] = first_outs[0]
            mb = os.path.getsize(first_outs[0]) / 1024 / 1024
            extra = f"，另含 {'、'.join(formats[1:])}" if len(formats) > 1 else ""
            sub(f"{first:8s} → {os.path.basename(first_outs[0])}"
                f"（{mb:.1f} MB，{res['seconds']}s{extra}）")
            _absorb_result()
        else:
            sub(f"合并执行失败：{res['error']}")
            sub("回退为「先算布局聚类，再单独出图」")

    if not first_done:
        banner("第五步 · 让 VOSviewer 计算布局与聚类")
        res = R.pass_layout_clustering(
            env, map_in, net_in, full_map, full_net,
            os.path.join(workdir, "map_final.json"),
            resolution=args.resolution,
            merge_small_clusters=True,
            largest_component=args.largest_component,
        )
        if not res["ok"]:
            print(f"  布局聚类失败：{res['error']}")
            print("  改用本地计算结果继续出图。")
            full_map, full_net = map_in, net_in
            V.write_map(full_map, net, with_layout=True, with_cluster=True)
        else:
            sub(f"完成，用时 {res['seconds']}s")
            _absorb_result()

    # 其余视图各启动一次（每次可同时导出多种格式）
    remaining = views[1:] if first_done else views
    if remaining:
        banner("第六步 · 导出其余视图")
        for view in remaining:
            outs = [os.path.join(figdir, f"vosviewer_{view}.{f}") for f in formats]
            r = R.pass_render(
                env, full_map, full_net, outs, view=view,
                white_background=args.white_background,
                zoom_level=args.zoom_level,
                min_line_strength=args.min_line_strength,
                label_size_variation=args.label_size_variation,
                scale=args.scale,
                max_n_lines=args.max_n_lines,
            )
            if r["ok"]:
                produced[view] = outs[0]
                mb = os.path.getsize(outs[0]) / 1024 / 1024
                extra = f"，另含 {'、'.join(formats[1:])}" if len(formats) > 1 else ""
                sub(f"{view:8s} → {os.path.basename(outs[0])}"
                    f"（{mb:.1f} MB，{r['seconds']}s{extra}）")
            else:
                sub(f"{view:8s} → 失败：{r['error']}")

    if not produced:
        print("\n所有视图导出失败。中间文件仍可用，可手工在 VOSviewer 里打开出图。")

    # ------------------------------------------------------------------
    banner("第七步 · 生成分析报告")
    report_path = _write_report(outdir, net, assignment, q, cent, raw_freq,
                                merged_freq, mstats, args, produced)
    sub(f"报告：{os.path.basename(report_path)}")
    sub(f"复核清单：{os.path.basename(review_path)}")

    banner("全部完成")
    print(f"输出目录：{outdir}")
    print(f"  图片    ：{figdir}")
    print(f"  中间文件：{workdir}")
    print(f"  报告    ：{os.path.basename(report_path)}")
    print(f"  复核清单：{os.path.basename(review_path)}")
    print(f"\nVOSviewer 窗口启动 {env.launches} 次"
          f"（已合并「算布局聚类 + 出图」并一次导出多种格式）")
    print(f"总用时 {time.time() - _T0:.1f}s")
    return 0


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def _run_render_only(args: argparse.Namespace, outdir: str,
                     workdir: str, figdir: str) -> int:
    """render 阶段：复用已有的中间文件，只跑 VOSviewer 出图。

    不重新解析题录、不重新归并同义词——适合「复核完 thesaurus 后只想重新出图」。
    """
    banner("复用已有中间结果 · 直接出图")

    map_in = os.path.join(workdir, "map.txt")
    net_in = os.path.join(workdir, "network.txt")
    for f in (map_in, net_in):
        if not os.path.isfile(f):
            print(f"错误：找不到 {f}")
            print("请先用 --stage analyze 或 --stage full 跑一次，生成中间文件。")
            return 2

    net = V.read_map(map_in)
    if not net.nodes:
        print("错误：map.txt 里没有读到任何节点。")
        return 2
    net.edges = V.read_network(net_in)

    # 节点数很少时提示
    sub(f"载入 {len(net.nodes)} 个关键词、{len(net.edges)} 条连线")

    # 从上次的指标文件里补回文献总数与归并统计
    n_docs = 0
    mstats = {"merged": 0, "dropped": 0, "kept": 0}
    meta_path = os.path.join(outdir, "指标.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            n_docs = int((meta.get("metrics") or {}).get("文献总数") or 0)
            mstats = meta.get("merge_stats") or mstats
            sub(f"已载入上次的指标（文献总数 {n_docs}）")
        except Exception:
            pass
    net.n_docs = n_docs

    # 重建聚类与指标
    assignment, q = louvain_cluster(net, resolution=args.resolution)
    for nid, cid in assignment.items():
        net.nodes[nid].cluster = cid
    cent = betweenness(net)
    sub(f"本地 Louvain：{len(set(assignment.values()))} 个聚类，Q = {q:.4f}")

    env = detect_env(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not env.ok():
        print("未找到 Java 或 VOSviewer.jar，无法出图。")
        return 1

    # 若有上次 VOSviewer 算好的结果，且**输入没变**，才复用，省掉一轮布局聚类
    #
    # 这里必须校验新鲜度：analyze 阶段重跑后 map.txt 可能已经变了
    # （比如改了同义词表），此时旧的 map_final.txt 就是过期结果，
    # 直接复用会导致「图和报告对不上」。实测踩过这个坑。
    full_map = os.path.join(workdir, "map_final.txt")
    full_net = os.path.join(workdir, "network_final.txt")
    reuse = False
    if os.path.isfile(full_map) and os.path.isfile(full_net):
        try:
            cached = V.read_map(full_map)
            cached_labels = {n.label for n in cached.nodes.values()}
            current_labels = {n.label for n in net.nodes.values()}
            if cached_labels == current_labels:
                reuse = True
            else:
                only_cached = cached_labels - current_labels
                only_current = current_labels - cached_labels
                sub(f"检测到中间结果已过期（旧 {len(cached_labels)} 词 / 新 "
                    f"{len(current_labels)} 词），将重新计算布局聚类")
                if only_cached:
                    sub(f"  旧结果独有：{'、'.join(sorted(only_cached)[:6])}")
                if only_current:
                    sub(f"  新结果独有：{'、'.join(sorted(only_current)[:6])}")
        except Exception as e:
            sub(f"读取缓存失败（{e}），重新计算")

    if reuse:
        sub("复用上次 VOSviewer 算好的布局与聚类")
        vnet = V.read_map(full_map)
        if vnet.nodes:
            for nid, n in vnet.nodes.items():
                if nid in net.nodes:
                    net.nodes[nid].cluster = n.cluster
                    net.nodes[nid].x = n.x
                    net.nodes[nid].y = n.y
            assignment = {nid: n.cluster for nid, n in net.nodes.items()}
    else:
        banner("让 VOSviewer 计算布局与聚类")
        full_json = os.path.join(workdir, "map_final.json")
        res = R.pass_layout_clustering(
            env, map_in, net_in, full_map, full_net, full_json,
            resolution=args.resolution, merge_small_clusters=True,
            largest_component=args.largest_component,
        )
        if not res["ok"]:
            print(f"  失败：{res['error']}，改用本地布局")
            full_map, full_net = map_in, net_in
            V.write_map(full_map, net, with_layout=True, with_cluster=True)
        else:
            sub(f"完成，用时 {res['seconds']}s")
            vnet = V.read_map(full_map)
            if vnet.nodes:
                for nid, n in vnet.nodes.items():
                    if nid in net.nodes:
                        net.nodes[nid].cluster = n.cluster
                        net.nodes[nid].x = n.x
                        net.nodes[nid].y = n.y
                assignment = {nid: n.cluster for nid, n in net.nodes.items()}

    # 用最终聚类重算 Q
    try:
        import networkx as nx
        g = nx.Graph()
        g.add_nodes_from(net.nodes)
        for (a, b), s in net.edges.items():
            if s > 0:
                g.add_edge(a, b, weight=s)
        groups: dict[int, set] = {}
        for nid, n in net.nodes.items():
            groups.setdefault(n.cluster, set()).add(nid)
        if groups:
            q = float(nx.algorithms.community.modularity(
                g, list(groups.values()), weight="weight"))
    except Exception:
        pass

    banner("导出图片")
    views = list(args.views)
    formats = [args.format]
    if args.vector and "svg" not in formats:
        formats.append("svg")

    produced: dict[str, str] = {}
    for view in views:
        outs = [os.path.join(figdir, f"vosviewer_{view}.{f}") for f in formats]
        r = R.pass_render(
            env, full_map, full_net, outs, view=view,
            white_background=args.white_background,
            zoom_level=args.zoom_level,
            min_line_strength=args.min_line_strength,
            label_size_variation=args.label_size_variation,
            scale=args.scale, max_n_lines=args.max_n_lines,
        )
        if r["ok"]:
            produced[view] = outs[0]
            mb = os.path.getsize(outs[0]) / 1024 / 1024
            extra = f"，另含 {'、'.join(formats[1:])}" if len(formats) > 1 else ""
            sub(f"{view:8s} → {os.path.basename(outs[0])}"
                f"（{mb:.1f} MB，{r['seconds']}s{extra}）")
        else:
            sub(f"{view:8s} → 失败：{r['error']}")

    banner("出图完成")
    print(f"图片目录：{figdir}")
    for view, path in produced.items():
        print(f"  {view:8s} {os.path.basename(path)}")
    print(f"\nVOSviewer 窗口启动 {env.launches} 次")
    return 0


def _write_report(outdir, net, assignment, q, cent, raw_freq, merged_freq,
                  mstats, args, produced) -> str:
    metrics = network_metrics(net, assignment, q, cent)
    profile = cluster_profile(net, assignment)
    gaps = detect_gaps(net, assignment, min_occ=max(2, args.min_occurrences))

    L: list[str] = []
    L.append("# 文献计量网络分析报告\n")
    L.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    L.append("\n## 一、数据概况\n")
    L.append("| 项目 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 文献总数 | {metrics['文献总数']} |")
    L.append(f"| 入网关键词数 | {metrics['关键词总数']} |")
    L.append(f"| 连线总数 | {metrics['连线总数']} |")
    L.append(f"| 聚类数 | {metrics['聚类数']} |")
    L.append(f"| 模块度 Q | {metrics['模块度Q']} |")
    L.append(f"| 聚类质量 | {metrics['Q评价']} |")
    L.append(f"| 计数方式 | {metrics['计数方式']} |")
    L.append(f"| 同义词合并次数 | {mstats['merged']} |")

    L.append("\n### 方法学说明（可直接改写进论文）\n")
    L.append(
        f"本研究以关键词共现为分析单元，采用{'全计数' if args.counting == 'full' else '二进制' if args.counting == 'binary' else '分数计数'}"
        f"方式统计共现频次，关键词入选阈值为出现次数 ≥ {args.min_occurrences} 次，"
        f"同义词经人工核对后合并（共 {mstats['merged']} 次合并操作，规则见 thesaurus.txt）。"
        f"网络布局与聚类均由 VOSviewer（v1.6.21）完成，"
        f"关联强度采用 VOSviewer 默认的关联强度归一化（association strength），"
        f"聚类算法为 VOSviewer 内置的模块度优化方法（分辨率参数 = {args.resolution}）。"
        f"最终得到 {metrics['聚类数']} 个主题聚类，模块度 Q = {metrics['模块度Q']}"
        f"（{metrics['Q评价']}）。"
    )

    L.append("\n## 二、主题聚类画像\n")
    for c in profile:
        terms = "、".join(c["top_terms"])
        L.append(f"\n### 聚类 {c['cluster']}（{c['size']} 个关键词，"
                 f"累计出现 {c['total_occurrences']} 次）\n")
        L.append(f"- 核心关键词：{terms}")
        if c["avg_year"]:
            L.append(f"- 平均发表年：{c['avg_year']:.1f}")
        if c["avg_citations"]:
            L.append(f"- 平均被引：{c['avg_citations']:.1f}")

    L.append("\n## 三、最强共现关系（网络骨架）\n")
    L.append("| 关键词 A | 关键词 B | 共现次数 | 关联强度 |")
    L.append("|---|---|---|---|")
    for e in metrics["最强共现对"]:
        L.append(f"| {e['词A']} | {e['词B']} | {e['共现次数']} | {e['关联强度']} |")

    L.append("\n## 四、桥接主题（中介中心性 Top 10）\n")
    L.append("中介中心性高 = 该主题连接了多个研究群落，是领域内的枢纽概念。\n")
    L.append("| 关键词 | 中介中心性 |")
    L.append("|---|---|")
    for e in metrics["中介中心性Top10"]:
        L.append(f"| {e['关键词']} | {e['中介中心性']} |")

    L.append("\n## 五、研究空白候选（客观证据）\n")
    L.append("判据：两个关键词分属不同聚类（不同研究群落）、各自都不冷门、"
             "实际共现次数低于按频次推算的期望值，**且经超几何检验达到统计显著**。\n")
    L.append("显著性检验逻辑：若两词彼此独立，则「同时出现的文献数」服从超几何分布 "
             "Hypergeometric(N, nA, nB)。p 值 = P(X ≤ 实际观测值)。\n")

    sig = gaps.get("significant_pairs") or []
    n_tested = gaps.get("n_tested", 0)
    L.append(f"共检验 {n_tested} 个跨聚类的低共现词对，"
             f"其中 **{len(sig)} 对达到统计显著**（α = {gaps.get('alpha', 0.05)}）。\n")

    if sig:
        L.append("### 显著的研究空白（可直接作为客观证据）\n")
        L.append("| 关键词 A | 关键词 B | 所属聚类 | 实际共现 | 期望共现 | 实际/期望 | p 值 |")
        L.append("|---|---|---|---|---|---|---|")
        for p in sig:
            star = "★" if p["p_value"] < 0.01 else ""
            L.append(f"| {p['term_a']} | {p['term_b']} | "
                     f"{p['cluster_a']} / {p['cluster_b']} | "
                     f"{p['actual_cooccurrence']} | {p['expected_cooccurrence']} | "
                     f"{p['ratio']} | {p['p_value']} {star} |")
        L.append("\n> p < 0.05 表示：若两个主题真的互不相关，"
                 "观察到这么低的共现次数的概率不足 5%。")
        L.append("> 这类结果可以作为『研究空白』的**客观证据**，"
                 "但仍需结合文献精读说明其学理意义。")
    else:
        L.append("### 未发现统计显著的研究空白\n")
        L.append("当前数据中所有低共现词对都可以用随机波动解释。")
        L.append("常见原因：文献量偏少导致统计功效不足，或该领域主题间本就普遍有交叉。")

    # 未达显著的候选单独列出，明确标注不可直接引用
    weak = [p for p in (gaps.get("pairs") or []) if not p["significant"]]
    if weak:
        L.append("\n### 低共现但**未达显著**的词对（仅作线索，不可直接引用）\n")
        L.append("| 关键词 A | 关键词 B | 实际共现 | 期望共现 | p 值 |")
        L.append("|---|---|---|---|---|")
        for p in weak[:10]:
            L.append(f"| {p['term_a']} | {p['term_b']} | "
                     f"{p['actual_cooccurrence']} | {p['expected_cooccurrence']} | "
                     f"{p['p_value']} |")
        L.append("\n> 这些词对的实际共现确实偏低，但在统计上**无法排除偶然**。")
        L.append("> 可以作为你进一步读文献的线索，但**不要当作研究空白的证据写进论文**——"
                 "审稿人一算 p 值就能反驳。")

    L.append("\n## 六、产出文件\n")
    L.append("| 文件 | 说明 |")
    L.append("|---|---|")
    for view, path in (produced or {}).items():
        L.append(f"| `{os.path.basename(path)}` | VOSviewer {view} 视图 |")
    L.append("| `vosviewer_input/map.txt` | VOSviewer 节点文件（可手工导入微调） |")
    L.append("| `vosviewer_input/network.txt` | VOSviewer 连线文件 |")
    L.append("| `vosviewer_input/thesaurus.txt` | 同义词归并规则（可编辑后重跑） |")
    L.append("| `同义词归并复核清单.md` | 需人工确认的归并项 |")

    L.append("\n## 七、使用限制与注意事项\n")
    L.append(f"- 本分析基于 {net.n_docs} 篇文献。"
             + ("文献量偏少（<100 篇），聚类结果稳定性有限，"
                "结论宜定位为探索性发现。" if net.n_docs < 100 else
                "样本量达到常规文献计量分析的推荐区间。"))
    L.append(f"- 模块度 Q = {metrics['模块度Q']}。"
             + ("聚类结构清晰，可直接用于主题划分。" if q >= 0.3 else
                "聚类结构偏弱，建议在论文中说明主题边界存在交叉。"))
    L.append("- 关键词共现反映的是**研究者的表述习惯**，"
             "不等于知识结构的客观真值，需与文献精读相互印证。")
    L.append("- 若关键词来源为 WoS 的 Keywords Plus，"
             "注意其由数据库算法生成，与作者原意可能有偏差。")

    path = os.path.join(outdir, "分析报告.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    # 同时存一份机器可读的指标
    with open(os.path.join(outdir, "指标.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "metrics": metrics,
            "cluster_profile": profile,
            "gaps": gaps,
            "merge_stats": mstats,
            "params": vars(args),
        }, fh, ensure_ascii=False, indent=2)

    return path


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="文献题录 → 关键词共现网络 → VOSviewer 出版级配图 + 分析报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--input", "-i", nargs="+", required=True,
                   help="文献导出文件，可多个（WoS savedrecs.txt / CNKI / Scopus / RIS / BibTeX / CSV / Excel）")
    p.add_argument("--outdir", "-o", default="outputs", help="输出目录（默认 outputs）")
    p.add_argument("--thesaurus", "-t", default=None,
                   help="人工同义词表（VOSviewer thesaurus 格式，label<TAB>replace by）")
    p.add_argument("--preset", nargs="*", default=None,
                   help="领域预设同义词表，可用：smart-eldercare_zh / smart-eldercare_en；"
                        "也可传自己的文件路径。用户 --thesaurus 优先级更高")
    p.add_argument("--keyword-source", default="keywords",
                   choices=["keywords", "keywords_plus", "both"],
                   help="用哪个字段做共现：作者关键词 / WoS扩展关键词 / 两者合并（默认 keywords）")
    p.add_argument("--counting", default="full",
                   choices=["full", "binary", "fractional"],
                   help="共现计数方式（默认 full）")
    p.add_argument("--min-occurrences", type=int, default=5,
                   help="关键词入选阈值（默认 5，样本量小可降到 2-3）")
    p.add_argument("--max-terms", type=int, default=None,
                   help="最多保留多少个关键词（默认不限）")
    p.add_argument("--resolution", type=float, default=1.0,
                   help="聚类分辨率，越大聚类越多（默认 1.0）")
    p.add_argument("--similarity", type=float, default=0.88,
                   help="疑似同义词的相似度阈值（默认 0.88）")
    p.add_argument("--filter-terms", nargs="*", default=None,
                   help="领域相关词白名单。只保留标题/摘要/关键词中出现其中"
                        "任意一个的文献，用于剔除检索污染。"
                        "例：--filter-terms 养老 老年 老龄化 失能 独居")
    p.add_argument("--exclude-terms", nargs="*", default=None,
                   help="从网络中剔除的关键词。**建议把检索词本身放进来**——"
                        "检索词往往出现在半数以上文献里，会变成星形枢纽把结构压平。"
                        "例：--exclude-terms 主动健康")
    p.add_argument("--drop-generic", action="store_true",
                   help="剔除通用学科词（如 Computer science / 医学 / 研究进展）。"
                        "OpenAlex 数据或 WoS Keywords Plus 强烈建议开启")
    p.add_argument("--views", nargs="+", default=["network", "overlay", "density"],
                   choices=["network", "overlay", "density"],
                   help="导出哪些视图（默认三种都出）")
    p.add_argument("--format", default="png", choices=["png", "pdf", "svg", "tiff", "jpg"],
                   help="图片格式（默认 png；论文排版建议 svg 或 pdf）")
    p.add_argument("--vector", action="store_true",
                   help="额外导出一份 SVG 矢量图")
    p.add_argument("--white-background", default="true",
                   choices=["true", "false"], help="是否白底（默认 true，论文用）")
    p.add_argument("--zoom-level", type=float, default=None, help="缩放级别")
    p.add_argument("--min-line-strength", type=float, default=None,
                   help="隐藏强度低于该值的连线，图更干净")
    p.add_argument("--label-size-variation", type=float, default=None,
                   help="标签大小差异程度")
    p.add_argument("--scale", type=float, default=None, help="整体缩放")
    p.add_argument("--max-n-lines", type=int, default=None, help="最多显示多少条连线")
    p.add_argument("--largest-component", action="store_true",
                   help="只保留最大连通分量（去掉孤立小团）")
    p.add_argument("--stage", default="full",
                   choices=["full", "analyze", "render"],
                   help="分阶段执行。analyze=只解析并产出同义词清单（不跑 VOSviewer，秒级完成），"
                        "复核完再跑 render=复用已有中间结果只出图。默认 full 一次跑完")
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.white_background = args.white_background == "true"
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
