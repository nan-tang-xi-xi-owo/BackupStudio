"""Windows exe 的 PE 版本资源门禁（审查 N1）。

真实缺陷
--------
``packaging/build.py`` 从未给 PyInstaller 传 ``--version-file``，也从未构造
``VSVersionInfo``；PyInstaller 日志里只有 ``Copying 0 resources to EXE``。
后果：``dist/BackupStudio/BackupStudio.exe`` 的
「文件版本 / 产品版本 / 公司名 / 产品名 / 版权」**全空**，
而安装包 ``BackupStudio-Setup-1.1.0.exe`` 有版本（Inno Setup 自己写的）——
「安装包有版本、装进去的主程序没版本」，资源管理器、杀软信誉、
企业软件清单、卸载列表全部受影响。

本文件不去 mock PyInstaller，而是：
1. 直接调用 ``_write_version_file()`` 生成真实文件；
2. 用 PyInstaller **自己的** 反序列化函数读回来（它就是构建期用的那条路径）；
3. 断言六个字段齐全、版本号来自真源（不是另起一份硬编码）；
4. 静态断言构建流程真的会把 ``--version-file`` 传给 PyInstaller，且在 Windows 分支。

真正「exe 里到底有没有版本资源」由 CI 的 ``Get-Item ... VersionInfo`` 门禁覆盖，
那条门禁已写进 ``.github/workflows/build.yml``。
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_VERSION = "1.1.0"
BUILD_PY = ROOT / "packaging" / "build.py"


def _load_build_module():
    """按真实运行方式加载 ``packaging/build.py``（``sys.path[0]`` 是 ``packaging/``）。"""
    pkg_dir = str(ROOT / "packaging")
    added = pkg_dir not in sys.path
    if added:
        sys.path.insert(0, pkg_dir)
    try:
        spec = importlib.util.spec_from_file_location("_bs_build_version_under_test", BUILD_PY)
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


@pytest.fixture()
def version_file(build_mod, tmp_path):
    """在临时目录生成一份真实版本资源文件（不碰仓库里的 build/）。"""
    return build_mod._write_version_file(tmp_path / "version_info.txt", EXPECTED_VERSION)


# ---------------------------------------------------------------------------
# 生成函数本身
# ---------------------------------------------------------------------------

def test_version_tuple_pads_to_four(build_mod):
    assert build_mod._version_tuple("1.1.0") == (1, 1, 0, 0)
    assert build_mod._version_tuple("2.3") == (2, 3, 0, 0)
    assert build_mod._version_tuple("1.2.3.4.5") == (1, 2, 3, 4)
    assert build_mod._version_tuple("1.1.0-beta") == (1, 1, 0, 0)


def test_version_file_is_written(build_mod, version_file):
    assert Path(version_file).is_file(), f"未生成版本资源文件：{version_file}"
    text = Path(version_file).read_text(encoding="utf-8")
    assert "VSVersionInfo(" in text, "版本文件必须是 VSVersionInfo 文本序列化"
    assert "FixedFileInfo(" in text
    assert "StringFileInfo(" in text
    assert "VarFileInfo(" in text


def test_version_file_has_all_required_fields(build_mod, version_file):
    """六个必需字段一个都不能少。"""
    text = Path(version_file).read_text(encoding="utf-8")
    for field in build_mod.VERSION_INFO_FIELDS:
        assert f"'{field}'" in text, f"版本资源缺字段 {field}"


def test_required_fields_cover_the_six_that_matter(build_mod):
    """固化的字段清单必须覆盖 N1 要求的六项。"""
    required = {
        "FileVersion",
        "ProductVersion",
        "CompanyName",
        "FileDescription",
        "ProductName",
        "LegalCopyright",
    }
    assert required <= set(build_mod.VERSION_INFO_FIELDS), (
        f"VERSION_INFO_FIELDS 缺少：{required - set(build_mod.VERSION_INFO_FIELDS)}"
    )


def test_version_file_versions_match(build_mod, version_file):
    text = Path(version_file).read_text(encoding="utf-8")
    assert "filevers=(1, 1, 0, 0)" in text, f"filevers 不对：\n{text[:600]}"
    assert "prodvers=(1, 1, 0, 0)" in text, f"prodvers 不对：\n{text[:600]}"
    assert "'FileVersion', '1.1.0'" in text
    assert "'ProductVersion', '1.1.0'" in text


def test_version_file_product_name_and_company_non_empty(build_mod, version_file):
    text = Path(version_file).read_text(encoding="utf-8")
    m_prod = re.search(r"StringStruct\('ProductName',\s*'([^']*)'\)", text)
    m_comp = re.search(r"StringStruct\('CompanyName',\s*'([^']*)'\)", text)
    m_desc = re.search(r"StringStruct\('FileDescription',\s*'([^']*)'\)", text)
    m_copy = re.search(r"StringStruct\('LegalCopyright',\s*'([^']*)'\)", text)
    for name, m in (
        ("ProductName", m_prod),
        ("CompanyName", m_comp),
        ("FileDescription", m_desc),
        ("LegalCopyright", m_copy),
    ):
        assert m, f"版本资源里找不到 {name}"
        assert m.group(1).strip(), f"{name} 不能为空（这正是 N1 的缺陷表现）"


def test_version_comes_from_single_source_of_truth(build_mod, version_file):
    """版本号必须来自 ``_app_version()``（pyproject/__init__ 同源），不能另起一份硬编码。"""
    assert build_mod._app_version() == EXPECTED_VERSION
    text = Path(version_file).read_text(encoding="utf-8")
    assert f"'{EXPECTED_VERSION}'" in text
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert m and m.group(1) == EXPECTED_VERSION


def test_verify_version_file_passes_for_generated_file(build_mod, version_file):
    assert build_mod._verify_version_file(version_file, EXPECTED_VERSION) is True


def test_verify_version_file_rejects_missing_field(build_mod, tmp_path):
    """反向验证：缺字段必须被判失败（否则门禁形同虚设）。"""
    broken = tmp_path / "broken.txt"
    broken.write_text(
        "VSVersionInfo(ffi=FixedFileInfo(filevers=(1,1,0,0)), kids=[])",
        encoding="utf-8",
    )
    assert build_mod._verify_version_file(broken, EXPECTED_VERSION) is False


def test_verify_version_file_rejects_wrong_version(build_mod, version_file):
    assert build_mod._verify_version_file(version_file, "9.9.9") is False


# ---------------------------------------------------------------------------
# 能被 PyInstaller 真正反序列化（构建期走的就是这条路径）
# ---------------------------------------------------------------------------

def test_pyinstaller_can_deserialize_version_file(version_file):
    """PyInstaller 用 ``eval()`` 读版本文件，格式错了会直接构建失败。

    这是构建期走的**同一条**代码路径，所以必须真调它，不能只靠正则匹配文本。
    """
    versioninfo = pytest.importorskip("PyInstaller.utils.win32.versioninfo")
    info = versioninfo.load_version_info_from_text_file(str(version_file))
    assert isinstance(info, versioninfo.VSVersionInfo)
    mod = _load_build_module()
    strings = mod._extract_version_strings(info)
    for field in mod.VERSION_INFO_FIELDS:
        assert str(strings.get(field, "")).strip(), f"反序列化后 {field} 为空"
    assert strings["FileVersion"] == EXPECTED_VERSION
    assert strings["ProductVersion"] == EXPECTED_VERSION


def test_pyinstaller_reads_fixed_file_info_versions(version_file):
    """固定版本信息（数字部分）也必须真的写进去了。"""
    versioninfo = pytest.importorskip("PyInstaller.utils.win32.versioninfo")
    info = versioninfo.load_version_info_from_text_file(str(version_file))
    assert info.ffi.fileVersionMS == (1 << 16) | 1, "fileVersionMS 不是 1.1"
    assert info.ffi.fileVersionLS == 0, "fileVersionLS 不是 .0.0"
    assert info.ffi.productVersionMS == (1 << 16) | 1


# ---------------------------------------------------------------------------
# 构建流程确实会用它（静态契约）
# ---------------------------------------------------------------------------

def test_build_passes_version_file_to_pyinstaller():
    text = BUILD_PY.read_text(encoding="utf-8")
    assert "--version-file" in text, (
        "build.py 没给 PyInstaller 传 --version-file；"
        "PyInstaller 会完全跳过 RT_VERSION（Copying 0 resources to EXE）"
    )


def test_version_file_only_on_windows():
    """只有 Windows 才该传 —— macOS/Linux 传了会被 PyInstaller 警告/忽略。"""
    tree = ast.parse(BUILD_PY.read_text(encoding="utf-8"))
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build"),
        None,
    )
    assert func is not None, "找不到 build()"
    win_branch = next(
        (
            n for n in ast.walk(func)
            if isinstance(n, ast.If)
            and ("win32" in ast.unparse(n.test) or "_current_platform" in ast.unparse(n.test))
            and "version-file" in ast.unparse(n)
        ),
        None,
    )
    assert win_branch is not None, (
        "build() 里找不到「只在 Windows 传 --version-file」的分支"
    )
    assert "win32" in ast.unparse(win_branch.test), (
        f"这个分支没有判断 win32：{ast.unparse(win_branch.test)}"
    )


def test_platform_probe_is_patchable():
    """平台判断必须走 ``_current_platform()``，不能直接读 ``sys.platform``。

    直接 monkeypatch ``sys.platform`` 会污染整进程（PyInstaller 的 compat 在
    import 时就读它并缓存，之后 ``from PyInstaller.compat import win32api``
    会 ImportError）——测试里只能替换这一层。
    """
    mod = _load_build_module()
    assert callable(getattr(mod, "_current_platform", None)), "缺少 _current_platform()"
    assert mod._current_platform() == sys.platform
    tree = ast.parse(BUILD_PY.read_text(encoding="utf-8"))
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build"),
        None,
    )
    assert func is not None
    assert "sys.platform" not in ast.unparse(func), (
        "build() 里仍有直接读 sys.platform 的地方（测试无法安全替换）"
    )


def test_build_verifies_exe_version_after_build():
    """构建后必须回读 exe 的版本资源（只看"传了参数"不算验证）。"""
    text = BUILD_PY.read_text(encoding="utf-8")
    assert "_verify_windows_exe_version" in text, "缺少构建后的 exe 版本资源校验"
    assert "read_version_info_from_executable" in text, (
        "校验必须真的读回 exe 的 RT_VERSION 资源"
    )


def test_verify_windows_exe_version_fails_on_missing_resource(tmp_path):
    """非 PE 文件（没有版本资源）必须被判失败，而不是静默通过。"""
    mod = _load_build_module()
    fake = tmp_path / "not-an-exe.exe"
    fake.write_bytes(b"MZ" + b"\x00" * 64)
    # 读不出资源（None 或抛异常）都算失败，绝不返回 True
    assert mod._verify_windows_exe_version(fake, EXPECTED_VERSION) is False


@pytest.mark.skipif(
    not (ROOT / "dist" / "BackupStudio" / "BackupStudio.exe").is_file(),
    reason="尚未在本机构建 Windows 产物（CI 的 windows job 会构建并校验）",
)
def test_built_exe_has_version_resource():
    """若本机已有构建产物，直接断言 exe 的 PE 版本资源（最强的证据）。"""
    versioninfo = pytest.importorskip("PyInstaller.utils.win32.versioninfo")
    exe = ROOT / "dist" / "BackupStudio" / "BackupStudio.exe"
    try:
        info = versioninfo.read_version_info_from_executable(str(exe))
    except ImportError:  # pragma: no cover - 非 Windows 解释器
        pytest.skip("当前解释器无法读 PE 资源（非 Windows）")
    assert info is not None, f"{exe} 没有 PE 版本资源（N1 未修复）"
    mod = _load_build_module()
    strings = mod._extract_version_strings(info)
    assert strings.get("FileVersion") == EXPECTED_VERSION, f"FileVersion={strings.get('FileVersion')!r}"
    assert strings.get("ProductVersion") == EXPECTED_VERSION
    assert strings.get("ProductName") == "BackupStudio"
    assert strings.get("CompanyName", "").strip(), "CompanyName 为空"
    assert strings.get("LegalCopyright", "").strip(), "LegalCopyright 为空"
