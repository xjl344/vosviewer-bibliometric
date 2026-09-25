"""驱动 VOSviewer 出图。

核心机制（已实测验证）：
  VOSviewer 支持命令行参数，启动时会按参数打开/创建图谱并**直接写出图片**，
  但它是 GUI 程序，出图后不会自己退出，所以流程是：
      启动 → 轮询等待目标文件出现 → 关闭进程

参数清单来自官方手册 5.1 节，关键几个：
  -map / -network            指定输入的 map / network 文件
  -run_layout                强制重算布局
  -run_clustering            强制重算聚类
  -resolution                聚类分辨率（越大聚类越多）
  -network_visualization     网络视图
  -overlay_visualization     叠加视图（按 score 上色，可看年份趋势）
  -density_visualization     密度视图
  -white_background true     白底（论文用）
  -save_map / -save_network  回写计算结果
  -save_screenshot_png/pdf/svg/tiff/jpg/bmp/eps/emf  导出图片

实测输出尺寸约 3213×1714 像素，满足期刊出版要求；
需要矢量图时用 svg 或 pdf。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# 定位 Java 与 VOSviewer
# --------------------------------------------------------------------------

def runtime_dir() -> str:
    """共享运行时目录，存放 Java 与 VOSviewer.jar，跨项目复用。

    解析顺序：
      1. 环境变量 `VOSVIEWER_RUNTIME`（用户显式指定）
      2. 默认 `~/.vosviewer-bibliometric/runtime`
    """
    env = os.environ.get("VOSVIEWER_RUNTIME")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.expanduser("~"), ".vosviewer-bibliometric", "runtime")


JAVA_PATTERNS = [
    # 共享运行时（首选）
    "jre/bin/java.exe", "jre/bin/java",
    "jre/*/bin/java.exe",
    # 本技能目录下自带
    "runtime/*/bin/java.exe", "runtime/*/bin/java",
    # 系统安装
    "C:/Program Files/Java/*/bin/java.exe",
    "C:/Program Files/Eclipse Adoptium/*/bin/java.exe",
    "C:/Program Files/Zulu/*/bin/java.exe",
    "C:/Program Files/Microsoft/jdk-*/bin/java.exe",
    "/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java",
    "/usr/lib/jvm/*/bin/java",
]


def find_java(base_dir: str | None = None) -> str | None:
    """查找可用的 java 可执行文件。"""
    import glob

    search_dirs = [runtime_dir()]
    if base_dir:
        search_dirs.append(base_dir)
    search_dirs.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    for root in search_dirs:
        if not root or not os.path.isdir(root):
            continue
        for pat in JAVA_PATTERNS:
            for hit in glob.glob(os.path.join(root, pat)):
                if os.path.isfile(hit):
                    return hit

    which = shutil.which("java")
    if which:
        return which

    jh = os.environ.get("JAVA_HOME")
    if jh:
        for name in ("java.exe", "java"):
            p = os.path.join(jh, "bin", name)
            if os.path.isfile(p):
                return p
    return None


def find_vosviewer(base_dir: str | None = None) -> str | None:
    """查找 VOSviewer.jar。"""
    import glob

    roots = [runtime_dir()]
    if base_dir:
        roots.append(base_dir)
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for pat in ("VOSviewer.jar", "vosviewer/VOSviewer.jar",
                    "**/VOSviewer.jar"):
            for hit in glob.glob(os.path.join(root, pat), recursive=True):
                if os.path.isfile(hit) and os.path.basename(hit).lower().startswith("vosviewer"):
                    return hit
    return None


@dataclass
class VosEnv:
    java: str
    jar: str
    memory_mb: int = 2000
    timeout: int = 180
    log: list[str] = field(default_factory=list)
    launches: int = 0          # VOSviewer 窗口启动次数（用于向用户说明弹窗次数）

    def ok(self) -> bool:
        return bool(self.java and os.path.isfile(self.java)
                    and self.jar and os.path.isfile(self.jar))


def detect_env(base_dir: str | None = None) -> VosEnv:
    return VosEnv(java=find_java(base_dir) or "", jar=find_vosviewer(base_dir) or "")


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def _kill(proc: subprocess.Popen) -> None:
    """确保 VOSviewer 进程被关掉（它是 GUI 程序，不会自己退出）。"""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        # 最后兜底：按 PID 强杀
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass


def run_vosviewer(env: VosEnv, args: list[str],
                  outputs: list[str],
                  wait_extra: float = 1.5,
                  cwd: str | None = None) -> dict:
    """启动 VOSviewer 直到指定输出文件全部生成，然后关闭进程。

    返回 {"ok": bool, "produced": [...], "missing": [...], "seconds": float, "error": str}
    """
    if not env.ok():
        return {"ok": False, "produced": [], "missing": outputs,
                "seconds": 0.0,
                "error": "未找到 Java 或 VOSviewer.jar，请先运行 setup 检查环境"}

    for o in outputs:
        if os.path.exists(o):
            try:
                os.remove(o)
            except OSError:
                pass

    cmd = [env.java, f"-Xmx{env.memory_mb}m", "-jar", env.jar] + args

    env.launches += 1
    started = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=cwd or os.path.dirname(os.path.abspath(env.jar)),
    )

    produced: list[str] = []
    try:
        while time.time() - started < env.timeout:
            if proc.poll() is not None and not _all_exist(outputs):
                # 进程提前退出且没产出，直接失败
                if time.time() - started > 3:
                    break
            if _all_exist(outputs):
                # 等文件写完整（尺寸稳定）
                if _stable(outputs, wait_extra):
                    produced = list(outputs)
                    break
            time.sleep(0.5)
    finally:
        _kill(proc)

    elapsed = round(time.time() - started, 1)
    missing = [o for o in outputs if not os.path.exists(o)]
    result = {
        "ok": not missing,
        "produced": produced if not missing else [],
        "missing": missing,
        "seconds": elapsed,
        "error": "" if not missing else f"超时 {env.timeout}s，未生成：{missing}",
    }
    env.log.append(f"VOSviewer 执行 {elapsed}s，参数：{' '.join(args[:6])}... → "
                   f"{'成功' if result['ok'] else '失败'}")
    return result


def _all_exist(paths: list[str]) -> bool:
    return all(os.path.exists(p) and os.path.getsize(p) > 0 for p in paths)


def _stable(paths: list[str], wait: float) -> bool:
    """等一小会儿确认文件大小不再变化，避免读到写了一半的图。"""
    first = [os.path.getsize(p) for p in paths]
    time.sleep(wait)
    second = [os.path.getsize(p) for p in paths]
    return first == second and all(s > 0 for s in second)


# --------------------------------------------------------------------------
# 高层封装：两趟流程
# --------------------------------------------------------------------------

def pass_layout_clustering(env: VosEnv, map_file: str, network_file: str,
                           out_map: str, out_network: str,
                           out_json: str | None = None,
                           resolution: float = 1.0,
                           min_cluster_size: int | None = None,
                           merge_small_clusters: bool = True,
                           largest_component: bool = False,
                           thesaurus: str | None = None,
                           min_occurrences: int | None = None) -> dict:
    """第一趟：让 VOSviewer 跑布局与聚类，并把结果回写。

    这样聚类结果就是 VOSviewer 自己的算法产出，写进论文完全站得住脚。
    """
    args = ["-map", map_file, "-network", network_file,
            "-run_layout", "-run_clustering",
            "-resolution", str(resolution),
            "-save_map", out_map, "-save_network", out_network]
    if out_json:
        args += ["-save_json", out_json]
    if min_cluster_size is not None:
        args += ["-min_cluster_size", str(min_cluster_size)]
    args += ["-merge_small_clusters", "true" if merge_small_clusters else "false"]
    if largest_component:
        args += ["-largest_component"]
    if thesaurus:
        args += ["-thesaurus", thesaurus]
    if min_occurrences is not None:
        args += ["-min_n_occurrences", str(min_occurrences)]

    outs = [out_map, out_network] + ([out_json] if out_json else [])
    return run_vosviewer(env, args, outs)


VIEW_PARAMS = {
    "network": ["-network_visualization"],
    "overlay": ["-overlay_visualization"],
    "density": ["-density_visualization"],
}


def _screenshot_args(out_images: list[str]) -> list[str]:
    """把多个输出路径转成对应的 -save_screenshot_* 参数。

    VOSviewer 支持在**一次启动**里同时导出多种格式，
    所以 png + svg + pdf 可以一次搞定，不必每个格式启动一次。
    实测确认可行。
    """
    args: list[str] = []
    seen: set[str] = set()
    for path in out_images:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext not in ("png", "pdf", "svg", "tiff", "tif", "jpg", "jpeg",
                       "bmp", "eps", "emf", "gif"):
            ext = "png"
        # 同格式只保留第一个，避免参数重复
        if ext in seen:
            continue
        seen.add(ext)
        args += [f"-save_screenshot_{ext}", path]
    return args


def pass_render(env: VosEnv, map_file: str, network_file: str,
                out_image: str | list[str], view: str = "network",
                white_background: bool = True,
                zoom_level: float | None = None,
                min_line_strength: float | None = None,
                label_size_variation: float | None = None,
                cluster_colors: str | None = None,
                overlay_colors: str | None = None,
                scale: float | None = None,
                max_n_lines: int | None = None,
                min_score: float | None = None,
                max_score: float | None = None,
                extra_args: list[str] | None = None) -> dict:
    """按指定视图导出图片。

    out_image 可以是单个路径，也可以是路径列表——**列表会在同一次启动里
    导出多种格式**，减少 VOSviewer 窗口的弹出次数。

    输出格式由扩展名决定：png / pdf / svg / tiff / jpg / bmp / eps / emf
    """
    outputs = [out_image] if isinstance(out_image, str) else list(out_image)
    if not outputs:
        return {"ok": False, "produced": [], "missing": [],
                "seconds": 0.0, "error": "没有指定输出文件"}

    args = ["-map", map_file, "-network", network_file]
    args += VIEW_PARAMS.get(view, VIEW_PARAMS["network"])
    args += ["-white_background", "true" if white_background else "false"]

    if zoom_level is not None:
        args += ["-zoom_level", str(zoom_level)]
    if min_line_strength is not None:
        args += ["-min_line_strength", str(min_line_strength)]
    if label_size_variation is not None:
        args += ["-label_size_variation", str(label_size_variation)]
    if scale is not None:
        args += ["-scale", str(scale)]
    if max_n_lines is not None:
        args += ["-max_n_lines", str(max_n_lines)]
    if min_score is not None:
        args += ["-min_score", str(min_score)]
    if max_score is not None:
        args += ["-max_score", str(max_score)]
    if cluster_colors:
        args += ["-cluster_colors", cluster_colors]
    if overlay_colors:
        args += ["-overlay_colors", overlay_colors]
    if extra_args:
        args += extra_args

    args += _screenshot_args(outputs)
    return run_vosviewer(env, args, outputs)


def pass_layout_clustering_and_render(
        env: VosEnv, map_file: str, network_file: str,
        out_images: list[str], view: str = "network",
        out_map: str | None = None, out_network: str | None = None,
        resolution: float = 1.0,
        merge_small_clusters: bool = True,
        largest_component: bool = False,
        white_background: bool = True,
        zoom_level: float | None = None,
        min_line_strength: float | None = None,
        scale: float | None = None) -> dict:
    """**一次启动**完成：算布局 + 算聚类 + 回写结果 + 导出图片。

    实测确认 VOSviewer 会先跑完布局聚类再截图，所以这三件事可以合并，
    把原本两次窗口弹出压成一次。
    """
    args = ["-map", map_file, "-network", network_file,
            "-run_layout", "-run_clustering",
            "-resolution", str(resolution),
            "-merge_small_clusters", "true" if merge_small_clusters else "false"]
    if largest_component:
        args += ["-largest_component"]
    if out_map:
        args += ["-save_map", out_map]
    if out_network:
        args += ["-save_network", out_network]

    args += VIEW_PARAMS.get(view, VIEW_PARAMS["network"])
    args += ["-white_background", "true" if white_background else "false"]
    if zoom_level is not None:
        args += ["-zoom_level", str(zoom_level)]
    if min_line_strength is not None:
        args += ["-min_line_strength", str(min_line_strength)]
    if scale is not None:
        args += ["-scale", str(scale)]

    args += _screenshot_args(out_images)

    outputs = list(out_images)
    if out_map:
        outputs.append(out_map)
    if out_network:
        outputs.append(out_network)

    # 布局聚类比单纯截图慢，超时放宽
    old_timeout = env.timeout
    env.timeout = max(env.timeout, 300)
    try:
        return run_vosviewer(env, args, outputs)
    finally:
        env.timeout = old_timeout


def pass_term_map_from_corpus(env: VosEnv, corpus_file: str,
                              out_map: str, out_network: str,
                              min_occurrences: int = 10,
                              n_terms: int = 60,
                              counting_method: int = 2,
                              thesaurus: str | None = None) -> dict:
    """可选路线：让 VOSviewer 自己从语料（标题+摘要）做文本挖掘生成术语共现图。

    counting_method: 1=二进制计数，2=全计数
    """
    args = ["-corpus", corpus_file,
            "-min_n_occurrences", str(min_occurrences),
            "-n_terms", str(n_terms),
            "-counting_method", str(counting_method),
            "-run_layout", "-run_clustering",
            "-save_map", out_map, "-save_network", out_network]
    if thesaurus:
        args += ["-thesaurus", thesaurus]
    return run_vosviewer(env, args, [out_map, out_network])
