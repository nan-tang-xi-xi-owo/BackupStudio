"""PyInstaller 打包脚本（跨平台）。

用法::

    python packaging/build.py                # 当前平台：Windows → exe，macOS → .app(.dmg)
    python packaging/build.py --zip          # 额外打一个便携 zip
    python packaging/build.py --no-tools     # 不随包携带 7za（Windows）

输出位于 ``dist/``：
* Windows  ``dist/BackupStudio/BackupStudio.exe``（onedir，带托盘与内嵌 7za）
* macOS    ``dist/BackupStudio.app``（由 packaging/make-dmg.sh 生成 .dmg）
* Linux    ``dist/BackupStudio/``（由 packaging/make-appimage.sh 生成 AppImage）

注意：PyInstaller 不能交叉编译。每个平台必须在对应系统（或对应容器）里各跑一次。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from _console import make_console_utf8_safe

# B1：**在模块导入后立刻**切 UTF-8（不能等到 build() 里）——
# argparse 的 --help / 报错信息本身含中文，`python packaging/build.py --help`
# 在 cp1252 下会先在 argparse 阶段崩掉，根本走不到 build()。
make_console_utf8_safe()

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "pyinstaller"
APP_NAME = "BackupStudio"

# Windows 控制台默认 GBK：脚本里的 ✓/✗ 等符号会直接抛 UnicodeEncodeError
# 把构建打成"失败"（CI 实测）。统一改为 UTF-8 + 容错替换。
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def _project_version() -> str:
    """读取版本号（不 import 包：本脚本运行时 sys.path 不含项目根）。"""
    import re

    try:
        text = (ROOT / "backupstudio" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "unknown"


def _tool_dir() -> Path | None:
    if sys.platform == "win32":
        candidate = ROOT / "tools" / "win32" / "7z"
    elif sys.platform == "darwin":
        candidate = ROOT / "tools" / "darwin" / "7z"
    else:
        candidate = ROOT / "tools" / "linux" / "7z"
    return candidate if candidate.is_dir() else None


def _hidden_imports() -> list[str]:
    mods = [
        "py7zr", "py7zr.compressor", "py7zr.archiveinfo", "py7zr.py7zr",
        "py7zr.callbacks", "py7zr.io", "py7zr.properties", "py7zr.helpers",
        "rarfile", "zmq", "zmq.backend.cython", "psutil",
        "zstandard",
        "PySide6.QtSvg", "PySide6.QtNetwork",
        # py7zr 加密支持（动态导入，PyInstaller 看不到）
        "Cryptodome", "Cryptodome.Cipher", "Cryptodome.Cipher.AES",
        "Cryptodome.Cipher._raw_aes", "Cryptodome.Cipher._raw_aesni",
        "Cryptodome.Cipher._raw_cbc", "Cryptodome.Cipher._raw_ecb",
        "Cryptodome.Util", "Cryptodome.Util.strxor",
    ]
    try:
        import pyzstd  # noqa: F401

        mods.append("pyzstd")
    except ImportError:
        pass
    return mods


def _excludes() -> list[str]:
    return [
        "tkinter", "customtkinter", "matplotlib", "numpy", "pandas", "IPython",
        "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
        "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtTest", "PySide6.QtUiTools",
        "PyInstaller", "pytest",
    ]


def build(include_tools: bool = True, clean: bool = True, console: bool = False) -> int:
    # B1：幂等再切一次（覆盖被调用方/子进程提前改写流的场景）；
    # 真正的兜底在模块导入处，见文件头。
    make_console_utf8_safe()
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("请先安装 PyInstaller：pip install pyinstaller", file=sys.stderr)
        return 2

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", APP_NAME,
        "--windowed" if not console else "--console",
        "--onedir",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
    ]
    if clean:
        args.append("--clean")
    icon = ROOT / "packaging" / ("icon.ico" if sys.platform == "win32" else "icon.icns")
    if icon.exists():
        args += ["--icon", str(icon)]
    for mod in _hidden_imports():
        args += ["--hidden-import", mod]
    for mod in _excludes():
        args += ["--exclude-module", mod]
    # 应用资源（托盘图标等）：必须以数据文件形式加入，否则会被塞进 base_library.zip 而读不到
    assets = ROOT / "backupstudio" / "assets"
    if assets.is_dir():
        args += ["--add-data", f"{assets}{os.pathsep}backupstudio/assets"]
        print(f"随包携带图标资源：{assets}")

    if include_tools:
        tools = _tool_dir()
        if tools:
            args += ["--add-data", f"{tools}{os.pathsep}tools/{platform_key()}"]
            print(f"随包携带 7-Zip：{tools}")
        else:
            print("警告：未找到 tools 目录，将依赖用户系统的 7-Zip")

    entry = ROOT / "run_backupstudio.py"
    # macOS：让 PyInstaller 把正确的反向域名标识写进 Info.plist。
    # 不传这个参数时 PyInstaller 会 fallback 到 appname（裸 "BackupStudio"），
    # 与 Flatpak / LaunchAgent / metainfo 的 io.github.backupstudio.BackupStudio 不一致。
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", BUNDLE_IDENTIFIER]
    # 收集整个 Cryptodome 包（里面全是按需加载的原生扩展）
    try:
        import Cryptodome  # noqa: F401

        args += ["--collect-all", "Cryptodome"]
    except ImportError:
        print("警告：未安装 pycryptodomex，7z 加密功能在打包版中可能不可用")

    args.append(str(entry))

    print("运行：", " ".join(args))
    started = time.time()
    proc = subprocess.run(args, cwd=str(ROOT))
    if proc.returncode != 0:
        return proc.returncode
    print(f"打包完成，用时 {time.time() - started:.0f}s → {DIST}")
    _post_build()
    _verify_assets()
    if sys.platform == "darwin":
        # 必须在 PyInstaller 之后、make-dmg.sh 之前把 Info.plist 修正，
        # 否则 dmg 里的 .app 版本号恒为 0.0.0（文件名却是 1.1.0）。
        if not _fix_macos_plist():
            return 1
    return 0


def _macos_plist_path() -> Path:
    return DIST / f"{APP_NAME}.app" / "Contents" / "Info.plist"


def _fix_macos_plist() -> bool:
    """把 ``CFBundleShortVersionString`` / ``CFBundleIdentifier`` 改成本项目的真值。

    R-fix4 根因说明：PyInstaller 的 BUNDLE 模板（``PyInstaller/building/templates.py``）
    只把 ``bundle_identifier`` 传进 spec，**从不传 ``version=``**，于是
    ``BUNDLE.__init__`` 里的 ``kwargs.get("version", "0.0.0")`` 永远取默认值，
    Info.plist 的 ``CFBundleShortVersionString`` 恒为 ``0.0.0``。
    本仓库旧代码里的 ``_app_version()`` 从未被调用（死代码），因此「import 失败 →
    0.0.0」并不是唯一原因，真正原因是这条值根本没被喂给 PyInstaller。
    这里在打包完成后直接改写 plist，是跨 PyInstaller 版本都稳定的做法。
    """
    import plistlib  # noqa: PLC0415 - 仅 macOS 分支需要

    plist = _macos_plist_path()
    if not plist.is_file():
        print(f"✗ 未找到 Info.plist：{plist}", file=sys.stderr)
        return False
    version = _app_version()
    try:
        with open(plist, "rb") as fh:
            data = plistlib.load(fh)
        data["CFBundleShortVersionString"] = version
        data["CFBundleVersion"] = version
        data["CFBundleIdentifier"] = BUNDLE_IDENTIFIER
        with open(plist, "wb") as fh:
            plistlib.dump(data, fh)
    except Exception as exc:  # noqa: BLE001 - 任何失败都必须让构建失败，不能静默
        print(f"✗ 改写 Info.plist 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    print(f"✓ Info.plist 已修正：CFBundleShortVersionString={version} "
          f"CFBundleIdentifier={BUNDLE_IDENTIFIER}")
    return True


def _verify_macos_plist() -> bool:
    """校验 Info.plist 的版本号与包标识，供 CI 门禁复用。"""
    import plistlib  # noqa: PLC0415

    plist = _macos_plist_path()
    try:
        with open(plist, "rb") as fh:
            data = plistlib.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 读取 Info.plist 失败：{exc}", file=sys.stderr)
        return False
    version = str(data.get("CFBundleShortVersionString", ""))
    ident = str(data.get("CFBundleIdentifier", ""))
    expected = _app_version()
    ok = version == expected and version != "0.0.0" and ident == BUNDLE_IDENTIFIER
    print(f"{'✓' if ok else '✗'} Info.plist: version={version} (期望 {expected}), "
          f"identifier={ident} (期望 {BUNDLE_IDENTIFIER})")
    return ok


def _app_version() -> str:
    """读取包版本，供 ``Info.plist`` 的 ``CFBundleShortVersionString`` 使用。

    R-fix4 订正：旧实现 ``import backupstudio`` 失败时静默 ``return "0.0.0"``，
    而本脚本运行时 ``sys.path[0]`` 是 ``packaging/``、不含仓库根，import **必然**
    失败（已实测 ``ModuleNotFoundError``）→ 兜底值成了实际值，macOS 包内版本恒为
    0.0.0，与 dmg 文件名（1.1.0，由 make-dmg.sh 独立 import 得来）自相矛盾。

    现在改为三级取法，且**不再有静默兜底**：
      1. ``import backupstudio``（装了包 / PYTHONPATH 含仓库根时）；
      2. 正则读 ``pyproject.toml``（仓库根必存在，打包时一定可读）；
      3. 正则读 ``backupstudio/__init__.py``（与 ``_project_version()`` 同源）；
    三者全失败 → 显式 raise：打包脚本不该产出「版本号是 0.0.0」的坏包。
    """
    try:
        import backupstudio  # noqa: PLC0415 - 延迟导入：导入失败正是要兜底的场景

        version = str(getattr(backupstudio, "__version__", "")).strip()
        if version:
            return version
    except Exception:  # noqa: BLE001 - 见 docstring：失败要走下一级兜底
        pass

    for path, pattern in (
        (ROOT / "pyproject.toml", r'^\s*version\s*=\s*"([^"]+)"'),
        (ROOT / "backupstudio" / "__init__.py", r'__version__\s*=\s*"([^"]+)"'),
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(pattern, text, re.MULTILINE)
        if m and m.group(1).strip():
            return m.group(1).strip()

    raise RuntimeError(
        "无法确定应用版本号：import backupstudio 失败，且 pyproject.toml 与 "
        "backupstudio/__init__.py 均读不到 version。拒绝产出 0.0.0 的坏包。"
    )


# macOS 包标识：必须与 Flatpak / LaunchAgent / metainfo 的 app-id 完全一致，
# 否则 Gatekeeper 公证、LaunchAgent 归属与「应用身份」都对不上。
BUNDLE_IDENTIFIER = "io.github.backupstudio.BackupStudio"


def platform_key() -> str:
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _verify_assets() -> bool:
    """检查打包产物里的托盘图标数据文件是否存在。"""
    internal = DIST / APP_NAME / "_internal"
    candidate = internal / "backupstudio" / "assets" / "tray" / "app-32.png"
    ok = candidate.is_file()
    print(("✓ 托盘图标资源已打包：" if ok else "✗ 托盘图标资源缺失：") + str(candidate))
    return ok


def _post_build() -> None:
    """打包后处理：复制许可与说明。"""
    target = DIST / APP_NAME
    if not target.exists():
        return
    for name in ("packaging/THIRD-PARTY-NOTICES.txt", "README.md", "LICENSE"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, target / Path(name).name)
    readme = target / "README.md"
    if readme.exists():
        (target / "使用说明.txt").write_text(
            f"BackupStudio 备份工坊 {_project_version()}\n\n"
            "1. 双击 BackupStudio.exe 启动，右下角托盘会出现图标。\n"
            "2. 点「新建任务」选择要备份的文件夹、备份位置、压缩格式（可选密码/分卷）。\n"
            "3. 「计划与保留」里设置自动备份时间，并可自动清理旧备份。\n"
            "4. 关闭窗口会最小化到托盘；想彻底退出请右键托盘图标 → 退出。\n\n"
            "命令行：BackupStudio.exe --help（info / doctor / task / run / daemon / extract）\n"
            "详细说明见同目录的 README.md。\n",
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="BackupStudio 打包")
    parser.add_argument("--no-tools", action="store_true", help="不随包携带 7-Zip")
    parser.add_argument("--no-clean", action="store_true", help="保留 PyInstaller 缓存")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（调试用）")
    args = parser.parse_args()
    return build(include_tools=not args.no_tools, clean=not args.no_clean, console=args.console)


if __name__ == "__main__":
    sys.exit(main())
