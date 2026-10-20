"""加密正确性回归测试：N2（裸 LZMA2）/ fail-closed / 事后实测回填。

关键方法：raw LZMA2 全偏移扫描——真正的加密（AES coder）会让任何偏移的
原始 LZMA2 解码失败；而"假加密"（仅 header 加密 + 明文 LZMA2 载荷）能被
解出明文 canary。不能用字节 grep 判 AES（真假归档头都含 AES 标识）。
"""

from __future__ import annotations

import lzma
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CANARY = b"CANARY_FOR_RAW_LZMA2_DETECTION_12345"


@pytest.fixture()
def src_with_canary(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    (src / "secret.txt").write_text(CANARY.decode() * 10, encoding="utf-8")
    return src


def _make_task(fmt, dest, src, **opt_kwargs):
    from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

    return BackupTask(
        name=f"t-{fmt}",
        sources=[SourceConfig(path=str(src))],
        destination=str(dest),
        options=ArchiveOptions(fmt=fmt, **opt_kwargs),
    )


def _raw_lzma2_leak(path: Path) -> bool:
    """扫描全部偏移尝试 raw LZMA2 解码；命中 canary 返回 True（=泄漏）。"""
    data = path.read_bytes()
    for offset in range(0, min(len(data), 4096)):
        for dict_size in (1 << 16, 1 << 20, 1 << 24):
            try:
                dec = lzma.LZMADecompressor(
                    format=lzma.FORMAT_RAW,
                    filters=[{"id": lzma.FILTER_LZMA2, "dict_size": dict_size}],
                )
                out = dec.decompress(data[offset:], max_length=2_000_000)
                if CANARY in out:
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


class TestEncryption:
    def test_forced_7z_aes256_payload_is_encrypted(self, src_with_canary, tmp_path, no_7z):
        """无 7-Zip + 强制 7z+aes256：载荷必须是真加密（raw LZMA2 扫不出明文）。"""
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "out-enc"
        rec = TaskEngine().run(
            _make_task("7z", dest, src_with_canary, password="RealPw!", encrypt_mode="aes256", force=True)
        ).record
        assert rec.status == "success", rec.errors
        arc = Path(rec.archive)
        assert not _raw_lzma2_leak(arc), "载荷可被 raw LZMA2 解出明文——假加密！"
        # coder chain 必须含 AES
        import py7zr

        with py7zr.SevenZipFile(str(arc), "r", password="RealPw!") as z:
            folders = z.header.main_streams.unpackinfo.folders
            aes_bytes = py7zr.FILTER_CRYPTO_AES256_SHA256.to_bytes(4, "big")
            has_aes = any(
                isinstance(c.get("method"), bytes) and c["method"] == aes_bytes
                for fo in folders
                for c in fo.coders
            )
            assert has_aes, "coder chain 无 AES"
        # 无密码必须打不开
        with pytest.raises(Exception):
            with py7zr.SevenZipFile(str(arc), "r") as z:
                z.getnames()

    def test_zip_password_no_7z_fail_closed(self, src_with_canary, tmp_path, no_7z):
        """无 7-Zip + zip+密码：必须明确失败（而不是静默产出明文 zip）。"""
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "out-zip-pw"
        rec = TaskEngine().run(
            _make_task("zip", dest, src_with_canary, password="pw", encrypt_mode="aes256", force=True)
        ).record
        assert rec.status == "failure"
        assert rec.encrypted is False
        # 不得留下明文产物
        if rec.archive:
            arc = Path(rec.archive)
            if arc.exists():
                with zipfile.ZipFile(arc) as zf:
                    for info in zf.infolist():
                        if not info.is_dir():
                            assert info.flag_bits & 0x1, "产出了未加密的 zip！"

    def test_gpg_fail_closed(self, src_with_canary, tmp_path, no_7z):
        """gpg 加密（未实现）：必须明确失败，不得静默明文。"""
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "out-gpg"
        rec = TaskEngine().run(
            _make_task("tar.gz", dest, src_with_canary, password="pw", encrypt_mode="gpg", force=True)
        ).record
        assert rec.status == "failure"
        assert rec.encrypted is False

    def test_record_encrypted_matches_reality(self, src_with_canary, tmp_path, no_7z):
        """record.encrypted 必须与产物实测一致（无密码任务 = False）。"""
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "out-nopw"
        rec = TaskEngine().run(_make_task("zip", dest, src_with_canary, force=True)).record
        assert rec.status == "success", rec.errors
        assert rec.encrypted is False
