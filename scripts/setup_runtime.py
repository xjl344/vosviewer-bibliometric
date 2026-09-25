#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VOSviewer 运行时环境自检与安装。

检查两样东西：
  1. Java 运行时（VOSviewer 是 Java 程序，必须有 JRE 8+）
  2. VOSviewer.jar（官方跨平台版）

两者都装在共享运行时目录下（默认 `~/.vosviewer-bibliometric/runtime/`，
可用环境变量 `VOSVIEWER_RUNTIME` 覆盖），跨项目复用，不需要管理员权限。

用法：
    python setup_runtime.py --check      # 只检查，不下载
    python setup_runtime.py --install    # 缺什么装什么
    python setup_runtime.py --install --mirror   # 走国内镜像（网络受限时用）
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import zipfile

# Windows 控制台默认使用 ANSI 代码页（如 cp1252），直接 print 中文会抛
# UnicodeEncodeError 导致脚本崩溃。统一改用 UTF-8，并让无法编码的字符
# 降级为替代符而不是中断执行。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RT = os.environ.get("VOSVIEWER_RUNTIME") or os.path.join(
    os.path.expanduser("~"), ".vosviewer-bibliometric", "runtime")

# Java 下载源。GitHub 在部分网络下不可达，所以默认走 Azul CDN。
#
# 关键：**必须按平台选包**。早期版本把 default 写死成 Windows 版，
# 导致 Linux/macOS 用户装出来的是跑不了的 JRE。
_ZULU = "https://cdn.azul.com/zulu/bin/zulu21.52.15-ca-jre21.0.12"

JAVA_SOURCES = {
    "win_x64": f"{_ZULU}-win_x64.zip",
    "linux_x64": f"{_ZULU}-linux_x64.tar.gz",
    "linux_aarch64": f"{_ZULU}-linux_aarch64.tar.gz",
    "macosx_x64": f"{_ZULU}-macosx_x64.zip",
    "macosx_aarch64": f"{_ZULU}-macosx_aarch64.zip",
}

VOSVIEWER_SOURCES = {
    "default": "https://www.vosviewer.com/downloads/VOSviewer_1.6.21_jar.zip",
    "mirror": "https://www.vosviewer.com/downloads/VOSviewer_1.6.21_jar.zip",
}


def detect_java_platform() -> str:
    """按当前系统与 CPU 架构选出合适的 Java 包。"""
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")

    if sysname == "darwin":
        return "macosx_aarch64" if arm else "macosx_x64"
    if sysname == "linux":
        return "linux_aarch64" if arm else "linux_x64"
    # Windows 及其他：默认 x64
    return "win_x64"


def java_url(source: str = "default") -> str:
    """当前平台对应的 Java 下载地址。

    source="mirror" 时优先走清华 Adoptium 镜像（国内更快），
    失败则回退到 Azul CDN。
    """
    if source == "mirror":
        url = _adoptium_mirror_url()
        if url:
            return url
    return JAVA_SOURCES[detect_java_platform()]


# 平台 → (Adoptium 的 os 名, 架构名)
_ADOPTIUM_OS = {
    "win_x64": ("windows", "x64"),
    "linux_x64": ("linux", "x64"),
    "linux_aarch64": ("linux", "aarch64"),
    "macosx_x64": ("mac", "x64"),
    "macosx_aarch64": ("mac", "aarch64"),
}


def _adoptium_mirror_url() -> str | None:
    """从清华 Adoptium 镜像里找出当前平台的 JRE 包。

    镜像目录下的文件名带具体次版本号，所以需要先列目录再匹配。
    任何一步失败都返回 None，由调用方回退到 Azul CDN。
    """
    import re
    import urllib.request

    plat = detect_java_platform()
    osname, arch = _ADOPTIUM_OS.get(plat, ("windows", "x64"))
    base = f"https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/{arch}/{osname}/"

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(base, headers={"User-Agent": "vosviewer-bib/1.0"})
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    # 文件名形如 OpenJDK21U-jre_x64_windows_hotspot_21.0.12_8.zip
    pat = re.compile(r'href="(OpenJDK21U-jre_[^"]+\.(?:zip|tar\.gz))"')
    names = pat.findall(html)
    if not names:
        return None
    # 取版本号最大的一个
    names.sort(reverse=True)
    return base + names[0]


def _make_executable(root: str) -> int:
    """给解压出来的可执行文件加上执行位（Linux/macOS 必需）。

    zip 包不保留 Unix 权限位，tar.gz 在部分解压方式下也会丢失，
    所以解压后必须显式 chmod，否则 java 调用会 Permission denied。
    """
    if os.name == "nt":
        return 0
    fixed = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                mode = os.stat(p).st_mode
                # bin/ 下的都是可执行文件；.dylib/.so 只需读权限
                if os.sep + "bin" + os.sep in p or fn in ("java", "keytool"):
                    os.chmod(p, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    fixed += 1
            except OSError:
                pass
    return fixed


# --------------------------------------------------------------------------
# 检查
# --------------------------------------------------------------------------

def check() -> dict:
    """返回环境状态。"""
    from voslib.render import find_java, find_vosviewer

    java = find_java()
    jar = find_vosviewer()

    status = {
        "runtime_dir": RT,
        "java": java or "",
        "jar": jar or "",
        "java_ok": bool(java and os.path.isfile(java)),
        "jar_ok": bool(jar and os.path.isfile(jar)),
        "java_version": "",
    }

    if status["java_ok"]:
        try:
            out = subprocess.run([java, "-version"], capture_output=True,
                                 text=True, timeout=30)
            first = (out.stderr or out.stdout or "").splitlines()
            status["java_version"] = first[0] if first else ""
        except Exception as e:
            status["java_version"] = f"（调用失败：{e}）"

    return status


def print_status(s: dict) -> None:
    print("VOSviewer 运行时环境自检")
    print("-" * 58)
    print(f"运行时目录 : {s['runtime_dir']}")
    print(f"Java       : {'✓ ' + s['java'] if s['java_ok'] else '✗ 未找到'}")
    if s["java_version"]:
        print(f"              {s['java_version']}")
    print(f"VOSviewer  : {'✓ ' + s['jar'] if s['jar_ok'] else '✗ 未找到'}")
    print("-" * 58)
    if s["java_ok"] and s["jar_ok"]:
        print("环境就绪，可以开始分析。")
    else:
        print("环境不完整。运行以下命令自动安装：")
        print(f"    python {os.path.basename(__file__)} --install")


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

def _download(url: str, dest: str) -> bool:
    """下载文件。优先 curl（对证书与代理更宽容），退化到 urllib。"""
    print(f"  下载 {url}")
    print(f"    → {dest}")

    curl = shutil.which("curl")
    if curl:
        cmd = [curl, "-sSL", "-k", "--max-time", "900", "-o", dest, url]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=960)
            if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 1024:
                print(f"    完成，{os.path.getsize(dest) / 1024 / 1024:.1f} MB")
                return True
            err = (r.stderr or b"").decode("utf-8", "replace")[:200]
            print(f"    curl 失败（{r.returncode}）：{err}")
        except Exception as e:
            print(f"    curl 异常：{e}")

    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=900, context=ctx) as resp, \
                open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        if os.path.getsize(dest) > 1024:
            print(f"    完成，{os.path.getsize(dest) / 1024 / 1024:.1f} MB")
            return True
    except Exception as e:
        print(f"    urllib 也失败：{e}")
    return False


def install_java(source: str = "default") -> bool:
    print("\n[1/2] 安装 Java 运行时")
    plat = detect_java_platform()
    url = java_url(source)
    print(f"  平台：{plat}（{platform.system()} {platform.machine()}）")
    print(f"  来源：{'清华镜像' if 'tuna' in url else 'Azul CDN'}")
    tmp = tempfile.mkdtemp(prefix="vosjava_")
    # 保留真实扩展名，解压时才知道该用 zipfile 还是 tarfile
    ext = ".tar.gz" if url.endswith(".tar.gz") else ".zip"
    archive = os.path.join(tmp, "java" + ext)

    if not _download(url, archive):
        print("  Java 下载失败。可手动下载后解压到：")
        print(f"    {os.path.join(RT, 'jre')}")
        return False

    print("  解压中……")
    try:
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
    except zipfile.BadZipFile:
        import tarfile
        try:
            with tarfile.open(archive) as t:
                t.extractall(tmp)
        except Exception as e:
            print(f"  解压失败：{e}")
            return False

    # 找到含 bin/java 的那一层
    inner = None
    for root, dirs, files in os.walk(tmp):
        if "bin" in dirs and any(
            os.path.exists(os.path.join(root, "bin", n))
            for n in ("java.exe", "java")
        ):
            inner = root
            break
    if not inner:
        print("  解压后没找到 bin/java，包结构异常。")
        return False

    target = os.path.join(RT, "jre")
    if os.path.isdir(target):
        shutil.rmtree(target, ignore_errors=True)
    os.makedirs(RT, exist_ok=True)
    shutil.move(inner, target)
    shutil.rmtree(tmp, ignore_errors=True)

    # Linux/macOS 必须补执行位，否则 java 调用会 Permission denied
    n = _make_executable(target)
    if n:
        print(f"  已为 {n} 个文件补上执行权限")

    print(f"  Java 已安装到 {target}")
    return True


def install_vosviewer(source: str = "default") -> bool:
    print("\n[2/2] 安装 VOSviewer.jar")
    url = VOSVIEWER_SOURCES.get(source, VOSVIEWER_SOURCES["default"])
    tmp = tempfile.mkdtemp(prefix="vosjar_")
    archive = os.path.join(tmp, "vos.zip")

    if not _download(url, archive):
        print("  VOSviewer 下载失败。可手动下载后把 VOSviewer.jar 放到：")
        print(f"    {os.path.join(RT, 'VOSviewer.jar')}")
        return False

    print("  解压中……")
    try:
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
    except zipfile.BadZipFile as e:
        print(f"  解压失败：{e}")
        return False

    jar = None
    for root, _, files in os.walk(tmp):
        for f in files:
            if f.lower() == "vosviewer.jar":
                jar = os.path.join(root, f)
                break
        if jar:
            break

    if not jar:
        print("  包里没找到 VOSviewer.jar。")
        return False

    os.makedirs(RT, exist_ok=True)
    shutil.copy2(jar, os.path.join(RT, "VOSviewer.jar"))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"  VOSviewer.jar 已安装到 {RT}")
    return True


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="VOSviewer 运行时自检与安装")
    p.add_argument("--check", action="store_true", help="只检查环境")
    p.add_argument("--install", action="store_true", help="自动下载安装缺失组件")
    p.add_argument("--mirror", action="store_true", help="使用国内镜像")
    p.add_argument("--force", action="store_true", help="强制重新下载")
    args = p.parse_args()

    if not args.check and not args.install:
        args.check = True

    source = "mirror" if args.mirror else "default"

    if args.install:
        s = check()
        if args.force or not s["java_ok"]:
            install_java(source)
        else:
            print("\n[1/2] Java 已存在，跳过")
        if args.force or not s["jar_ok"]:
            install_vosviewer(source)
        else:
            print("\n[2/2] VOSviewer.jar 已存在，跳过")
        print()

    s = check()
    print_status(s)
    return 0 if (s["java_ok"] and s["jar_ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
