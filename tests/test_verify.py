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
