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


class TestBombGuardDoesNotSelfHarm:
    """A4 回归：绝对上限为主，且必须能放宽 —— 不得拒绝本产品自己产的正常备份。

    背景：旧实现 `limit = min(100 GiB, size × 1000)` 是硬拒绝且无逃生阀。
    实测一个 8 MiB 全零日志用 deflate 压到 8 KB（1014×），恢复时被自己的防护挡住。
    """

    @staticmethod
    def _zeros_zip(tmp_path, name="self.zip", size=8 * 1024 * 1024):
        arc = tmp_path / name
        with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("log.txt", b"\x00" * size)
        return arc

    def test_own_high_ratio_backup_is_extractable(self, tmp_path):
        """1014× 的自有备份必须能解开（旧实现直接拒绝）。"""
        from backupstudio.extract import extract_archive

        arc = self._zeros_zip(tmp_path)
        ratio = (8 * 1024 * 1024) / arc.stat().st_size
        assert ratio > 1000, f"测试前提失败：压缩比只有 {ratio:.0f}x"
        out = tmp_path / "out"
        ok, msg = extract_archive(arc, out)
        assert ok, f"{ratio:.0f}x 的自有备份被拒绝：{msg}"
        restored = out / "log.txt"
        assert restored.exists() and restored.stat().st_size == 8 * 1024 * 1024

    def test_absolute_cap_still_blocks_real_bomb(self, tmp_path, monkeypatch):
        """绝对上限压到 1 MiB 时，同一个包必须被拦（防护没被削弱成摆设）。"""
        from backupstudio import extract as ex

        arc = self._zeros_zip(tmp_path)
        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, str(1024 * 1024))
        ok, msg = ex.extract_archive(arc, tmp_path / "blocked")
        assert ok is False
        assert "炸弹" in msg or "超过上限" in msg, msg

    def test_env_can_disable_absolute_cap(self, tmp_path, monkeypatch):
        """BACKUPSTUDIO_MAX_UNPACKED_BYTES=0 = 关闭绝对上限，恢复必须成功。"""
        from backupstudio import extract as ex

        arc = self._zeros_zip(tmp_path)
        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, "0")
        ok, msg = ex.extract_archive(arc, tmp_path / "relaxed")
        assert ok, f"放宽开关无效：{msg}"

    def test_ratio_is_an_outer_bound_not_a_tight_cap(self, tmp_path, monkeypatch):
        """比例默认 10000（而非 1000）；刻意调小到 2 时仍能兜住。"""
        from backupstudio import extract as ex

        assert ex._MAX_EXPANSION_RATIO >= 10000, "默认比例应放宽到 10000"
        arc = self._zeros_zip(tmp_path)
        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, "0")
        monkeypatch.setenv(ex.ENV_MAX_UNPACK_RATIO, "2")
        ok, msg = ex.extract_archive(arc, tmp_path / "ratio")
        assert ok is False, "比例兜底失效"
        assert "炸弹" in msg or "超过上限" in msg, msg

    def test_cli_flag_overrides_limit(self, tmp_path, monkeypatch):
        """--max-unpacked-bytes 必须能被 CLI 解析并生效（逃生阀存在性）。"""
        from backupstudio import app as app_mod

        p = app_mod._build_parser()
        ns = p.parse_args(["extract", "a.zip", "--to", "o", "--max-unpacked-bytes", "0"])
        assert ns.max_unpacked_bytes == 0
        ns2 = p.parse_args(["extract", "a.zip", "--to", "o", "--max-unpack-ratio", "5"])
        assert ns2.max_unpack_ratio == 5

    def test_7z_external_has_cap_too(self, tmp_path, monkeypatch):
        """7z 外部解压路径也必须有绝对上限（旧实现完全没有炸弹防护）。"""
        from backupstudio import extract as ex
        from backupstudio.tools import find_7z

        seven = find_7z()
        if not seven:
            pytest.skip("本机无 7-Zip")
        import subprocess

        payload = tmp_path / "zeros.bin"
        payload.write_bytes(b"\x00" * (4 * 1024 * 1024))
        arc = tmp_path / "self.7z"
        r = subprocess.run([seven, "a", "-t7z", "-mx9", str(arc), str(payload)],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=300)
        if r.returncode != 0 or not arc.exists():
            # 某些受限环境（沙箱/杀软）里 7za 无法在临时目录建包，此时无法验证 7z 路径
            pytest.skip(f"本机 7-Zip 无法创建压缩包，跳过 7z 炸弹预检用例：{(r.stdout or b'')[-160:]!r}")

        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, str(1024 * 1024))
        ok, msg = ex.extract_archive(arc, tmp_path / "blocked7z")
        assert ok is False, f"7z 炸弹未被拦：{msg}"
        assert "炸弹" in msg or "超过上限" in msg, msg

        monkeypatch.delenv(ex.ENV_MAX_UNPACKED_BYTES)
        ok2, msg2 = ex.extract_archive(arc, tmp_path / "ok7z")
        assert ok2, f"正常 7z 被误拦：{msg2}"


class TestRarBombPrecheck:
    """P1 回归：rar 预检此前形同虚设（用 7z 的 ``-slt``/``-ba`` 参数去打 UnRAR）。

    UnRAR 不认 ``-slt``/``-ba``，返回非 0 且输出 ``Unknown option: ba`` →
    ``_declared_unpacked_7z`` 返回 None → ``_check_external_bomb`` 静默放行。
    """

    def test_declared_unpacked_unrar_parses_lt(self, tmp_path):
        """``_declared_unpacked_unrar`` 必须能从 ``unrar lt`` 读出真实大小。"""
        from backupstudio import extract as ex
        from backupstudio.tools import find_rar, find_unrar

        unrar, rar = find_unrar(), find_rar()
        if not unrar or not rar:
            pytest.skip("本机无 UnRAR/Rar")

        payload = tmp_path / "zeros.bin"
        payload.write_bytes(b"\x00" * (2 * 1024 * 1024))
        arc = tmp_path / "t.rar"
        r = subprocess.run([rar, "a", "-idq", "-m3", "-y", str(arc), str(payload)],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=300)
        if r.returncode != 0 or not arc.exists():
            pytest.skip(f"本机 Rar 无法建包：{(r.stdout or b'')[-160:]!r}")

        # 旧函数（7z 参数）读不出来 —— 证明"形同虚设"的根因
        assert ex._declared_unpacked_7z(unrar, arc, None) is None
        # 新函数能读
        got = ex._declared_unpacked_unrar(unrar, arc, None)
        assert got == 2 * 1024 * 1024, f"应读出 2 MiB，实际 {got}"

    def test_rar_bomb_is_blocked(self, tmp_path, monkeypatch):
        """上限压到 1 MiB 时 rar 炸弹必须被拒。"""
        from backupstudio import extract as ex
        from backupstudio.extract import extract_archive
        from backupstudio.tools import find_rar, find_unrar

        unrar, rar = find_unrar(), find_rar()
        if not unrar or not rar:
            pytest.skip("本机无 UnRAR/Rar")

        payload = tmp_path / "zeros.bin"
        payload.write_bytes(b"\x00" * (4 * 1024 * 1024))
        arc = tmp_path / "bomb.rar"
        r = subprocess.run([rar, "a", "-idq", "-m3", "-y", str(arc), str(payload)],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=300)
        if r.returncode != 0 or not arc.exists():
            pytest.skip(f"本机 Rar 无法建包：{(r.stdout or b'')[-160:]!r}")

        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, str(1024 * 1024))
        ok, msg = extract_archive(arc, tmp_path / "blocked")
        assert ok is False, f"rar 炸弹未被拦：{msg}"
        assert "炸弹" in msg or "超过上限" in msg, msg

        # 放宽后必须能解（证明不是"一律拒绝"）
        monkeypatch.delenv(ex.ENV_MAX_UNPACKED_BYTES)
        ok2, msg2 = extract_archive(arc, tmp_path / "ok")
        assert ok2, f"普通 rar 被误拦：{msg2}"
        got = list((tmp_path / "ok").rglob("*.bin"))
        assert got and got[0].stat().st_size == 4 * 1024 * 1024

    def test_unreadable_precheck_refuses_instead_of_passing(self, tmp_path):
        """预检读不出来时**拒绝放行**（旧实现静默 return 放行 = 防护形同虚设）。"""
        from backupstudio import extract as ex

        arc = tmp_path / "x.rar"
        arc.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 100)
        with pytest.raises(ex.ExtractionError) as ei:
            ex._check_external_bomb(str(tmp_path / "no-such-unrar.exe"), arc, None, unrar=True)
        assert "无法预读" in str(ei.value), str(ei.value)


class TestUnprotectedPaths:
    """P1 回归：两条零防护的外部解压路径（AES 加密 ZIP / tar.zst 7z 回退）。"""

    def test_encrypted_zip_bomb_blocked(self, tmp_path, monkeypatch):
        """AES 加密 ZIP 炸弹：``_extract_zip`` 的 password→7z 分支此前完全不检查。"""
        from backupstudio import extract as ex
        from backupstudio.extract import extract_archive
        from backupstudio.tools import find_7z

        seven = find_7z()
        if not seven:
            pytest.skip("本机无 7-Zip")

        payload = tmp_path / "zeros.bin"
        payload.write_bytes(b"\x00" * (8 * 1024 * 1024))
        arc = tmp_path / "bomb-enc.zip"
        r = subprocess.run(
            [seven, "a", "-tzip", "-ppw", "-mem=AES256", "-mx9", str(arc), str(payload)],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=300,
        )
        if r.returncode != 0 or not arc.exists():
            pytest.skip(f"本机 7-Zip 无法建加密包：{(r.stdout or b'')[-160:]!r}")

        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, str(1024 * 1024))
        ok, msg = extract_archive(arc, tmp_path / "blocked", password="pw")
        assert ok is False, f"加密 ZIP 炸弹未被拦：{msg}"
        wrote = sum(f.stat().st_size for f in (tmp_path / "blocked").rglob("*") if f.is_file())
        assert wrote == 0, f"炸弹被拦前已写入 {wrote} 字节"

        monkeypatch.delenv(ex.ENV_MAX_UNPACKED_BYTES)
        ok2, msg2 = extract_archive(arc, tmp_path / "ok", password="pw")
        assert ok2, f"放宽后加密 ZIP 应能解：{msg2}"

    def test_tar_zst_bomb_blocked(self, tmp_path, monkeypatch):
        """tar.zst 炸弹：7z 回退路径此前完全不检查（且 7z 读不出内层大小）。"""
        import io
        import tarfile

        zstandard = pytest.importorskip("zstandard")
        from backupstudio import extract as ex

        payload = tmp_path / "zeros.bin"
        payload.write_bytes(b"\x00" * (8 * 1024 * 1024))
        tar_path = tmp_path / "bomb.tar"
        with tarfile.open(tar_path, "w") as tf:
            data = payload.read_bytes()
            ti = tarfile.TarInfo("zeros.bin")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        arc = tmp_path / "bomb.tar.zst"
        with open(tar_path, "rb") as src, open(arc, "wb") as dst:
            zstandard.ZstdCompressor(level=9).copy_stream(src, dst)

        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, str(1024 * 1024))
        ok, msg = ex.extract_archive(arc, tmp_path / "blocked")
        assert ok is False, f"tar.zst 炸弹未被拦：{msg}"
        wrote = sum(f.stat().st_size for f in (tmp_path / "blocked").rglob("*") if f.is_file())
        assert wrote == 0, f"炸弹被拦前已写入 {wrote} 字节"

        monkeypatch.delenv(ex.ENV_MAX_UNPACKED_BYTES)
        ok2, msg2 = ex.extract_archive(arc, tmp_path / "ok")
        assert ok2, f"放宽后 tar.zst 应能解：{msg2}"
        got = list((tmp_path / "ok").rglob("*.bin"))
        assert got and got[0].stat().st_size == 8 * 1024 * 1024

    def test_tar_zst_sevenzip_fallback_bomb_blocked(self, tmp_path, monkeypatch):
        """把 zstandard 摘掉 → 强制走 7z ``e -so`` 流式回退，同样必须被拦。"""
        import io
        import tarfile

        zstandard = pytest.importorskip("zstandard")
        from backupstudio import extract as ex

        payload = tmp_path / "zeros.bin"
        payload.write_bytes(b"\x00" * (8 * 1024 * 1024))
        tar_path = tmp_path / "bomb.tar"
        with tarfile.open(tar_path, "w") as tf:
            data = payload.read_bytes()
            ti = tarfile.TarInfo("zeros.bin")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        arc = tmp_path / "bomb.tar.zst"
        with open(tar_path, "rb") as src, open(arc, "wb") as dst:
            zstandard.ZstdCompressor(level=9).copy_stream(src, dst)

        saved = sys.modules.pop("zstandard", None)
        try:
            monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, str(1024 * 1024))
            ok, msg = ex.extract_archive(arc, tmp_path / "blocked")
            assert ok is False, f"7z 回退路径炸弹未被拦：{msg}"
            wrote = sum(f.stat().st_size for f in (tmp_path / "blocked").rglob("*") if f.is_file())
            assert wrote == 0, f"炸弹被拦前已写入 {wrote} 字节"
        finally:
            if saved is not None:
                sys.modules["zstandard"] = saved


class TestBuiltinZipActuallyCompresses:
    """P1 回归：内置 ZIP 后端此前全是 STORED（ZipInfo.compress_type 覆盖 ZipFile 设置）。"""

    def test_zip_compressor_uses_deflate(self, tmp_path):
        from backupstudio.archives import ArchiveSpec, Progress, ZipCompressor

        src = tmp_path / "big.txt"
        src.write_bytes(b"compressible line of text for the builtin zip backend\n" * 100_000)
        dest = tmp_path / "out.zip"
        c = ZipCompressor(dest, ArchiveSpec(dest_path=dest, fmt="zip", level=5), Progress())
        c.add_file(src, "big.txt")
        c.close()

        with zipfile.ZipFile(dest) as zf:
            info = zf.getinfo("big.txt")
        assert info.compress_type == zipfile.ZIP_DEFLATED, "内置 ZIP 仍未压缩（compress_type=STORED）"
        ratio = dest.stat().st_size / src.stat().st_size
        assert ratio < 0.05, f"压缩率异常（{ratio:.3f}）：产物 {dest.stat().st_size}B"

    def test_zip_compressor_level_zero_is_stored(self, tmp_path):
        from backupstudio.archives import ArchiveSpec, Progress, ZipCompressor

        src = tmp_path / "big.txt"
        src.write_bytes(b"x" * (1024 * 1024))
        dest = tmp_path / "out0.zip"
        c = ZipCompressor(dest, ArchiveSpec(dest_path=dest, fmt="zip", level=0), Progress())
        c.add_file(src, "big.txt")
        c.close()
        with zipfile.ZipFile(dest) as zf:
            assert zf.getinfo("big.txt").compress_type == zipfile.ZIP_STORED

    def test_py7zr_zip_backends_use_deflate(self, tmp_path):
        """``py7zr_backend`` 的另外两处 ZipInfo 构造同样必须设 compress_type。"""
        pytest.importorskip("py7zr")
        from backupstudio.archives import ArchiveSpec, Progress
        from backupstudio.py7zr_backend import Py7zrCompressor, ZipFileCompressor

        src = tmp_path / "big.txt"
        src.write_bytes(b"compressible line of text for the builtin zip backend\n" * 100_000)
        for cls in (Py7zrCompressor, ZipFileCompressor):
            dest = tmp_path / f"{cls.__name__}.zip"
            spec = ArchiveSpec(dest_path=dest, fmt="zip", level=5)
            c = cls(dest, spec, Progress())
            c.add_file(src, "big.txt")
            c.close()
            with zipfile.ZipFile(dest) as zf:
                info = zf.getinfo("big.txt")
            assert info.compress_type == zipfile.ZIP_DEFLATED, f"{cls.__name__} 仍未压缩"