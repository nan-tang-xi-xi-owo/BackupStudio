"""pytest 共享夹具：配置目录隔离、缓存清理、7z 环境模拟。

背景（审查 R4/J-cfgdir）：paths._DECIDED 存在跨用例缓存泄漏；
capabilities 的 lru_cache 同样需要清理。本文件对所有用例自动生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """每个用例独立的配置目录 + 清空进程级缓存（防跨用例泄漏）。"""
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

    需要同时 patch 三处模块级绑定：
    - archives.find_7z（模块顶部 from .tools import find_7z）
    - capabilities.find_7z（同上）
    - tools.find_7z（engine 函数内 `from .tools import find_7z` 走这里）
    """
    from backupstudio import archives, capabilities, tools

    monkeypatch.setattr(archives, "find_7z", lambda: None)
    monkeypatch.setattr(capabilities, "find_7z", lambda: None)
    monkeypatch.setattr(tools, "find_7z", lambda: None)
    capabilities.reset_cache()
    yield
    capabilities.reset_cache()


def has_real_7z() -> bool:
    """本机 tools/ 或 PATH 中是否有真实 7-Zip（打包版环境标记）。"""
    from backupstudio.tools import find_7z

    return find_7z() is not None
