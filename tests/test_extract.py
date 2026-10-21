"""extract.py 加固回归（审查 R6-A5 / T8 / R1-N7）。"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestExtractZip:
    def test_plain_zip(self, tmp_path):
        from backupstudio.extract import extract_archive

        arc = tmp_path / "a.zip"
        with zipfile.ZipFile(arc, "w") as zf:
            zf.writestr("dir/x.txt", "hello")
        ok, msg = extract_archive(arc, tmp_path / "out")
        assert ok, msg
        assert (tmp_path / "out" / "dir" / "x.txt").read_text(encoding="utf-8") == "hello"

    def test_zip_slip_still_blocked(self, tmp_path):
        from backupstudio.extract import extract_archive

        arc = tmp_path / "evil.zip"
        with zipfile.ZipFile(arc, "w") as zf:
            zf.writestr("../evil.txt", "pwned")
        ok, _ = extract_archive(arc, tmp_path / "out2")
        assert ok is False
        assert not (tmp_path / "evil.txt").exists()

    def test_bomb_guard(self, tmp_path, monkeypatch):
        """高压缩比归档被拒绝（限制常数缩小以便快速测试）。"""
        from backupstudio import extract as ex

        monkeypatch.setattr(ex, "_MAX_EXPANSION_RATIO", 10)
        monkeypatch.setattr(ex, "_MAX_TOTAL_BYTES", 1024 * 1024)
        arc = tmp_path / "bomb.zip"
        with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", b"\x00" * (5 * 1024 * 1024))  # 5MB 零压缩到极小
        ok, msg = ex.extract_archive(arc, tmp_path / "out3")
        assert ok is False
        assert "炸弹" in msg or "超过上限" in msg, msg

    def test_entry_count_guard(self, tmp_path, monkeypatch):
        from backupstudio import extract as ex

        monkeypatch.setattr(ex, "_MAX_ENTRIES", 5)
        arc = tmp_path / "many.zip"
        with zipfile.ZipFile(arc, "w") as zf:
            for i in range(10):
                zf.writestr(f"f{i}.txt", "x")
        ok, msg = ex.extract_archive(arc, tmp_path / "out4")
        assert ok is False
        assert "条目数" in msg or "炸弹" in msg


class TestExtractEncrypted:
    def test_encrypted_zip_roundtrip_with_7z(self, tmp_path):
        """有 7-Zip：加密 ZIP（AES）带密码解压成功、往返内容一致。"""
        from backupstudio.extract import extract_archive
        from backupstudio.tools import find_7z

        seven = find_7z()
        if not seven:
            pytest.skip("本机无 7-Zip")
        payload = tmp_path / "secret.txt"
        payload.write_text("TOP SECRET DATA", encoding="utf-8")
        arc = tmp_path / "enc.zip"
        r = subprocess.run(
            [seven, "a", "-tzip", "-ppw123", "-mem=AES256", str(arc), str(payload)],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
        )
        assert r.returncode == 0, r.stdout
        # 无密码 → 失败（且不挂起——stdin 隔离）
        ok, _ = extract_archive(arc, tmp_path / "noPw")
        assert ok is False
        # 带密码 → 成功
        ok2, msg2 = extract_archive(arc, tmp_path / "withPw", password="pw123")
        assert ok2, msg2
        got = list((tmp_path / "withPw").rglob("secret.txt"))
        assert got and got[0].read_text(encoding="utf-8") == "TOP SECRET DATA"

    def test_no_pw_read_does_not_hang(self, tmp_path):
        """无密码读加密归档必须快速失败（stdin=DEVNULL；审查 T8 的 180s 挂起回归）。"""
        from backupstudio.extract import extract_archive
        from backupstudio.tools import find_7z

        seven = find_7z()
        if not seven:
            pytest.skip("本机无 7-Zip")
        payload = tmp_path / "s.txt"
        payload.write_text("x", encoding="utf-8")
        arc = tmp_path / "enc7.7z"
        subprocess.run(
            [seven, "a", "-t7z", "-ppw", str(arc), str(payload)],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
        )
        import time

        t0 = time.monotonic()
        ok, _ = extract_archive(arc, tmp_path / "o")
        elapsed = time.monotonic() - t0
        assert ok is False
        assert elapsed < 30, f"无密码读加密归档耗时 {elapsed:.1f}s（疑似挂起）"
