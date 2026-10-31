"""打包资源与版本号门禁（R-fix4）。

覆盖两个已实测的真实缺陷：

1. **P0-1 Flatpak 托盘图标资源全缺失**
   ``pyproject.toml`` 的 ``[tool.setuptools.package-data]`` 原本只有 ``py.typed``，
   仓库也没有 ``MANIFEST.in``。Flatpak manifest 用
   ``pip3 install --prefix=/app --no-build-isolation .`` 安装，**非代码数据不入包**
   → ``site-packages/backupstudio/assets/tray/*.png`` 全缺，Flatpak 版托盘图标没了。
   （PyInstaller 路线靠 ``--add-data``，所以另外三平台正常，掩盖了这个问题。）

2. **P1-2 macOS ``CFBundleShortVersionString = 0.0.0``**
   ``packaging/build.py::_app_version()`` 在 ``import backupstudio`` 失败时静默返回
   ``"0.0.0"``；而该脚本运行时 ``sys.path[0]`` 是 ``packaging/``，import **必然**失败
   → 兜底值成了实际值。dmg 文件名（1.1.1）由 ``make-dmg.sh`` 独立 import 得来，
   于是「文件名对、包内版本错」，两个 dmg 都中招。

这里不 mock 打包流程，而是**真的检查产物/真源**，避免断言弱化。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS_TRAY = ROOT / "backupstudio" / "assets" / "tray"
# 从唯一真源读取，不再硬编码 —— 每次发版都要改测试是设计缺陷。
def _src_version() -> str:
    import re
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[1]
    text = (root / "backupstudio" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    assert m, "backupstudio/__init__.py 里找不到 __version__"
    return m.group(1)


EXPECTED_VERSION = _src_version()
BUNDLE_IDENTIFIER = "io.github.backupstudio.BackupStudio"


def _load_build_module():
    """按真实运行方式加载 ``packaging/build.py``。

    ``build.py`` 顶部有 ``from _console import make_console_utf8_safe``，
    所以加载时 ``packaging/`` 必须在 ``sys.path`` 上（真实运行时 ``sys.path[0]``
    就是 ``packaging/``）。
    """
    path = ROOT / "packaging" / "build.py"
    pkg_dir = str(ROOT / "packaging")
    added = pkg_dir not in sys.path
    if added:
        sys.path.insert(0, pkg_dir)
    try:
        spec = importlib.util.spec_from_file_location("_bs_build_under_test", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(pkg_dir)
    return module


def _tray_pngs() -> list[Path]:
    return sorted(ASSETS_TRAY.glob("*.png"))


# ---------------------------------------------------------------------------
# 资源本体
# ---------------------------------------------------------------------------

def test_tray_assets_exist_in_repo():
    """仓库里必须有 7 个托盘 PNG（16/24/32/48/64/128/256）。"""
    pngs = _tray_pngs()
    assert len(pngs) >= 7, f"仓库内托盘 PNG 不足：{len(pngs)}/7 -> {ASSETS_TRAY}"
    sizes = {int(re.search(r"app-(\d+)\.png$", p.name).group(1)) for p in pngs}
    assert {16, 24, 32, 48, 64, 128, 256} <= sizes, f"缺少某些尺寸：{sorted(sizes)}"
    for p in pngs:
        assert p.stat().st_size > 0, f"空文件：{p}"


# ---------------------------------------------------------------------------
# P0-1：package-data / MANIFEST.in 必须把资源带进安装产物
# ---------------------------------------------------------------------------

def test_pyproject_declares_asset_package_data():
    """``[tool.setuptools.package-data]`` 必须显式声明 assets 下的图片。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.setuptools.package-data]", 1)
    assert len(section) == 2, "pyproject.toml 缺少 [tool.setuptools.package-data]"
    body = section[1].split("\n[", 1)[0]
    assert "assets/tray/*.png" in body, (
        "package-data 未声明 assets/tray/*.png —— Flatpak 的 pip install 会丢图标。\n"
        f"当前内容：{body.strip()}"
    )
    assert "py.typed" in body, "package-data 丢了 py.typed"


def test_manifest_in_includes_assets():
    """``MANIFEST.in`` 必须存在且包含 assets 图片（管 sdist）。"""
    manifest = ROOT / "MANIFEST.in"
    assert manifest.is_file(), "缺少 MANIFEST.in：sdist 会丢运行期资源"
    text = manifest.read_text(encoding="utf-8")
    assert "recursive-include backupstudio/assets" in text, (
        "MANIFEST.in 未 recursive-include backupstudio/assets"
    )
    for ext in ("*.png", "*.ico", "*.icns", "*.svg"):
        assert ext in text, f"MANIFEST.in 未包含 {ext}"


def test_wheel_contains_tray_assets(tmp_path):
    """真打一个 wheel，断言 7 个托盘 PNG 都在包里。

    这是 Flatpak ``pip3 install --prefix=/app`` 的等价路径（wheel 是 pip 的中间产物）。
    """
    out = tmp_path / "wheel"
    out.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--disable-pip-version-check", "-w", str(out), str(ROOT)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=600,
    )
    assert proc.returncode == 0, f"pip wheel 失败：\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    wheels = list(out.glob("backupstudio-*.whl"))
    assert wheels, f"未产出 wheel：{list(out.iterdir())}"

    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
        modules = [n for n in names if "assets" in n and n.endswith(".png")]
        assert len(modules) >= 7, (
            f"wheel 内托盘 PNG 不足：{len(modules)}/7\n"
            f"assets 相关条目：{[n for n in names if 'assets' in n]}"
        )
        # 目录层级必须是 backupstudio/assets/tray，否则 gui/__init__.py::ASSETS 找不到
        for name in _tray_pngs():
            want = f"backupstudio/assets/tray/{name.name}"
            assert want in names, f"wheel 缺少 {want}"
        # RECORD 必须登记这些资源，否则部分安装器会漏装
        record = [n for n in names if n.endswith("RECORD")]
        assert record, "wheel 缺 RECORD"
        record_text = zf.read(record[0]).decode("utf-8")
        assert "assets/tray/app-32.png" in record_text, "RECORD 未登记托盘资源"


def test_installed_package_has_tray_assets(tmp_path):
    """模拟 Flatpak：``pip install --target`` 后资源必须在 site-packages 内。"""
    target = tmp_path / "pkgtest"
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation",
         "--disable-pip-version-check", "--target", str(target), str(ROOT)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=600,
    )
    assert proc.returncode == 0, f"pip install --target 失败：\n{proc.stderr[-3000:]}"
    installed_tray = target / "backupstudio" / "assets" / "tray"
    assert installed_tray.is_dir(), (
        f"安装产物里没有 assets/tray —— 这正是 Flatpak 图标缺失的根因：{installed_tray}"
    )
    got = sorted(p.name for p in installed_tray.glob("*.png"))
    want = sorted(p.name for p in _tray_pngs())
    assert got == want, f"安装产物托盘 PNG 不一致：got={got} want={want}"


def test_sdist_contains_tray_assets(tmp_path):
    """sdist 也必须带资源（MANIFEST.in 的作用域）。"""
    from setuptools import build_meta

    out = tmp_path / "sdist"
    out.mkdir()
    cwd = Path.cwd()
    try:
        import os

        os.chdir(ROOT)
        name = build_meta.build_sdist(str(out))
    finally:
        os.chdir(cwd)
    archive = out / name
    assert archive.is_file(), f"未产出 sdist：{list(out.iterdir())}"
    with tarfile.open(archive) as tf:
        members = [m.name for m in tf.getmembers() if m.name.endswith(".png")]
    tray = [m for m in members if "assets/tray" in m]
    assert len(tray) >= 7, f"sdist 内托盘 PNG 不足：{len(tray)}/7 -> {tray}"


# ---------------------------------------------------------------------------
# P1-2：版本号不得静默退化为 0.0.0
# ---------------------------------------------------------------------------

def test_app_version_from_import():
    """正常路径（能 import）返回真实版本。"""
    mod = _load_build_module()
    assert mod._app_version() == EXPECTED_VERSION


def test_app_version_when_import_fails(monkeypatch):
    """**关键回归**：``import backupstudio`` 失败时必须回退到 pyproject.toml。

    旧实现这里返回 ``"0.0.0"``，导致 macOS Info.plist 版本号恒为 0.0.0。
    """
    mod = _load_build_module()
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "backupstudio" or name.startswith("backupstudio."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert mod._app_version() == EXPECTED_VERSION, (
        "_app_version() 在 import 失败时没回退到 pyproject.toml（旧 bug：返回 0.0.0）"
    )


def test_app_version_never_returns_zero():
    """任何路径都不得返回 0.0.0 —— 打包脚本不该产出坏包。"""
    mod = _load_build_module()
    version = mod._app_version()
    assert version != "0.0.0", "版本号退化为 0.0.0（macOS Info.plist 会写错）"
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"版本号格式异常：{version!r}"


def test_app_version_matches_pyproject():
    """``_app_version()`` 与 pyproject.toml / __init__.py 三者一致。"""
    mod = _load_build_module()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert m, "pyproject.toml 里找不到 version"
    assert mod._app_version() == m.group(1)

    init = (ROOT / "backupstudio" / "__init__.py").read_text(encoding="utf-8")
    m2 = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    assert m2, "backupstudio/__init__.py 里找不到 __version__"
    assert mod._app_version() == m2.group(1), (
        f"版本号真源不一致：_app_version()={mod._app_version()} "
        f"__version__={m2.group(1)}"
    )


def test_app_version_raises_when_all_sources_fail(monkeypatch, tmp_path):
    """三者全读不到时必须显式 raise，**不能**静默返回 0.0.0。"""
    mod = _load_build_module()
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "backupstudio" or name.startswith("backupstudio."):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    # 把两个真源都指到不存在的路径
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="版本号"):
        mod._app_version()


def test_bundle_identifier_is_reverse_dns():
    """macOS 包标识必须是反向域名，不能是裸名 ``BackupStudio``。"""
    mod = _load_build_module()
    assert mod.BUNDLE_IDENTIFIER == BUNDLE_IDENTIFIER
    assert "." in mod.BUNDLE_IDENTIFIER, "CFBundleIdentifier 不能是裸名"


def test_build_py_passes_bundle_identifier_to_pyinstaller():
    """``build.py`` 必须在 macOS 分支传 ``--osx-bundle-identifier``。"""
    text = (ROOT / "packaging" / "build.py").read_text(encoding="utf-8")
    assert "--osx-bundle-identifier" in text, (
        "build.py 没给 PyInstaller 传 --osx-bundle-identifier；"
        "PyInstaller 会 fallback 到裸名 appname"
    )
    assert "BUNDLE_IDENTIFIER" in text


def test_macos_plist_fix_functions_exist():
    """必须存在 Info.plist 的修正与校验入口（PyInstaller 模板不传 version=）。"""
    mod = _load_build_module()
    assert callable(getattr(mod, "_fix_macos_plist", None)), "缺少 _fix_macos_plist()"
    assert callable(getattr(mod, "_verify_macos_plist", None)), "缺少 _verify_macos_plist()"
    text = (ROOT / "packaging" / "build.py").read_text(encoding="utf-8")
    assert "CFBundleShortVersionString" in text, "build.py 未设置 CFBundleShortVersionString"

    # 确保 _app_version() 的**代码**里不再有静默 0.0.0 兜底。
    # 只看函数体（去掉 docstring），避免把说明性文字误判成代码。
    import ast

    tree = ast.parse(text)
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_app_version"),
        None,
    )
    assert func is not None, "找不到 _app_version()"
    body_nodes = [n for n in func.body if not isinstance(n, ast.Expr)
                  or not isinstance(getattr(n, "value", None), ast.Constant)
                  or not isinstance(n.value.value, str)]
    body_code = "\n".join(ast.unparse(n) for n in body_nodes)
    assert 'return "0.0.0"' not in body_code, (
        f"_app_version() 仍有静默 0.0.0 兜底：\n{body_code}"
    )
    assert "RuntimeError" in body_code, "_app_version() 全路径失败时应显式 raise"


def test_macos_plist_rewrite_roundtrip(tmp_path):
    """用假 .app 验证 _fix_macos_plist 真的把 0.0.0 改成 1.1.1。"""
    plistlib = pytest.importorskip("plistlib")
    mod = _load_build_module()
    app = tmp_path / "BackupStudio.app" / "Contents"
    app.mkdir(parents=True)
    plist = app / "Info.plist"
    with open(plist, "wb") as fh:
        plistlib.dump(
            {
                "CFBundleShortVersionString": "0.0.0",
                "CFBundleIdentifier": "BackupStudio",
                "CFBundleName": "BackupStudio",
            },
            fh,
        )
    # 让模块的 DIST 指向临时目录
    import types

    fake_dist = tmp_path
    original = mod.DIST
    mod.DIST = fake_dist
    try:
        assert mod._fix_macos_plist() is True
        with open(plist, "rb") as fh:
            data = plistlib.load(fh)
        assert data["CFBundleShortVersionString"] == EXPECTED_VERSION
        assert data["CFBundleIdentifier"] == BUNDLE_IDENTIFIER
        assert mod._verify_macos_plist() is True
    finally:
        mod.DIST = original
