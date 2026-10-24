"""pytest 共享夹具：配置目录隔离、缓存清理、7z 环境模拟。

背景（审查 R4/J-cfgdir）：paths._DECIDED 存在跨用例缓存泄漏；
capabilities 的 lru_cache 同样需要清理。本文件对所有用例自动生效。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _patch_no_7z(monkeypatch) -> None:
    """把三处模块级 ``find_7z`` 绑定全部置为 None（模拟"无 7-Zip"机器）。

    需要同时 patch 全部绑定，否则会漏：
    - archives.find_7z / capabilities.find_7z / extract.find_7z（模块顶部 import）
    - tools.find_7z（engine 函数内 `from .tools import find_7z` 走这里）
    """
    from backupstudio import archives, capabilities, extract, tools

    monkeypatch.setattr(archives, "find_7z", lambda: None)
    monkeypatch.setattr(capabilities, "find_7z", lambda: None)
    monkeypatch.setattr(extract, "find_7z", lambda: None)
    monkeypatch.setattr(tools, "find_7z", lambda: None)
    capabilities.reset_cache()


@pytest.fixture(autouse=True)
def _no_7z_env(monkeypatch):
    """``BACKUPSTUDIO_NO_7Z=1`` 时，整轮测试在"无 7-Zip"环境下运行。

    CI 的"无 7z 轮"依赖此开关：仅靠改名 tools/win32/7z 不够——runner 自带
    Program Files\\7-Zip，find_7z() 会回退到 PATH/安装路径继续命中，
    导致门禁空转（审查 R-10）。本 fixture 在测试层确定性地屏蔽。
    """
    if os.environ.get("BACKUPSTUDIO_NO_7Z") == "1":
        _patch_no_7z(monkeypatch)
    yield


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch, _no_7z_env):
    """每个用例独立的配置目录 + 清空进程级缓存（防跨用例泄漏）。

    依赖 ``_no_7z_env`` —— 必须先屏蔽 7-Zip 再 reset_cache()，
    否则 environment_report() 会用真实 7-Zip 结果回填缓存。
    """
    from backupstudio import paths

    cfg = tmp_path / "_bs_config"
    cfg.mkdir()
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(cfg))
    paths.reset_decisions()
    try:
        from backupstudio import capabilities

        capabilities.reset_cache()
    except Exception:  # noqa: BLE001 - 探测层不可用时跳过
        pass
    yield cfg
    paths.reset_decisions()


@pytest.fixture()
def no_7z(monkeypatch):
    """模拟"无 7-Zip"环境（普通用户机 / CI test job）。

    与 ``BACKUPSTUDIO_NO_7Z=1`` 环境开关共用 ``_patch_no_7z``，保证两条
    路径模拟出的环境完全一致。
    """
    from backupstudio import capabilities

    _patch_no_7z(monkeypatch)
    yield
    capabilities.reset_cache()


def has_real_7z() -> bool:
    """本机 tools/ 或 PATH 中是否有真实 7-Zip（打包版环境标记）。"""
    from backupstudio.tools import find_7z

    return find_7z() is not None
