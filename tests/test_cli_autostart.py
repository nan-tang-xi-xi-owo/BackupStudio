"""G-13 回归：开机自启动命令必须能被 CLI 解析。

历史缺陷：autostart 生成 ``tray --minimized``（源码形态）或裸 ``--minimized``
（冻结形态），但 argparse 只为 ``start`` 注册了 ``--minimized`` →
登录后进程 rc=2 立即退出且无提示（设置页仍显示"已开启"）。

这里只测命令串解析，不触碰注册表 / 文件系统 / GUI。
"""

from __future__ import annotations

import sys

import pytest

from backupstudio import autostart
from backupstudio.app import _build_parser


def _app_argv(full_cmd: list[str]) -> list[str]:
    """剥掉解释器与 ``-m backupstudio``，得到应用自身收到的 argv。

    对应 ``app.main()`` 里的 ``sys.argv[1:]``：
    * 源码：``python -m backupstudio start --minimized`` → ``["start", "--minimized"]``
    * 冻结：``BackupStudio.exe start --minimized``       → ``["start", "--minimized"]``
    """
    argv = list(full_cmd[1:])
    if argv[:2] == ["-m", "backupstudio"]:
        argv = argv[2:]
    return argv


def _parse(argv: list[str]):
    """解析命令串；若 argparse 拒绝会抛 SystemExit（rc=2），即测试失败。"""
    return _build_parser().parse_args(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["start", "--minimized"],  # 现在写入的形态
        ["tray", "--minimized"],   # 旧注册表 / plist / .desktop / installer 形态（兼容 no-op）
        ["tray"],                  # 裸 tray
        ["start"],                 # 裸 start
    ],
)
def test_command_forms_all_parse(argv):
    assert _parse(argv).command == argv[0]


@pytest.mark.parametrize("frozen", [False, True])
def test_generated_autostart_argv_normalized_and_parses(frozen, monkeypatch):
    """真实生成路径：源码/冻结两种形态都产出 ``start --minimized`` 并可直接解析。"""
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    argv = autostart._autostart_argv()
    assert argv == ["start", "--minimized"]  # 规范化：不再沿用 tray
    _parse(argv)


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux-desktop", "linux-systemd"])
@pytest.mark.parametrize("frozen", [False, True])
def test_platform_command_shapes_parse(platform, frozen, monkeypatch):
    """模拟四个写入点（Run 键 / LaunchAgent / .desktop / systemd）各自生成的命令形态。"""
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    exe, args, _workdir = autostart.launch_spec()
    full = [exe, *args, "--minimized"]  # 四个写入点一致：launch_spec() + "--minimized"

    if platform == "win32":
        rendered = " ".join(autostart._quote_windows(p) for p in full)
    elif platform == "linux-desktop":
        rendered = " ".join(autostart._desktop_quote(p) for p in full)
    else:
        rendered = " ".join(full)
    assert "start" in rendered  # 命令串里出现的是 start 而不是 tray

    app_argv = _app_argv(full)
    assert app_argv == ["start", "--minimized"]
    _parse(app_argv)


def test_frozen_exe_shape_parses():
    """冻结形态 ``[exe, "start", "--minimized"]``（exe 换成 sys.executable）可解析。"""
    full = [sys.executable, "start", "--minimized"]
    assert _parse(_app_argv(full)).command == "start"


@pytest.mark.parametrize("frozen", [False, True])
def test_selfcheck_passes_for_generated_command(frozen, monkeypatch):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    assert autostart._verify_autostart_command() == ""


def test_selfcheck_catches_unparseable_command(monkeypatch):
    """反向验证：无法解析的命令必须被自检拦截（返回非空说明）。"""
    monkeypatch.setattr(
        autostart, "gui_command", lambda: [sys.executable, "-m", "backupstudio", "nonsense"]
    )
    assert autostart._verify_autostart_command() != ""
