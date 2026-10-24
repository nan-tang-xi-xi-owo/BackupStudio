"""Flatpak 打包契约（审查 N2 / N3 / N4）。

本文件盯的是 manifest 与「沙箱能力边界」两件事，全部是对**真源**的硬断言：

**N2：配置目录被强制指向不可写路径**
``finish-args`` 里曾有 ``--env=BACKUPSTUDIO_CONFIG_DIR=/var/config/backupstudio``。
Flatpak 沙箱内 ``/var/config`` 不可写，``paths.app_data_dir()`` 于是静默回退
（``XDG_RUNTIME_DIR`` → ``~/.backupstudio`` → ``tempfile.gettempdir()``），
而 Flatpak 的 ``/tmp`` 是**私有 tmpfs**，重启即清空
→ 用户每次重启都丢光备份任务、设置与调度记录，``selftest`` 却仍报
``[OK] 配置目录可写``（它测的正是回退后的那个目录）。
正确做法是删掉这行，让程序用 Flatpak 默认的
``~/.var/app/<app-id>/.config``（天然可写且持久）。

**N3：自启动写进沙箱内 .config/autostart，宿主不可见**
**N4：没有文件门户授权 + 守护进程独立沙箱**
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "packaging" / "io.github.backupstudio.BackupStudio.yml"
APP_ID = "io.github.backupstudio.BackupStudio"
PORTAL_BUS = "org.freedesktop.portal.Desktop"


def _manifest_text() -> str:
    assert MANIFEST.is_file(), f"缺少 Flatpak manifest：{MANIFEST}"
    return MANIFEST.read_text(encoding="utf-8")


def _manifest_data() -> dict:
    yaml = pytest.importorskip("yaml", reason="需要 PyYAML 解析 manifest")
    return yaml.safe_load(_manifest_text())


def _finish_args() -> list[str]:
    return [str(a) for a in _manifest_data().get("finish-args", [])]


# ---------------------------------------------------------------------------
# N2：不得强制覆盖配置目录
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pattern",
    [
        r"BACKUPSTUDIO_CONFIG_DIR",
        r"/var/config",
        r"--env=BACKUPSTUDIO_CONFIG_DIR",
    ],
)
def test_manifest_does_not_force_config_dir(pattern):
    """manifest 里**不得**出现任何强制配置目录的写法（N2 的根因）。"""
    text = _manifest_text()
    assert not re.search(pattern, text), (
        f"manifest 仍包含 {pattern!r}：\n"
        "Flatpak 沙箱内该路径不可写 → 程序回退到私有 tmpfs → 重启丢全部配置。\n"
        "正确做法：删掉这行，用 Flatpak 默认的 ~/.var/app/<app-id>/.config。"
    )


def test_finish_args_have_no_config_env_override():
    """finish-args 里不得有任何 BACKUPSTUDIO_CONFIG_DIR 的 --env。"""
    bad = [a for a in _finish_args() if "BACKUPSTUDIO_CONFIG_DIR" in a]
    assert not bad, f"finish-args 仍强制配置目录：{bad}"


def test_default_linux_config_dir_is_under_home(monkeypatch):
    """删掉覆盖后，Linux 默认配置目录必须落在 ``$XDG_CONFIG_HOME``/``~/.config`` 下。

    Flatpak 会把这些指到 ``~/.var/app/<app-id>/``，天然可写且持久。
    本机可能是 Windows，所以显式把 ``paths._platform()`` 切成 ``linux``
    （被测的就是 Linux 那条分支）。
    **不能** monkeypatch ``sys.platform``：PyInstaller 的 ``compat`` 模块在 import
    时就读它并缓存，全局改动会污染同进程里其它用例（已踩过）。
    """
    from backupstudio import paths

    monkeypatch.setattr(paths, "_platform", lambda: "linux")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    try:
        os.environ.pop("XDG_CONFIG_HOME", None)
        paths.reset_decisions()
        assert paths._appdata_dir() == Path.home() / ".config" / "backupstudio"

        os.environ["XDG_CONFIG_HOME"] = "/home/u/.var/app/x/.config"
        paths.reset_decisions()
        assert paths._appdata_dir() == Path("/home/u/.var/app/x/.config") / "backupstudio"
    finally:
        if old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old_xdg
        paths.reset_decisions()


# ---------------------------------------------------------------------------
# N4：必须有文件门户授权
# ---------------------------------------------------------------------------

def test_manifest_declares_portal_talk_name():
    """``finish-args`` 必须含 ``--talk-name=org.freedesktop.portal.Desktop``。

    没有它，GUI 的文件选择器与 home 之外的目录访问都不可靠；
    用户会看到「手动能备份、定时不备份」这种自相矛盾的现象。
    """
    args = _finish_args()
    assert f"--talk-name={PORTAL_BUS}" in args, (
        f"finish-args 缺少 --talk-name={PORTAL_BUS}，实际：{args}"
    )


def test_manifest_still_allows_home_and_removable_media():
    """删掉配置覆盖后，原有文件系统授权不能被误删。"""
    args = " ".join(_finish_args())
    for need in ("--filesystem=home", "--filesystem=/media", "--filesystem=/mnt", "--filesystem=/run/media"):
        assert need in args, f"finish-args 丢了 {need}"


def test_manifest_is_valid_json_object():
    """manifest 必须是合法 JSON（本仓库用的是 JSON 形式的 YAML）。"""
    import json

    data = json.loads(_manifest_text())
    assert data["app-id"] == APP_ID
    assert data["command"] == "backupstudio"
    names = [m.get("name") for m in data.get("modules", [])]
    assert "p7zip" in names and "backupstudio" in names, names


# ---------------------------------------------------------------------------
# 沙箱探测模块本身（N3/N4 的判据）
# ---------------------------------------------------------------------------

@pytest.fixture()
def flatpak_env(monkeypatch, tmp_path):
    """构造一个「看起来在 Flatpak 沙箱内」的环境，供探测逻辑测试。"""

    def _install(info_text: str | None) -> None:
        from backupstudio import flatpak

        flatpak.reset_cache()
        if info_text is None:
            monkeypatch.delenv("FLATPAK_ID", raising=False)
            monkeypatch.setattr(flatpak, "FLATPAK_INFO", tmp_path / "no-such-flatpak-info")
            return
        p = tmp_path / ".flatpak-info"
        p.write_text(info_text, encoding="utf-8")
        monkeypatch.setattr(flatpak, "FLATPAK_INFO", p)

    yield _install
    from backupstudio import flatpak

    flatpak.reset_cache()


SAMPLE_INFO = """\
[Application]
name=io.github.backupstudio.BackupStudio
runtime=org.freedesktop.Platform/x86_64/24.08
app-path=/app
app-commit=abc123
instance-path=/run/user/1000/.flatpak/io.github.backupstudio.BackupStudio
sandboxed=true

[Instance]
instance-id=12345
app-path=/app

[Context]
filesystems=home;/media;/mnt;/run/media;
shared=network;ipc;

[Session Bus Policy]
org.freedesktop.Notifications=talk
org.freedesktop.portal.Desktop=talk
org.kde.StatusNotifierWatcher=talk
"""


def test_not_flatpak_on_this_machine(flatpak_env):
    flatpak_env(None)
    from backupstudio import flatpak

    assert flatpak.is_flatpak() is False
    assert flatpak.autostart_supported() is True
    assert flatpak.daemon_supported() is True
    assert flatpak.describe() == []


def test_detects_flatpak_from_info_file(flatpak_env):
    flatpak_env(SAMPLE_INFO)
    from backupstudio import flatpak

    assert flatpak.is_flatpak() is True
    assert flatpak.app_id() == APP_ID
    assert "home" in flatpak.granted_filesystems()
    assert flatpak.portal_declared() is True


def test_detects_flatpak_from_env_var(flatpak_env, monkeypatch, tmp_path):
    """只有 FLATPAK_ID、没有标记文件时也要能识别（部分工具链如此）。"""
    from backupstudio import flatpak

    flatpak.reset_cache()
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(flatpak, "FLATPAK_INFO", tmp_path / "nope")
    monkeypatch.setenv("FLATPAK_ID", APP_ID)
    try:
        assert flatpak.is_flatpak() is True
    finally:
        flatpak.reset_cache()


def test_portal_not_declared_when_missing(flatpak_env):
    flatpak_env(SAMPLE_INFO.replace("org.freedesktop.portal.Desktop=talk\n", ""))
    from backupstudio import flatpak

    assert flatpak.is_flatpak() is True
    assert flatpak.portal_declared() is False


def test_autostart_unsupported_inside_flatpak(flatpak_env):
    """N3：沙箱内自启动必须判为不支持，并给出可执行的替代指引。"""
    flatpak_env(SAMPLE_INFO)
    from backupstudio import flatpak

    assert flatpak.autostart_supported() is False
    hint = flatpak.autostart_hint()
    assert "flatpak run" in hint and "start --minimized" in hint
    assert APP_ID in hint
    assert "不支持" in hint


def test_daemon_unsupported_inside_flatpak(flatpak_env):
    """N4：沙箱内不启动独立守护进程，指向内置调度器。"""
    flatpak_env(SAMPLE_INFO)
    from backupstudio import flatpak

    assert flatpak.daemon_supported() is False
    hint = flatpak.daemon_hint()
    assert "内置调度器" in hint
    assert "门户" in hint


def test_describe_reports_sandbox_limits(flatpak_env):
    flatpak_env(SAMPLE_INFO)
    from backupstudio import flatpak

    rows = dict(flatpak.describe())
    assert "Flatpak" in rows["运行沙箱"]
    assert "不支持" in rows["开机自启"]
    assert "内置调度器" in rows["守护进程"]
    assert "已声明" in rows["文件门户"]


def test_malformed_info_file_is_survivable(flatpak_env):
    """标记文件损坏时不能崩（环境探测失败永远不该拖垮程序）。"""
    flatpak_env("这不是 ini 格式\n还带中文\n")
    from backupstudio import flatpak

    assert isinstance(flatpak.is_flatpak(), bool)
    assert isinstance(flatpak.granted_filesystems(), list)


# ---------------------------------------------------------------------------
# N3：set_autostart 在沙箱内必须拒绝（而不是"假成功"）
# ---------------------------------------------------------------------------

def test_set_autostart_refuses_inside_flatpak(flatpak_env, monkeypatch):
    """关键回归：沙箱内 ``set_autostart(True)`` 不得返回成功。"""
    flatpak_env(SAMPLE_INFO)
    from backupstudio import autostart

    from backupstudio import flatpak

    flatpak.reset_cache()
    called = {"n": 0}
    monkeypatch.setattr(
        autostart, "_set_autostart_linux",
        lambda enabled: called.__setitem__("n", called["n"] + 1) or (True, "已写入（假）"),
    )
    ok, msg = autostart.set_autostart(True)
    assert ok is False, "Flatpak 沙箱内设置自启动却返回了成功（用户会以为下次登录生效）"
    assert "不支持" in msg and "flatpak run" in msg
    assert called["n"] == 0, "沙箱内不应再去写任何 autostart 文件"


def test_autostart_available_flag(flatpak_env):
    flatpak_env(SAMPLE_INFO)
    from backupstudio import autostart

    from backupstudio import flatpak

    flatpak.reset_cache()
    assert autostart.autostart_available() is False


def test_autostart_disable_still_allowed_inside_flatpak(flatpak_env, monkeypatch):
    """关闭自启（enabled=False）不该被拦——它只是清理，不会骗人。

    该断言与宿主平台无关：``set_autostart(False)`` 会按 ``sys.platform`` 分派到
    对应实现，若只 patch ``_set_autostart_linux``，在 Windows/macOS 上跑的 CI
    仍会走本机分支（实测 Windows runner 上 FileNotFoundError → 误红）。
    因此把三平台实现全部替换为桩。
    """
    flatpak_env(SAMPLE_INFO)
    from backupstudio import autostart

    from backupstudio import flatpak

    flatpak.reset_cache()
    stub = lambda enabled: (True, "已取消开机自启动")  # noqa: E731
    for name in ("_set_autostart_linux", "_set_autostart_windows", "_set_autostart_macos"):
        if hasattr(autostart, name):
            monkeypatch.setattr(autostart, name, stub)
    ok, msg = autostart.set_autostart(False)
    assert ok is True, msg


# ---------------------------------------------------------------------------
# N4：ensure_daemon 在沙箱内必须拒绝（而不是静默失败）
# ---------------------------------------------------------------------------

def test_ensure_daemon_refuses_inside_flatpak(flatpak_env, monkeypatch):
    flatpak_env(SAMPLE_INFO)
    from backupstudio import autostart, daemon

    from backupstudio import flatpak

    flatpak.reset_cache()
    spawned = {"n": 0}

    def fake_spawn(minimized: bool = False):
        spawned["n"] += 1
        return None

    monkeypatch.setattr(autostart, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(autostart, "find_daemon_pid", lambda *a, **k: None)
    ok, msg = daemon.ensure_daemon()
    assert ok is False, "Flatpak 沙箱内仍去拉起独立守护进程（home 之外的备份会静默失败）"
    assert "内置调度器" in msg
    assert spawned["n"] == 0, "沙箱内不应启动子进程守护进程"


def test_gui_settings_disables_controls_in_flatpak():
    """GUI 侧必须真的把这些开关/按钮置灰（不只是文案提示）。"""
    text = (ROOT / "backupstudio" / "gui" / "settings_page.py").read_text(encoding="utf-8")
    assert "autostart_available" in text, "设置页没有判断自启动是否真的可用"
    assert "daemon_available" in text or "daemon_supported" in text, (
        "设置页没有判断守护进程是否真的可用"
    )
    assert "setEnabled(False)" in text, "设置页没有把不可用的控件置灰"


# ---------------------------------------------------------------------------
# CI 门禁（真机上验证 Flatpak 行为的唯一途径）
# ---------------------------------------------------------------------------

def test_ci_has_flatpak_gates():
    """CI 必须对 Flatpak 做真机门禁（本机无法构建 Flatpak，只能靠 CI）。"""
    workflow = ROOT / ".github" / "workflows" / "build.yml"
    assert workflow.is_file()
    text = workflow.read_text(encoding="utf-8")
    assert "linux-flatpak" in text
    # 配置目录必须落在 ~/.var/app 下且真的可写（重启不丢的关键）
    assert ".var/app" in text, "CI 未校验 Flatpak 配置目录落在 ~/.var/app 下"
    # 自启动/守护进程在沙箱内必须被如实拒绝（返回非零或输出 WARN）
    assert "selftest" in text
