"""配置目录回退必须被如实报告（审查 N2 的自检侧）。

真实缺陷
--------
``paths.app_data_dir()`` 有一整条回退链（``XDG_RUNTIME_DIR`` → ``~/.backupstudio``
→ ``tempfile.gettempdir()``），设计上没问题；问题是 ``selftest`` 只检查
**最终**目录可写：

    cfg = config_dir()
    check("配置目录可写", cfg.is_dir() and os.access(cfg, os.W_OK), str(cfg))

而最终目录恰恰是回退出来的那个。Flatpak 里回退链终点是私有 tmpfs
（``/tmp``，重启即清空）→ 用户每次重启都丢光任务/设置/调度记录，
``selftest`` 却打印 ``[OK] 配置目录可写``，``--json`` 也没有任何异常标记。

修法：``paths`` 结构化记录「是否回退 / 首选目录 / 原因」，``selftest``
据此输出 ``[WARN]``（核心项仍 OK，因为程序确实能跑，只是数据不持久）。

注：这里**不 mock** selftest 的输出，而是直接调用 ``_cmd_selftest(as_json=True)``
解析它真实的 JSON。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backupstudio import paths  # noqa: E402


def _run_selftest_json(capsys) -> tuple[int, dict]:
    from backupstudio.app import _cmd_selftest

    code = _cmd_selftest(as_json=True)
    out = capsys.readouterr().out
    # JSON 是全量输出（as_json 下不打印人类可读文本），直接解析
    return code, json.loads(out)


@pytest.fixture(autouse=True)
def _clean_paths():
    paths.reset_decisions()
    yield
    paths.reset_decisions()


# ---------------------------------------------------------------------------
# paths 侧：结构化状态
# ---------------------------------------------------------------------------


def _block_preferred_config(tmp_path, monkeypatch):
    """按平台把「首选配置目录」变成不可用（用普通文件占位使 mkdir 失败）。

    返回该"占位文件"路径，供断言 ``preferred`` 使用。

    各平台的首选目录来源不同，所以必须设对应的环境变量：
      win32  → APPDATA
      darwin → HOME（首选 = ~/Library/Application Support/<app>）
      其它   → XDG_CONFIG_HOME
    """
    import sys

    monkeypatch.delenv("BACKUPSTUDIO_CONFIG_DIR", raising=False)
    if sys.platform == "win32":
        blocker = tmp_path / "appdata"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(blocker))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        return blocker
    if sys.platform == "darwin":
        home = tmp_path / "home"
        appsup = home / "Library" / "Application Support"
        appsup.mkdir(parents=True, exist_ok=True)
        blocker = appsup / "BackupStudio"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        return blocker
    # Linux / 其它 POSIX
    blocker = tmp_path / "xdg"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return blocker

def _prime_writable_preferred(tmp_path, monkeypatch):
    """按平台把「首选配置目录」指向 tmp_path 下的可写位置。

    用于那些主断言与首选目录无关的用例（如测 ``BACKUPSTUDIO_CONFIG_DIR``
    被忽略时），避免落到真实用户目录、也避免在 Linux 上设了 ``APPDATA``
    却根本没被读取。
    """
    import sys

    if sys.platform == "win32":
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    elif sys.platform == "darwin":
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

def test_state_not_fell_back_for_writable_override(tmp_path, monkeypatch):
    """显式指定且可写的覆盖目录 = 用户的选择，**不算**回退。"""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(cfg))
    paths.reset_decisions()
    st = paths.config_dir_state()
    assert st["actual"] == cfg
    assert st["fell_back"] is False
    assert st["override_ignored"] is False


def test_state_flags_unusable_override(tmp_path, monkeypatch):
    """指定的目录不可用 → 必须标记 ``override_ignored``（而不是悄悄换地方）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(blocker / "sub"))
    _prime_writable_preferred(tmp_path, monkeypatch)
    paths.reset_decisions()
    st = paths.config_dir_state()
    assert st["override_ignored"] is True, "不可用的 BACKUPSTUDIO_CONFIG_DIR 没有被标记"
    assert st["actual"] != blocker / "sub"
    assert st["reason"], "回退/忽略必须记录原因"


def test_state_flags_fallback_when_preferred_unwritable(tmp_path, monkeypatch):
    """首选目录不可写 → ``fell_back`` 为真，并记录首选目录与原因。"""
    blocker = _block_preferred_config(tmp_path, monkeypatch)
    paths.reset_decisions()
    st = paths.config_dir_state()
    assert st["fell_back"] is True, (
        f"首选目录不可写却没有标记 fell_back：{st}"
    )
    # 首选目录名按平台不同：Windows 用 APP_NAME（BackupStudio），
    # Linux 用 app 名（backupstudio）。用 paths 内部常量断言，避免写死大小写。
    preferred = Path(str(st["preferred"]))
    assert preferred.parent == blocker, f"首选目录应落在占位路径下：{preferred}"
    assert Path(str(st["actual"])) != preferred
    assert st["reason"], "回退必须记录原因"


def test_reset_clears_state(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(tmp_path / "cfg"))
    paths.reset_decisions()
    paths.config_dir_state()
    paths.reset_decisions()
    assert paths.STARTUP_DIAGNOSTICS == []
    # 清空后再决定一次仍然正常
    assert paths.config_dir_state()["actual"] == tmp_path / "cfg"


# ---------------------------------------------------------------------------
# selftest 侧：必须输出 WARN（而不是纯 OK）
# ---------------------------------------------------------------------------

def test_selftest_reports_fallback_warning(tmp_path, monkeypatch, capsys):
    """核心回归：回退后 selftest 必须出现 WARN，且 JSON 里可断言。"""
    blocker = _block_preferred_config(tmp_path, monkeypatch)
    # 让运行时/日志目录也走临时位置，避免落到真实 APPDATA
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    paths.reset_decisions()

    code, payload = _run_selftest_json(capsys)
    assert payload["config_fell_back"] is True, (
        f"配置目录已回退，selftest 却报告未回退：{payload}"
    )
    statuses = {c["label"]: c["status"] for c in payload["checks"]}
    assert any(s == "WARN" for s in statuses.values()), (
        f"回退时没有任何 WARN —— 这就是「假绿」：{statuses}"
    )
    assert payload["warnings"], "warnings 列表为空"
    assert any("回退" in w for w in payload["warnings"]), payload["warnings"]
    # 核心项仍然通过（程序能跑，只是数据不持久），所以退出码是 0
    assert code == 0, "回退属于「需要留意」，不该当成硬失败"
    assert payload["ok"] is True


def test_selftest_clean_env_has_no_config_warning(tmp_path, monkeypatch, capsys):
    """正常环境（可写目录）不得出现配置目录 WARN —— 否则门禁会一直红。"""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(cfg))
    paths.reset_decisions()

    code, payload = _run_selftest_json(capsys)
    assert payload["config_fell_back"] is False
    assert payload["config_override_ignored"] is False
    assert not any("配置目录" in w for w in payload["warnings"]), payload["warnings"]
    assert code == 0


def test_selftest_reports_ignored_override(tmp_path, monkeypatch, capsys):
    """指定了不可用的目录 → 必须报 WARN（用户以为在用那个目录）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(blocker / "sub"))
    _prime_writable_preferred(tmp_path, monkeypatch)
    paths.reset_decisions()

    code, payload = _run_selftest_json(capsys)
    assert payload["config_override_ignored"] is True
    assert any("BACKUPSTUDIO_CONFIG_DIR" in w for w in payload["warnings"]), payload["warnings"]
    assert code == 0


def test_selftest_json_exposes_paths(tmp_path, monkeypatch, capsys):
    """JSON 必须给出实际目录与首选目录（便于 CI 断言，不靠匹配中文）。"""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(cfg))
    paths.reset_decisions()

    _code, payload = _run_selftest_json(capsys)
    assert Path(payload["config_dir"]) == cfg
    assert payload["preferred_config_dir"]
    assert "sandbox" in payload
    for key in ("is_flatpak", "autostart_supported", "daemon_supported", "portal_declared"):
        assert key in payload["sandbox"], f"selftest JSON 缺 sandbox.{key}"


def test_selftest_text_output_still_says_all_pass(tmp_path, monkeypatch, capsys):
    """文本输出保持向后兼容：正常环境下仍是「全部通过 ✓」。"""
    from backupstudio.app import _cmd_selftest

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(cfg))
    paths.reset_decisions()

    code = _cmd_selftest(as_json=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "全部通过" in out, out[-500:]


def test_selftest_text_shows_warn_on_fallback(tmp_path, monkeypatch, capsys):
    """文本输出也必须出现 WARN 字样（用户看的就是这个）。"""
    from backupstudio.app import _cmd_selftest

    blocker = _block_preferred_config(tmp_path, monkeypatch)
    paths.reset_decisions()

    code = _cmd_selftest(as_json=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "[WARN]" in out, f"回退时文本输出没有 WARN：\n{out}"
    assert "回退" in out, out[-800:]


# ---------------------------------------------------------------------------
# 图形界面侧：回退必须可见
# ---------------------------------------------------------------------------

def test_settings_page_shows_fallback(tmp_path, monkeypatch):
    """设置页的「配置目录」必须标出回退（否则用户永远不知道数据会丢）。"""
    text = (ROOT / "backupstudio" / "gui" / "settings_page.py").read_text(encoding="utf-8")
    assert "config_dir_state" in text, "设置页没有读取配置目录状态"
    assert "fell_back" in text, "设置页没有处理回退状态"
    assert "已回退" in text, "设置页没有把回退展示出来"


def test_appdata_dir_never_raises_with_hostile_env(monkeypatch, tmp_path):
    """任何环境都不能让目录决定抛异常（启动路径上的硬性要求）。

    构造「覆盖目录 + 首选目录都不可用」的敌对环境，断言：
      1. ``app_data_dir()`` 仍返回 Path（不抛异常）；
      2. 状态里标记了回退（fell_back=True）。
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    # 首选目录也要不可用 —— 用 _block_preferred_config（按平台设对应 env）
    _block_preferred_config(tmp_path, monkeypatch)
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(blocker / "sub"))
    paths.reset_decisions()
    got = paths.app_data_dir()
    assert isinstance(got, Path)
    state = paths.config_dir_state()
    assert state["actual"] == got
    assert state["fell_back"] is True, f"所有候选都不可用却没有标记回退：{state}"
