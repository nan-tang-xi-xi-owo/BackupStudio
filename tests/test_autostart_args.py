"""自启动命令的 CLI 契约（R-fix4）。

真实缺陷（P1-1）
----------------
``packaging/installer.iss.in`` 曾把开机自启项写成 ``Parameters: "--tray --minimized"``。
但 ``tray`` 是 argparse 的**子命令**，不是长选项；``--minimized`` 是 ``tray``/``start``
子命令自己的参数。于是 ``BackupStudio.exe --tray --minimized`` 会被判为
``error: unrecognized arguments`` → 进程 rc=2 立即退出。

后果：用户勾选「开机自启」后，定时备份一次都不执行，而且没有任何提示
（进程在 GUI 起来之前就死了，连日志都不一定留下）。

本文件把「合法写法集合」固化下来，防止回退。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backupstudio import autostart  # noqa: E402
from backupstudio.app import _build_parser  # noqa: E402

VALID_ARGV = [
    ["start", "--minimized"],  # autostart.py 各写入点（当前正确形态）
    ["start"],
    ["tray", "--minimized"],   # 旧注册表 / plist / .desktop 形态（兼容 no-op）
    ["tray"],                  # 裸 tray
]

INVALID_ARGV = [
    ["--tray", "--minimized"],  # ← 正是那个 bug
    ["--tray"],
    ["--minimized"],
]


@pytest.fixture(scope="module")
def parser():
    return _build_parser()


# ---------------------------------------------------------------------------
# argparse 契约
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", VALID_ARGV)
def test_valid_autostart_argv_parses(parser, argv):
    """这些写法必须能被 argparse 接受（不抛 SystemExit）。"""
    ns = parser.parse_args(argv)
    assert getattr(ns, "command", None) == argv[0]


@pytest.mark.parametrize("argv", INVALID_ARGV)
def test_invalid_autostart_argv_is_rejected(parser, argv):
    """``--tray`` 这类写法必须被拒绝——否则说明旧的 bug 又活了。"""
    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_start_minimized_sets_flag(parser):
    """``start --minimized`` 必须真正把 minimized 置 True。"""
    ns = parser.parse_args(["start", "--minimized"])
    assert ns.command == "start"
    assert getattr(ns, "minimized", False) is True


def test_start_without_minimized_defaults_false(parser):
    ns = parser.parse_args(["start"])
    assert getattr(ns, "minimized", True) is False


# ---------------------------------------------------------------------------
# 安装包模板
# ---------------------------------------------------------------------------

def test_installer_template_uses_valid_args():
    """``installer.iss.in`` 的自启动项必须用可解析的写法。"""
    iss = ROOT / "packaging" / "installer.iss.in"
    assert iss.is_file(), f"缺少 {iss}"
    text = iss.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if "Parameters:" in ln]
    assert lines, "installer.iss.in 里没有 Parameters: 行（自启动项丢了？）"
    for line in lines:
        # 取出 Parameters: "..." 的内容
        m = re.search(r'Parameters:\s*"([^"]*)"', line)
        assert m, f"无法解析 Parameters：{line.strip()}"
        params = m.group(1).split()
        # 必须能真的被 CLI 解析
        ns = _build_parser().parse_args(params)
        assert getattr(ns, "command", None) in ("start", "tray"), (
            f"安装包自启动参数 {params} 解析出的子命令异常"
        )
        # 必须带 minimized（开机自启要驻留托盘，不弹窗）
        assert getattr(ns, "minimized", False) is True, (
            f"安装包自启动参数 {params} 未启用 minimized"
        )


def test_installer_template_not_using_bare_tray_flag():
    """显式反向断言：整个模板里不得出现 ``--tray`` 这种非法写法。"""
    text = (ROOT / "packaging" / "installer.iss.in").read_text(encoding="utf-8")
    # 只看实际配置行（注释里可能提到历史写法，属说明性文字）
    config = "\n".join(
        ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith(";")
    )
    assert "--tray" not in config, (
        "installer.iss.in 仍在使用非法的 --tray 写法（tray 是子命令，不是选项）"
    )


# ---------------------------------------------------------------------------
# autostart.py 生成路径
# ---------------------------------------------------------------------------

def test_autostart_argv_parses(monkeypatch):
    """``autostart._autostart_argv()`` 产出的命令必须能解析。"""
    argv = autostart._autostart_argv()
    ns = _build_parser().parse_args(argv)
    assert getattr(ns, "command", None) == "start"
    assert getattr(ns, "minimized", False) is True


def test_autostart_verify_returns_empty(monkeypatch):
    """自检函数本身应返回空串（无问题）。"""
    assert autostart._verify_autostart_command() == ""


def test_autostart_verify_detects_bad_command(monkeypatch):
    """自检必须能抓住坏命令——这是防止再次写错的最后一道闸。"""
    monkeypatch.setattr(
        autostart, "gui_command", lambda: [sys.executable, "--tray", "--minimized"]
    )
    problem = autostart._verify_autostart_command()
    assert problem, "自检放过了一个非法自启动命令"


@pytest.mark.parametrize("frozen", [True, False])
def test_launch_spec_args_parse(frozen, monkeypatch):
    """冻结/源码两种形态下，自启动 argv 都能解析。

    注意：源码形态 ``launch_spec()`` 返回 ``[python, "-m", "backupstudio", "start"]``，
    其中 ``-m backupstudio`` 由解释器消费、不属于应用 argv；要验证的是
    ``autostart._autostart_argv()`` 归一化之后的形态。
    """
    monkeypatch.setattr(autostart, "is_frozen", lambda: frozen)
    exe, args, _workdir = autostart.launch_spec()
    assert exe  # 可执行文件非空
    argv = autostart._autostart_argv()
    ns = _build_parser().parse_args(argv)
    assert getattr(ns, "command", None) == "start"
    assert getattr(ns, "minimized", False) is True


def test_all_autostart_write_points_use_start():
    """四个平台写入点（注册表 / LaunchAgent / XDG autostart / systemd）都走 gui_command。"""
    text = (ROOT / "backupstudio" / "autostart.py").read_text(encoding="utf-8")
    # 每个写入点都是 launch_spec() 或 gui_command() + "--minimized"
    assert text.count('"--minimized"') >= 4, "自启动写入点数量异常（应 ≥4 处追加 --minimized）"
    # 不允许出现把 tray 当选项的写法
    assert '"--tray"' not in text, "autostart.py 出现了非法的 --tray 写法"
    assert "'--tray'" not in text


def test_contract_checker_script_passes():
    """CI 用的契约校验脚本必须能独立跑通（与 pytest 同一套断言）。"""
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "check_autostart_args.py")],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert proc.returncode == 0, f"契约校验脚本失败：\n{proc.stdout}\n{proc.stderr}"
