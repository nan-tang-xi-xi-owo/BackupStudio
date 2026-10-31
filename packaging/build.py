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


def _current_platform() -> str:
    """当前平台标识。

    单独抽一层是为了**可测**：直接 monkeypatch ``sys.platform`` 会污染整个进程
    （PyInstaller 的 ``compat`` 在 import 时就读它并缓存，之后
    ``from PyInstaller.compat import win32api`` 会 ImportError）。
    """
    return sys.platform


def _tool_dir() -> Path | None:
    if _current_platform() == "win32":
        candidate = ROOT / "tools" / "win32" / "7z"
    elif _current_platform() == "darwin":
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
    icon = ROOT / "packaging" / ("icon.ico" if _current_platform() == "win32" else "icon.icns")
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
    # N5：PyInstaller 只收集 .py 模块，**不会**带上 py.typed 这种包数据文件
    # （已实测：产物里 `_internal/backupstudio/py.typed` 不存在）。
    # PEP 561 标记缺失时，类型检查器不会把本包当作带类型注解的包，必须显式加进去。
    py_typed = ROOT / "backupstudio" / "py.typed"
    if py_typed.is_file():
        args += ["--add-data", f"{py_typed}{os.pathsep}backupstudio"]
        print(f"随包携带 py.typed：{py_typed}")

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
    if _current_platform() == "darwin":
        args += ["--osx-bundle-identifier", BUNDLE_IDENTIFIER]
    # Windows：把 PE 版本资源（文件版本/产品名/公司名/版权）写进 exe。
    # 不传 --version-file 时 PyInstaller 完全跳过 RT_VERSION，
    # 资源管理器里 BackupStudio.exe 的版本信息全空（N1）。
    if _current_platform() == "win32":
        version = _app_version()
        version_file = _write_version_file(version=version)
        if not _verify_version_file(version_file, version):
            return 1
        args += ["--version-file", str(version_file)]
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
    # N7：_verify_assets() 的返回值曾**被直接丢弃** → 唯一的资源门禁形同虚设，
    # 缺托盘图标也能"打包成功"。资源缺失必须让构建失败（CI 才能拦住坏包）。
    if not _verify_assets():
        print("✗ 构建失败：托盘图标资源未打进产物（见上）", file=sys.stderr)
        return 1
    # N8：7-Zip 是 LGPL，分发二进制必须随附许可文本。
    if not _verify_licenses():
        print("✗ 构建失败：第三方许可未随包分发（7-Zip 为 LGPL，分发时必须附许可）", file=sys.stderr)
        return 1
    # N1：回读 exe 的 PE 版本资源，确认真写进去了（而不是只看"传了参数"）。
    if _current_platform() == "win32":
        exe = DIST / APP_NAME / f"{APP_NAME}.exe"
        if not _verify_windows_exe_version(exe, _app_version()):
            print("✗ 构建失败：exe 缺少 PE 版本资源", file=sys.stderr)
            return 1
    if _current_platform() == "darwin":
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
    if _current_platform() == "win32":
        return "win32"
    if _current_platform() == "darwin":
        return "darwin"
    return "linux"


# ---------------------------------------------------------------------------
# N1：Windows PE 版本资源（VSVersionInfo）
# ---------------------------------------------------------------------------
#
# 缺陷：打包产出的 ``BackupStudio.exe`` 的「文件版本 / 产品版本 / 公司名 /
# 产品名 / 版权」**全空**。原因是 PyInstaller 只有在收到 ``--version-file``
# （或 spec 里 ``version=``）时才会把 RT_VERSION 资源写进 exe；本仓库从未传过。
# 于是安装包 ``BackupStudio-Setup-1.1.0.exe`` 有版本（Inno Setup 自己写的），
# 装进去的主程序却没有 —— 资源管理器、杀软信誉、企业软件清单、卸载列表全部受影响。
#
# 修法：构建时按 ``backupstudio.__version__`` 生成一份 VSVersionInfo 文本文件，
# 只在 Windows 上通过 ``--version-file`` 交给 PyInstaller，并在构建后回读 exe
# 的版本资源做硬校验（缺字段即构建失败）。
#
# 说明：这里**自己渲染** VSVersionInfo 文本，而不是 import
# ``PyInstaller.utils.win32.versioninfo`` —— 后者在非 Windows 上 import 就会失败
# （依赖 pefile/pywin32），而本脚本要能跨平台加载、也要能被测试直接调用。

#: 版本资源里必须存在的字段（缺一即视为构建失败）
VERSION_INFO_FIELDS = (
    "CompanyName",
    "FileDescription",
    "FileVersion",
    "LegalCopyright",
    "ProductName",
    "ProductVersion",
)


def _version_tuple(version: str) -> tuple[int, int, int, int]:
    """把 ``"1.1.0"`` 之类的版本串转成四元组（不足补 0）。"""
    parts: list[int] = []
    for token in re.findall(r"\d+", version):
        parts.append(int(token))
        if len(parts) == 4:
            break
    while len(parts) < 4:
        parts.append(0)
    return parts[0], parts[1], parts[2], parts[3]


def _project_author() -> str:
    """公司名/版权主体：优先取 ``pyproject.toml`` 的 authors，避免另起一份硬编码。"""
    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "BackupStudio contributors"
    m = re.search(r'authors\s*=\s*\[\s*\{\s*name\s*=\s*"([^"]+)"', text)
    return m.group(1).strip() if m else "BackupStudio contributors"


def _legal_copyright() -> str:
    """版权行：取 LICENSE 里的 Copyright 行，读不到则用作者 + 年份兜底。"""
    try:
        text = (ROOT / "LICENSE").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        if "copyright" in line.lower() and line.strip():
            return line.strip()
    year = time.strftime("%Y")
    return f"Copyright (c) {year} {_project_author()}"


def _render_version_info(version: str) -> str:
    """渲染 PyInstaller ``--version-file`` 可 ``eval`` 的文本。

    格式必须与 ``PyInstaller.utils.win32.versioninfo.VSVersionInfo.__str__``
    一致（PyInstaller 用 ``eval()`` 反序列化），否则会被判为
    ``Failed to deserialize VSVersionInfo``。
    """
    major, minor, patch, build = _version_tuple(version)
    author = _project_author()
    copyright_line = _legal_copyright()
    # 语言/代码页 0409 = en-US，04B0 = Unicode(1200)；与 VarFileInfo 的 Translation 对应
    strings = [
        ("CompanyName", author),
        ("FileDescription", f"{APP_NAME} 备份工坊"),
        ("FileVersion", version),
        ("InternalName", APP_NAME),
        ("LegalCopyright", copyright_line),
        ("OriginalFilename", f"{APP_NAME}.exe"),
        ("ProductName", APP_NAME),
        ("ProductVersion", version),
    ]
    body = ",\n        ".join(
        f"StringStruct({name!r}, {value!r})" for name, value in strings
    )
    return (
        "# UTF-8\n"
        "#\n"
        "# 由 packaging/build.py::_write_version_file() 自动生成，请勿手工编辑。\n"
        "# 内容等价于 PyInstaller.utils.win32.versioninfo.VSVersionInfo 的文本序列化。\n"
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers=({major}, {minor}, {patch}, {build}),\n"
        f"    prodvers=({major}, {minor}, {patch}, {build}),\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "    ),\n"
        "  kids=[\n"
        "    StringFileInfo(\n"
        "      [\n"
        "      StringTable(\n"
        "        '040904B0',\n"
        f"        [{body}])\n"
        "      ]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )


def _version_file_path() -> Path:
    """版本资源文件的位置。

    注意：**不能**放在 ``BUILD``（``build/pyinstaller``）里 —— ``--clean`` 会清空
    该目录下的所有内容，PyInstaller 随后读不到版本文件（实测：文件被删后
    ``--version-file`` 直接报 file not found）。放在 ``build/`` 根下即可。
    """
    return ROOT / "build" / "version_info.txt"


def _write_version_file(path: Path | None = None, version: str | None = None) -> Path:
    """生成 PyInstaller ``--version-file`` 所需的 VSVersionInfo 文件。

    :param path: 输出路径（默认 ``build/pyinstaller/version_info.txt``）
    :param version: 显式版本（默认取 ``_app_version()``，与 pyproject/__init__ 同源）
    :raises RuntimeError: 版本号取不到时（拒绝产出空版本资源的 exe）
    """
    resolved = version or _app_version()
    target = Path(path) if path is not None else _version_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_version_info(resolved), encoding="utf-8")
    return target


def _verify_version_file(path: Path, version: str) -> bool:
    """校验版本文件：六个必需字段齐全，且版本号与真源一致。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"✗ 读不到版本资源文件 {path}：{exc}", file=sys.stderr)
        return False
    missing = [f for f in VERSION_INFO_FIELDS if f"'{f}'" not in text]
    if missing:
        print(f"✗ 版本资源缺少字段：{', '.join(missing)} → {path}", file=sys.stderr)
        return False
    if f"'{version}'" not in text:
        print(f"✗ 版本资源里的版本号不是 {version} → {path}", file=sys.stderr)
        return False
    print(f"✓ 版本资源已生成：{path}（{version}，字段齐全）")
    return True


def _verify_windows_exe_version(exe: Path, version: str) -> bool:
    """构建后回读 exe 的 PE 版本资源（真正的门禁：产物里到底有没有）。

    依赖 PyInstaller 自带的 ``read_version_info_from_executable``
    （pefile + pywin32），只在 Windows 上可调用。
    """
    try:
        from PyInstaller.utils.win32.versioninfo import (  # noqa: PLC0415
            read_version_info_from_executable,
        )
    except Exception as exc:  # noqa: BLE001 - 读不了就不该宣称"已验证"
        print(f"✗ 无法加载版本资源读取器（{type(exc).__name__}: {exc}）", file=sys.stderr)
        return False
    try:
        info = read_version_info_from_executable(str(exe))
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 读取 {exe} 的版本资源失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    if info is None:
        print(f"✗ {exe} 没有 PE 版本资源（RT_VERSION 缺失）", file=sys.stderr)
        return False
    strings = _extract_version_strings(info)
    missing = [f for f in VERSION_INFO_FIELDS if not str(strings.get(f, "")).strip()]
    if missing:
        print(f"✗ {exe} 的版本资源缺字段：{', '.join(missing)}", file=sys.stderr)
        return False
    got = str(strings.get("FileVersion", "")).strip()
    if _version_tuple(got) != _version_tuple(version):
        print(f"✗ {exe} 的 FileVersion={got!r} 与期望 {version!r} 不一致", file=sys.stderr)
        return False
    print(
        "✓ exe 版本资源："
        f"FileVersion={got} ProductVersion={strings.get('ProductVersion')} "
        f"CompanyName={strings.get('CompanyName')} ProductName={strings.get('ProductName')}"
    )
    return True


def _extract_version_strings(info) -> dict[str, str]:  # noqa: ANN001 - PyInstaller 结构体
    """把 VSVersionInfo 里的 StringTable 拍平成 ``{字段名: 值}``。"""
    flat: dict[str, str] = {}
    for kid in getattr(info, "kids", []) or []:
        if kid.__class__.__name__ != "StringFileInfo":
            continue
        for table in getattr(kid, "kids", []) or []:
            for item in getattr(table, "kids", []) or []:
                name = getattr(item, "name", None)
                if name:
                    flat[str(name)] = str(getattr(item, "val", "") or "")
    return flat


def _verify_assets() -> bool:
    """检查打包产物里的托盘图标数据文件与 py.typed 是否存在。

    N7：本函数原先被裸调用（返回值丢弃），门禁形同虚设。
    N5：PyInstaller 只收集 ``.py``，``py.typed`` 必须显式 ``--add-data``；
    这里一并校验，缺了就让构建失败。
    """
    internal = DIST / APP_NAME / "_internal"
    candidate = internal / "backupstudio" / "assets" / "tray" / "app-32.png"
    ok = candidate.is_file()
    print(("✓ 托盘图标资源已打包：" if ok else "✗ 托盘图标资源缺失：") + str(candidate))
    typed = internal / "backupstudio" / "py.typed"
    typed_ok = typed.is_file()
    print(("✓ py.typed 已打包：" if typed_ok else "✗ py.typed 缺失：") + str(typed))
    return ok and typed_ok


#: 必须随二进制分发的第三方许可（N8：7-Zip 是 LGPL，分发时必须附许可文本）
LICENSE_FILES = ("THIRD-PARTY-NOTICES.txt", "THIRD-PARTY-7ZIP.txt")
LICENSE_SOURCES = {
    "THIRD-PARTY-NOTICES.txt": "packaging/THIRD-PARTY-NOTICES.txt",
    "THIRD-PARTY-7ZIP.txt": "packaging/THIRD-PARTY-7ZIP.txt",
}


def _copy_licenses(target: Path) -> None:
    """把 7-Zip / 第三方许可复制到产物根目录**和** ``_internal/``。

    为什么要复制到两个地方：
    * 产物根目录：用户（和打包脚本、法务清单）能直接看到；
    * ``_internal/``：``gui/settings_page.py::_licenses_text()`` 在冻结形态下
      以 ``backupstudio`` 包目录的上一级为基准找文件，也就是 ``_internal/``。
      旧实现只复制到根目录 → 设置页的「第三方许可」对话框里**看不到 7-Zip 许可全文**，
      等于分发了 LGPL 二进制却没有随附许可文本。
    """
    internal = target / "_internal"
    for name in LICENSE_FILES:
        src = ROOT / LICENSE_SOURCES[name]
        if not src.is_file():
            continue
        for dest_dir in (target, internal):
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_dir / name)


def _verify_licenses() -> bool:
    """校验许可文件确实进了产物（缺一个就是合规问题，必须让构建失败）。"""
    target = DIST / APP_NAME
    internal = target / "_internal"
    missing: list[str] = []
    for name in LICENSE_FILES:
        if not (target / name).is_file():
            missing.append(str(target / name))
        if not (internal / name).is_file():
            missing.append(str(internal / name))
    if missing:
        print("✗ 随附许可缺失：\n  - " + "\n  - ".join(missing), file=sys.stderr)
        return False
    print("✓ 随附许可已就位：" + "、".join(LICENSE_FILES) + "（产物根目录 + _internal）")
    return True


def _post_build() -> None:
    """打包后处理：复制许可与说明。"""
    target = DIST / APP_NAME
    if not target.exists():
        return
    for name in ("README.md", "LICENSE"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, target / Path(name).name)
    _copy_licenses(target)
    readme = target / "README.md"
    if readme.exists():
        (target / "使用说明.txt").write_text(
            f"BackupStudio 备份工坊 {_project_version()}\n\n"
            "1. 双击 BackupStudio.exe 启动，右下角托盘会出现图标。\n"
            "2. 点「新建任务」选择要备份的文件夹、备份位置、压缩格式（可选密码/分卷）。\n"
            "3. 「计划与保留」里设置自动备份时间，并可自动清理旧备份。\n"
            "4. 关闭窗口会最小化到托盘；想彻底退出请右键托盘图标 → 退出。\n\n"
            "命令行：BackupStudio.exe --help（info / doctor / task / run / daemon / extract）\n"
            "详细说明见同目录的 README.md。\n"
            "第三方许可见 THIRD-PARTY-NOTICES.txt 与 THIRD-PARTY-7ZIP.txt（7-Zip 为 LGPL）。\n",
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
