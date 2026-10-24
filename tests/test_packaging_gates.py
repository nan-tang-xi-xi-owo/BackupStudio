"""打包门禁：py.typed / 资源校验 / 第三方许可（审查 N5 / N7 / N8）。

三个缺陷都属于「声明了但没生效」这一类，共同点是**没有任何东西会因此变红**：

**N5 ``py.typed`` 声明了但文件不存在**
``pyproject.toml`` 的 ``[tool.setuptools.package-data]`` 与 ``MANIFEST.in``
都列了 ``backupstudio/py.typed``，仓库里却没有这个文件 → wheel/sdist 里都没有。
PEP 561 标记缺失，类型检查器不会去读本包的类型注解。

**N7 ``_verify_assets()`` 返回值被丢弃**
``build.py`` 里 ``_verify_assets()`` 是裸调用，函数返回 bool 却没人看。
唯一的托盘资源门禁形同虚设：缺图标也照样"打包成功"。

**N8 7-Zip 许可未随附**
``packaging/THIRD-PARTY-7ZIP.txt`` 存在但从未被复制进产物；7-Zip 是 LGPL，
**分发二进制时必须随附许可文本**。冻结后 ``_internal/THIRD-PARTY-NOTICES.txt``
也不存在 → 设置页的「第三方许可」对话框读不到全文。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_PY = ROOT / "packaging" / "build.py"
PKG = ROOT / "backupstudio"


def _load_build_module():
    pkg_dir = str(ROOT / "packaging")
    added = pkg_dir not in sys.path
    if added:
        sys.path.insert(0, pkg_dir)
    try:
        spec = importlib.util.spec_from_file_location("_bs_build_gates_under_test", BUILD_PY)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(pkg_dir)
    return module


@pytest.fixture(scope="module")
def build_mod():
    return _load_build_module()


# ---------------------------------------------------------------------------
# N5：py.typed（PEP 561）
# ---------------------------------------------------------------------------

def test_py_typed_file_exists():
    """仓库里必须真的有 ``backupstudio/py.typed``。"""
    marker = PKG / "py.typed"
    assert marker.is_file(), (
        f"缺少 {marker}：pyproject.toml 与 MANIFEST.in 都声明了它，"
        "但文件不存在 → wheel/sdist 里也没有，类型检查器不会读本包注解"
    )


def test_py_typed_is_empty_or_comment_only():
    """PEP 561 的 ``py.typed`` 是标记文件：空文件或只有注释。"""
    text = (PKG / "py.typed").read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped or stripped.startswith("#"), (
            f"py.typed 只应是标记文件，不应包含内容：{line!r}"
        )


def test_py_typed_declared_and_present_consistent():
    """``package-data`` 里声明了就必须存在（两者不能脱节）。"""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = pyproject.split("[tool.setuptools.package-data]", 1)
    assert len(section) == 2, "pyproject.toml 缺少 [tool.setuptools.package-data]"
    body = section[1].split("\n[", 1)[0]
    if "py.typed" in body:
        assert (PKG / "py.typed").is_file(), (
            "package-data 声明了 py.typed，但 backupstudio/py.typed 不存在"
        )


def test_manifest_in_py_typed_consistent():
    text = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    if "py.typed" in text:
        assert (PKG / "py.typed").is_file(), "MANIFEST.in 声明了 py.typed 但文件不存在"


def test_installed_wheel_contains_py_typed(tmp_path):
    """真打一个 wheel，断言 ``backupstudio/py.typed`` 在包里（PEP 561 的落地判据）。"""
    import subprocess
    import zipfile

    out = tmp_path / "wheel"
    out.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--disable-pip-version-check", "-w", str(out), str(ROOT)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=600,
    )
    assert proc.returncode == 0, f"pip wheel 失败：\n{proc.stderr[-2000:]}"
    wheels = list(out.glob("backupstudio-*.whl"))
    assert wheels, f"未产出 wheel：{list(out.iterdir())}"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    assert "backupstudio/py.typed" in names, (
        f"wheel 里没有 backupstudio/py.typed：{[n for n in names if 'typed' in n]}"
    )


# ---------------------------------------------------------------------------
# N7：资源校验不是"裸调用"
# ---------------------------------------------------------------------------

def _build_func_ast() -> ast.FunctionDef:
    tree = ast.parse(BUILD_PY.read_text(encoding="utf-8"))
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build"),
        None,
    )
    assert func is not None, "找不到 build()"
    return func


def test_verify_assets_result_is_used():
    """``_verify_assets()`` 的返回值必须被用于决定成败。

    旧实现是裸调用 ``_verify_assets()``（表达式语句），返回值直接丢弃：
    缺托盘图标也照样"打包成功"，CI 的二次校验才是唯一兜底。
    """
    func = _build_func_ast()
    bare_calls = [
        node
        for node in func.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute | ast.Name)
        and "_verify_assets" in ast.unparse(node.value.func)
    ]
    assert not bare_calls, (
        "build() 里仍有裸调用 _verify_assets()（返回值被丢弃，门禁无效）"
    )
    # 必须出现在 if not ... 里，并且导致非零返回
    guard = next(
        (
            n for n in ast.walk(func)
            if isinstance(n, ast.If) and "_verify_assets" in ast.unparse(n.test)
        ),
        None,
    )
    assert guard is not None, "找不到 `if not _verify_assets(): ...` 形式的分支"
    assert "return 1" in ast.unparse(guard), (
        "资源缺失时 build() 必须返回非零（否则 CI 会放过坏包）"
    )


def test_verify_assets_returns_false_when_missing(build_mod, tmp_path, monkeypatch):
    """反向验证：产物里没图标时 ``_verify_assets()`` 必须返回 False。"""
    monkeypatch.setattr(build_mod, "DIST", tmp_path)
    assert build_mod._verify_assets() is False


def test_verify_assets_requires_py_typed(build_mod, tmp_path, monkeypatch):
    """有图标但没有 py.typed 时也必须判失败（N5 在产物侧的落地判据）。"""
    monkeypatch.setattr(build_mod, "DIST", tmp_path)
    tray = tmp_path / build_mod.APP_NAME / "_internal" / "backupstudio" / "assets" / "tray"
    tray.mkdir(parents=True)
    (tray / "app-32.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    marker = tmp_path / build_mod.APP_NAME / "_internal" / "backupstudio" / "py.typed"
    assert not marker.exists()
    assert build_mod._verify_assets() is False, (
        "缺 py.typed 时 _verify_assets() 仍返回 True → 产物的 PEP 561 标记无声丢失"
    )
    marker.write_text("", encoding="utf-8")
    assert build_mod._verify_assets() is True


def test_build_adds_py_typed_as_data_file():
    """``build.py`` 必须显式把 py.typed 作为数据文件加入（PyInstaller 不收 .py 之外的文件）。"""
    text = BUILD_PY.read_text(encoding="utf-8")
    assert "py.typed" in text, "build.py 没有把 py.typed 加入产物"
    assert "--add-data" in text
    # 必须真的拼进 PyInstaller 参数（形如 py_typed ... backupstudio）
    assert "backupstudio" in text.split("py.typed", 1)[1][:400]


def test_verify_assets_returns_true_when_present(build_mod, tmp_path, monkeypatch):
    """正向：图标与 py.typed 均就位时返回 True（避免门禁永远红）。"""
    monkeypatch.setattr(build_mod, "DIST", tmp_path)
    internal = tmp_path / build_mod.APP_NAME / "_internal" / "backupstudio"
    tray = internal / "assets" / "tray"
    tray.mkdir(parents=True)
    (tray / "app-32.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (internal / "py.typed").write_text("", encoding="utf-8")
    assert build_mod._verify_assets() is True


def test_build_fails_when_assets_missing(build_mod, tmp_path, monkeypatch):
    """端到端：资源缺失时 ``build()`` 必须返回非零。

    这里不真跑 PyInstaller（太慢），而是把 subprocess 换成"假装成功"，
    并让产物目录为空 —— 门禁必须自己把构建打成失败。
    """
    monkeypatch.setattr(build_mod, "DIST", tmp_path / "dist")
    monkeypatch.setattr(build_mod, "_post_build", lambda: None)
    monkeypatch.setattr(build_mod, "_verify_licenses", lambda: True)

    # 这个假对象会被**所有** subprocess.run 调用看到（build_mod.subprocess 就是
    # 全局 subprocess 模块），PyInstaller 自己的 compat 层导入时也会拿它去
    # `"musl" in proc.stderr`（既有 bytes 模式也有 text 模式）→ stderr 用一个
    # 「怎么 in 都返回 False」的占位对象，避免测试假阳性变成无关的 TypeError。
    class _NeverContains:
        def __contains__(self, item):  # noqa: ANN001, D105
            return False

        def __str__(self) -> str:  # noqa: D105
            return ""

        def __bool__(self) -> bool:  # noqa: D105
            return False

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = _NeverContains()

    monkeypatch.setattr(build_mod.subprocess, "run", lambda *a, **k: _Proc())
    # 跳过 Windows 专属版本校验（本用例只关心资源门禁）
    monkeypatch.setattr(build_mod, "_verify_windows_exe_version", lambda *a, **k: True)
    # 用「平台函数」而不是 monkeypatch sys.platform：后者是全局模块属性，
    # PyInstaller 的 compat 在 import 时会读它并缓存，污染同进程其它用例（已踩过）。
    monkeypatch.setattr(build_mod, "_current_platform", lambda: "linux")
    assert build_mod.build(include_tools=False, clean=False) == 1, (
        "托盘资源缺失时 build() 没有返回非零 —— N7 门禁仍然无效"
    )

# ---------------------------------------------------------------------------
# N8：7-Zip 许可必须随包分发
# ---------------------------------------------------------------------------

def test_third_party_7zip_notice_exists():
    notice = ROOT / "packaging" / "THIRD-PARTY-7ZIP.txt"
    assert notice.is_file(), f"缺少 {notice}"
    text = notice.read_text(encoding="utf-8", errors="replace")
    assert "Igor Pavlov" in text, "许可文本里没有版权人"
    assert "LGPL" in text, "许可文本里没有 LGPL 声明"


def test_license_files_list_covers_7zip(build_mod):
    assert "THIRD-PARTY-7ZIP.txt" in build_mod.LICENSE_FILES, (
        "LICENSE_FILES 未包含 7-Zip 许可 → 分发时不会随附（LGPL 合规问题）"
    )
    assert "THIRD-PARTY-NOTICES.txt" in build_mod.LICENSE_FILES


def test_copy_licenses_puts_files_in_both_locations(build_mod, tmp_path, monkeypatch):
    """许可必须复制到**产物根目录**和 ``_internal/`` 两处。

    * 根目录：用户与打包脚本能直接看到；
    * ``_internal/``：冻结后设置页的「第三方许可」以包目录上一级为基准找文件，
      只放根目录会导致对话框里看不到 7-Zip 许可全文。
    """
    fake_dist = tmp_path / "dist"
    target = fake_dist / build_mod.APP_NAME
    (target / "_internal").mkdir(parents=True)
    monkeypatch.setattr(build_mod, "DIST", fake_dist)

    build_mod._copy_licenses(target)

    for name in build_mod.LICENSE_FILES:
        assert (target / name).is_file(), f"产物根目录缺少 {name}"
        assert (target / "_internal" / name).is_file(), f"_internal 缺少 {name}"


def test_verify_licenses_fails_when_missing(build_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(build_mod, "DIST", tmp_path)
    (tmp_path / build_mod.APP_NAME / "_internal").mkdir(parents=True)
    assert build_mod._verify_licenses() is False


def test_verify_licenses_passes_when_present(build_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(build_mod, "DIST", tmp_path)
    target = tmp_path / build_mod.APP_NAME
    (target / "_internal").mkdir(parents=True)
    for name in build_mod.LICENSE_FILES:
        (target / name).write_text("x", encoding="utf-8")
        (target / "_internal" / name).write_text("x", encoding="utf-8")
    assert build_mod._verify_licenses() is True


def test_post_build_copies_licenses_into_internal(build_mod, tmp_path, monkeypatch):
    """``_post_build()`` 必须真的把许可放进 ``_internal/``（旧实现只放根目录）。"""
    fake_dist = tmp_path / "dist"
    target = fake_dist / build_mod.APP_NAME
    target.mkdir(parents=True)
    monkeypatch.setattr(build_mod, "DIST", fake_dist)
    build_mod._post_build()
    for name in build_mod.LICENSE_FILES:
        assert (target / "_internal" / name).is_file(), (
            f"_post_build() 没有把 {name} 复制进 _internal/"
        )


def test_licenses_text_reads_7zip_notice_in_source_tree():
    """源码运行时设置页必须能读到 7-Zip 许可全文。"""
    from backupstudio.gui.settings_page import _licenses_text

    text = _licenses_text()
    assert "THIRD-PARTY-7ZIP.txt" in text, "设置页没有包含 7-Zip 许可全文"
    assert "Igor Pavlov" in text, text[:500]


def test_licenses_text_works_when_frozen_layout(tmp_path, monkeypatch):
    """模拟冻结布局（``_internal/`` 里有许可、仓库根没有）也必须读得到。"""
    import backupstudio.gui.settings_page as sp

    fake_internal = tmp_path / "_internal"
    fake_internal.mkdir()
    (fake_internal / "THIRD-PARTY-7ZIP.txt").write_text(
        "7-Zip License for use and distribution\nCopyright (C) Igor Pavlov.\n",
        encoding="utf-8",
    )
    (fake_internal / "THIRD-PARTY-NOTICES.txt").write_text(
        "BackupStudio 第三方组件与许可\n", encoding="utf-8"
    )
    monkeypatch.setattr(sp, "_license_search_dirs", lambda: [fake_internal])
    text = sp._licenses_text()
    assert "Igor Pavlov" in text, f"冻结布局下读不到 7-Zip 许可：\n{text[:400]}"


def test_build_has_license_gate():
    """构建流程必须有许可门禁（缺许可 → 构建失败）。"""
    text = BUILD_PY.read_text(encoding="utf-8")
    assert "_verify_licenses" in text, "build.py 没有许可校验"
    func = _build_func_ast()
    guard = next(
        (
            n for n in ast.walk(func)
            if isinstance(n, ast.If) and "_verify_licenses" in ast.unparse(n.test)
        ),
        None,
    )
    assert guard is not None, "build() 没有 `if not _verify_licenses(): ...` 分支"
    assert "return 1" in ast.unparse(guard), "许可缺失时 build() 必须返回非零"


def test_gui_license_dialog_mentions_7zip():
    text = (ROOT / "backupstudio" / "gui" / "settings_page.py").read_text(encoding="utf-8")
    assert "THIRD-PARTY-7ZIP.txt" in text, "设置页没有引用 7-Zip 许可文件"


# ---------------------------------------------------------------------------
# CI 门禁
# ---------------------------------------------------------------------------

def test_ci_checks_exe_version_and_licenses():
    """CI 必须校验 exe 版本资源与随附许可（防止复发）。"""
    workflow = ROOT / ".github" / "workflows" / "build.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "VersionInfo" in text or "FileVersion" in text, (
        "CI 未校验 exe 的 PE 版本资源（N1 会复发）"
    )
    assert "THIRD-PARTY-7ZIP" in text, "CI 未校验 7-Zip 许可随包（N8 会复发）"
