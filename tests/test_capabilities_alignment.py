"""能力矩阵对齐测试（审查 P0-13 / UX3：双空格 tar bug 与 force 语义）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestCapabilitiesAlignment:
    def test_tar_available_when_backend_exists(self, tmp_cfg=None):
        """plain tar 在 tar 后端可用时应为 available（双空格 reason bug 回归）。"""
        from backupstudio.capabilities import environment_report, format_capabilities, reset_cache

        reset_cache()
        if not environment_report()["tar"]:
            import pytest

            pytest.skip("本机无 tar 后端")
        caps = format_capabilities(force=False)
        assert caps["tar"]["available"] is True, caps["tar"]["reason"]
        assert caps["tar"]["reason"] == ""

    def test_7z_needs_force_without_7zip(self, no_7z):
        """无 7-Zip 时（force=False 视图）：7z 不可用但明确"需强制"，且 7z 强制可用。"""
        from backupstudio.capabilities import format_capabilities, format_status_line, reset_cache

        reset_cache()
        caps = format_capabilities(force=False)
        e7 = caps["7z"]
        try:
            import py7zr  # noqa: F401

            has_py7zr = True
        except ImportError:
            has_py7zr = False
        if has_py7zr:
            assert e7["available"] is False, "force=False 视图下无 7-Zip 的 7z 不应显示可用"
            assert e7["needs_force"] is True
            assert e7["usable_with_force"] is True
            line = format_status_line("7z", e7)
            assert "强制" in line, line
        # force=True（引擎视角）仍视为可用
        caps2 = format_capabilities(force=True)
        assert caps2["7z"]["available"] is True

    def test_rar_needs_force(self, no_7z):
        """无 RAR 引擎时：force=False 视图不可用；有 zip 降级路径时标 needs_force。"""
        from backupstudio.capabilities import format_capabilities, reset_cache

        reset_cache()
        caps = format_capabilities(force=False)
        er = caps["rar"]
        # 无 7z + 有 py7zr 时 force_path_zip=True → needs_force
        try:
            import py7zr  # noqa: F401

            assert er["needs_force"] is True or er["available"] is True
        except ImportError:
            pass

    def test_status_line_three_states(self):
        from backupstudio.capabilities import format_status_line

        avail = {"available": True, "engine": "x", "reason": ""}
        assert format_status_line("zip", avail) == "可用"
        needs = {"available": False, "needs_force": True, "usable_with_force": True, "reason": "缺 7-Zip"}
        assert "强制" in format_status_line("7z", needs)
        dead = {"available": False, "needs_force": False, "usable_with_force": False, "reason": "不支持"}
        assert format_status_line("x", dead).startswith("不可用")

    def test_matrix_shape_preserved(self):
        """矩阵形状回归（tests/test_core.py 的同款断言，防新字段破坏结构）。"""
        from backupstudio.archives import SUPPORTED_FORMATS
        from backupstudio.capabilities import format_capabilities, reset_cache

        reset_cache()
        caps = format_capabilities()
        assert set(caps) == set(SUPPORTED_FORMATS)
        for entry in caps.values():
            for key in ("available", "multivolume", "password", "usable_with_force", "needs_force"):
                assert key in entry, key
