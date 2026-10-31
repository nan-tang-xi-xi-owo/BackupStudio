"""校验体系测试（审查 E-1/E-2/C1/X2 回归）。

核心断言：**skipped 绝不当成 passed**；损坏包必须被检出；
分卷校验走真实拼接（截断卷必须失败）。
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestVerifyArchive:
    def test_nonexistent_file(self, tmp_path):
        from backupstudio.engine import verify_archive

        ok, msg = verify_archive(tmp_path / "nope.zip", "zip")
        assert ok is False, "不存在的文件绝不判 passed"

    def test_valid_zip(self, tmp_path):
        from backupstudio.engine import verify_archive

        p = tmp_path / "good.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("a.txt", "hello")
        ok, _ = verify_archive(p, "zip")
        assert ok is True

    def test_truncated_zip(self, tmp_path):
        from backupstudio.engine import verify_archive

        p = tmp_path / "good.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("a.txt", "x" * 10000)
        data = p.read_bytes()
        bad = tmp_path / "bad.zip"
        bad.write_bytes(data[: len(data) // 2])
        ok, _ = verify_archive(bad, "zip")
        assert ok is False, "截断的 zip 必须判 failed"

    def test_7z_without_7z_not_passed(self, tmp_path, no_7z):
        """无 7-Zip 时损坏 7z 不得判 passed（E-1 的核心回归）。"""
        from backupstudio.engine import verify_archive

        bad = tmp_path / "broken.7z"
        bad.write_bytes(b"7z\xbc\xaf\x27\x1c" + os.urandom(200))  # 有 magic 但内容垃圾
        ok, msg = verify_archive(bad, "7z")
        assert ok is not True, f"损坏 7z 被判 passed！（msg={msg}）"

    def test_tar_roundtrip(self, tmp_path):
        from backupstudio.engine import verify_archive

        p = tmp_path / "good.tar"
        with tarfile.open(p, "w") as tf:
            data = b"hello world"
            ti = tarfile.TarInfo("a.txt")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        ok, _ = verify_archive(p, "tar")
        assert ok is True

    def test_tar_zst_without_7z_passes_real_file(self, tmp_path, no_7z):
        """无 7-Zip 时完好的 tar.zst 必须 passed（C1 回归——此前误报失败）。"""
        zstandard = pytest.importorskip("zstandard")
        from backupstudio.engine import verify_archive

        p = tmp_path / "good.tar.zst"
        with open(p, "wb") as raw:
            comp = zstandard.ZstdCompressor().stream_writer(raw)
            with tarfile.open(fileobj=comp, mode="w|") as tf:
                data = b"content"
                ti = tarfile.TarInfo("a.txt")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            comp.close()
        ok, msg = verify_archive(p, "tar.zst")
        assert ok is True, f"完好的 tar.zst 被判失败：{msg}"


class TestVolumeVerify:
    def test_split_volume_truncation_detected(self, tmp_path):
        """分卷截断必须被检出（E-2：此前只查 size>0，截成 1 字节仍 passed）。"""
        from backupstudio.archives import VolumeInfo
        from backupstudio.engine import _verify_volumes

        # 真造一个 zip 再手工切片
        src = tmp_path / "good.zip"
        with zipfile.ZipFile(src, "w") as zf:
            for i in range(20):
                zf.writestr(f"f{i}.txt", "x" * 5000)
        data = src.read_bytes()

        def make_vols(chunks):
            vols = []
            for i, chunk in enumerate(chunks, 1):
                p = tmp_path / f"v{i}.zip.part{i:02d}"
                p.write_bytes(chunk)
                vols.append(VolumeInfo(p, len(chunk)))
            return vols

        half = len(data) // 2
        good = make_vols([data[:half], data[half:]])
        ok, _ = _verify_volumes(good, "zip")
        assert ok is True, "完好的拼接分卷应 passed"

        truncated = make_vols([data[:half][: 1], data[half:]])  # 第一段截成 1 字节
        ok2, msg2 = _verify_volumes(truncated, "zip")
        assert ok2 is False, f"截断分卷未检出（msg={msg2}）"


class TestPy7zrPayloadVerify:
    """A1 回归：无 7-Zip 时 7z 校验必须读**载荷**，不能只读头。

    背景：旧实现只调 ``getnames()``，一个载荷被翻转字节的 300 KB 7z 也会返回
    ``(True, 'py7zr 结构校验通过')``，而同一文件 ``py7zr.extractall()`` 抛 CrcError。
    这是「skipped 不再当 passed」修复自己引入的**新假 passed**。
    """

    @staticmethod
    def _make_7z(tmp_path, name="src.bin", size=64 * 1024):
        py7zr = pytest.importorskip("py7zr")
        payload = tmp_path / name
        payload.write_bytes(os.urandom(size))
        arc = tmp_path / "good.7z"
        with py7zr.SevenZipFile(arc, "w") as z:
            z.write(payload, name)
        return arc

    def test_corrupted_payload_is_failed_not_passed(self, tmp_path, no_7z):
        """载荷中间翻转一个字节 → 必须 (False, ...)，绝不能 True。"""
        from backupstudio.engine import verify_archive

        arc = self._make_7z(tmp_path)
        data = bytearray(arc.read_bytes())
        mid = len(data) // 2
        data[mid] ^= 0xFF
        bad = tmp_path / "bad.7z"
        bad.write_bytes(bytes(data))

        ok, msg = verify_archive(bad, "7z")
        assert ok is False, f"载荷损坏的 7z 被判 {ok!r}（msg={msg}）—— A1 未修复"
        assert "CRC" in msg or "校验失败" in msg, msg

    def test_good_payload_is_passed(self, tmp_path, no_7z):
        """完好 7z 必须 True（不能为了修假通过而把好包误判失败）。"""
        from backupstudio.engine import verify_archive

        arc = self._make_7z(tmp_path)
        ok, msg = verify_archive(arc, "7z")
        assert ok is True, f"完好 7z 被判 {ok!r}（msg={msg}）"

    def test_header_only_damage_is_not_passed(self, tmp_path, no_7z):
        """截断到一半（头部不完整）→ 必须 False。"""
        from backupstudio.engine import verify_archive

        arc = self._make_7z(tmp_path)
        data = arc.read_bytes()
        trunc = tmp_path / "trunc.7z"
        trunc.write_bytes(data[: len(data) // 2])
        ok, msg = verify_archive(trunc, "7z")
        assert ok is False, f"截断 7z 被判 {ok!r}（msg={msg}）"

    def test_missing_testzip_degrades_to_skipped(self, tmp_path, no_7z, monkeypatch):
        """py7zr 无 testzip() 时必须降级为 skipped（None），而不是 passed。"""
        py7zr = pytest.importorskip("py7zr")
        from backupstudio.engine import verify_archive

        arc = self._make_7z(tmp_path)
        monkeypatch.delattr(py7zr.SevenZipFile, "testzip", raising=False)
        ok, msg = verify_archive(arc, "7z")
        assert ok is None, f"无 testzip() 时应 skipped，实际 {ok!r}（msg={msg}）"
        assert "testzip" in msg or "跳过" in msg, msg

    def test_encrypted_without_password_is_skipped_not_failed(self, tmp_path, no_7z):
        """加密 7z 未给密码 → skipped（None）；给了正确密码 → passed。"""
        py7zr = pytest.importorskip("py7zr")
        from backupstudio.engine import verify_archive

        payload = tmp_path / "s.txt"
        payload.write_text("secret", encoding="utf-8")
        arc = tmp_path / "enc.7z"
        with py7zr.SevenZipFile(arc, "w", password="pw") as z:
            z.write(payload, "s.txt")

        ok, _ = verify_archive(arc, "7z")
        assert ok is None, f"加密包无密码应 skipped，实际 {ok!r}"
        ok2, msg2 = verify_archive(arc, "7z", "pw")
        assert ok2 is True, f"正确密码应 passed，实际 {ok2!r}（msg={msg2}）"


class TestPy7zrCorruptVsUndecidable:
    """P1 回归：可压缩载荷损坏（LZMAError）必须判 failed，不能落进 skipped 兜底。

    盲区成因：``test_verify.py`` 原有用例的载荷是 ``os.urandom``（不可压缩）——
    随机数据翻转字节后仍能解码 → ``CrcError`` → 早就判 failed，测试常绿。
    真实备份（文本/日志/代码）高度可压缩，翻转中段字节后解码器报
    ``LZMAError: Corrupt input data``，旧实现落到 ``password is None`` 分支返回
    ``None``（skipped）→ 真实损坏被静默放过。
    """

    @staticmethod
    def _make_compressible_7z(tmp_path, size=200_000):
        py7zr = pytest.importorskip("py7zr")
        payload = tmp_path / "log.txt"
        # 真实备份形态：重复文本行（压缩率极高）
        payload.write_bytes(b"2026-10-24 INFO backupstudio compressible log line\n" * (size // 50))
        arc = tmp_path / "good.7z"
        with py7zr.SevenZipFile(arc, "w") as z:
            z.write(payload, "log.txt")
        return arc

    def test_corrupt_set_contains_lzma(self):
        """``_PY7ZR_CORRUPT`` 必须覆盖载荷层"输入损坏"的异常名。"""
        from backupstudio.engine import _PY7ZR_CORRUPT, _PY7ZR_UNDECIDABLE

        for name in ("LZMAError", "LZMACompressionError", "CrcError", "Bad7zFile"):
            assert name in _PY7ZR_CORRUPT, f"{name} 未归入 corrupt"
        assert not (_PY7ZR_CORRUPT & _PY7ZR_UNDECIDABLE), "两类异常不得重叠（否则顺序会串味）"

    def test_compressible_payload_corruption_is_failed(self, tmp_path, no_7z):
        """可压缩载荷翻转中段字节 → failed（旧实现返回 None）。"""
        from backupstudio.engine import verify_archive

        arc = self._make_compressible_7z(tmp_path)
        ok0, msg0 = verify_archive(arc, "7z")
        assert ok0 is True, f"完好的可压缩 7z 应 passed：{msg0}"

        data = bytearray(arc.read_bytes())
        data[len(data) // 2] ^= 0xFF
        bad = tmp_path / "bad.7z"
        bad.write_bytes(bytes(data))
        ok, msg = verify_archive(bad, "7z")
        assert ok is False, f"可压缩载荷损坏被判 {ok!r}（应为 failed）：{msg}"

    def test_compressible_good_payload_still_passed(self, tmp_path, no_7z):
        """修 LZMAError 不能把完好包误判失败。"""
        from backupstudio.engine import verify_archive

        arc = self._make_compressible_7z(tmp_path)
        ok, msg = verify_archive(arc, "7z")
        assert ok is True, f"完好可压缩 7z 被误判 {ok!r}：{msg}"

    def test_encrypted_without_password_still_none(self, tmp_path, no_7z):
        """加密包无密码仍须 None —— 加严 corrupt 分类不得殃及加密路径。"""
        py7zr = pytest.importorskip("py7zr")
        from backupstudio.engine import verify_archive

        payload = tmp_path / "s.txt"
        payload.write_text("secret" * 1000, encoding="utf-8")
        arc = tmp_path / "enc.7z"
        with py7zr.SevenZipFile(arc, "w", password="pw") as z:
            z.write(payload, "s.txt")

        ok, msg = verify_archive(arc, "7z")
        assert ok is None, f"加密包无密码应 skipped，实际 {ok!r}（password is None 分支被绕过？）：{msg}"
        ok2, msg2 = verify_archive(arc, "7z", "pw")
        assert ok2 is True, f"正确密码应 passed：{msg2}"