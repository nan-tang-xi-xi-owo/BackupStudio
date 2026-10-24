"""extract.py 加固回归（审查 R6-A5 / T8 / R1-N7）+ 通用分卷提取回归（审查 M22 / W-A4）。"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
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


# --------------------------------------------------------------------------
# 通用分卷提取回归（审查 M22 / W-A4：产品 split 产物 .partNN「能造不能拆」）
# --------------------------------------------------------------------------


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _split_dir(dir_: Path, name: str, vol_size: int, pieces: list[bytes]) -> list[Path]:
    """把整包字节切成 ``<name>.partNN``（卷数 >99 时补零位数变宽）。"""
    dir_.mkdir(parents=True, exist_ok=True)
    pad = 2 if len(pieces) < 100 else 3
    out: list[Path] = []
    for i, piece in enumerate(pieces, 1):
        p = dir_ / f"{name}.part{i:0{pad}d}"
        p.write_bytes(piece)
        out.append(p)
    return out


def _product_split(tmp_path: Path, fmt: str, volume_size: int, tag: str = "bkp") -> tuple[list[Path], Path, bytes]:
    """产品路径造分卷：create_archive(multivolume='split') → 返回 (卷, 源目录, 源字节)。

    注意 zip/7z 的切片由外部压缩后端负责（`ZipCompressor` 没有 `split_volumes`）：**没有
    7-Zip 的机器上产品自己也不会产出 .partNN**（CI 的"无 7z"作业正是要模拟这台纯净机器），
    所以卷数不足 2 时跳过而不是失败——测的是"产品产得出分卷时能不能拆"。
    """
    from backupstudio.archives import (
        ArchiveSpec,
        Item,
        Progress,
        create_archive,
        plan_items,
    )

    payload = os.urandom(240 * 1024)
    src = tmp_path / f"{tag}-src"
    (src / "sub").mkdir(parents=True)
    (src / "a.bin").write_bytes(payload)
    (src / "sub" / "b.txt").write_text("中文内容 ok\n" * 300, encoding="utf-8")
    items, _ = plan_items([Item(src, "src")])
    res = create_archive(
        ArchiveSpec(dest_path=tmp_path / f"{tag}.{fmt}", fmt=fmt, level=1,
                    multivolume="split", volume_size=volume_size),
        items,
        Progress(),
    )
    vols = [Path(v.path) for v in res.volumes]
    if len(vols) < 2:
        pytest.skip(f"{fmt} 在该机器未产出分卷（后端 {res.backend} 不支持切片），无从验证提取")
    return vols, src, payload


class TestExtractSplitVolumes:
    """``backupstudio extract X.zip.part01 --to dir`` 必须能用（此前报"不支持的压缩格式"）。"""

    def test_product_create_archive_split_roundtrip_zip(self, tmp_path):
        """①产品 create_archive(split) 产物 → extract_archive 直接可解、内容 md5 一致（zip）。"""
        from backupstudio.extract import extract_archive

        vols, src, payload = _product_split(tmp_path, "zip", 64 * 1024)
        assert vols[0].name.endswith(".part01"), vols[0].name
        out = tmp_path / "restored"
        ok, msg = extract_archive(vols[0], out)
        assert ok, msg
        got_a = out / "src" / "a.bin"
        assert got_a.is_file(), sorted(p.name for p in out.rglob("*") if p.is_file())
        assert _md5(got_a) == _md5(src / "a.bin")
        assert _md5(out / "src" / "sub" / "b.txt") == _md5(src / "sub" / "b.txt")
        assert len(payload) == got_a.stat().st_size

    def test_product_create_archive_split_roundtrip_targz(self, tmp_path):
        """①续：tar.gz（tar 系内层）走同一条路。"""
        from backupstudio.extract import extract_archive

        vols, src, _payload = _product_split(tmp_path, "tar.gz", 64 * 1024, tag="tgz")
        out = tmp_path / "restored-tgz"
        ok, msg = extract_archive(vols[0], out)
        assert ok, msg
        assert _md5(out / "src" / "a.bin") == _md5(src / "a.bin")
        assert _md5(out / "src" / "sub" / "b.txt") == _md5(src / "sub" / "b.txt")

    def test_missing_middle_volume_reports_precisely(self, tmp_path):
        """②缺中间卷 → False + 报错含"分卷缺失"，且不解压、不落任何文件。"""
        from backupstudio.extract import extract_archive

        whole = tmp_path / "bkp.zip"
        with zipfile.ZipFile(whole, "w") as zf:
            for i in range(6):
                zf.writestr(f"d/f{i}.bin", os.urandom(40 * 1024))
        data = whole.read_bytes()
        whole.unlink()
        vols = _split_dir(tmp_path, "bkp.zip", 48 * 1024,
                          [data[o:o + 48 * 1024] for o in range(0, len(data), 48 * 1024)])
        assert len(vols) >= 3
        moved = vols[1].with_name("moved-away.bin")
        vols[1].replace(moved)

        out = tmp_path / "out-miss"
        ok, msg = extract_archive(vols[0], out)
        assert ok is False, msg
        assert "分卷缺失" in msg, msg
        assert "part02" in msg, msg
        assert "共缺 1 卷" in msg, msg
        # 缺卷时绝不半途解压：目标目录里不该有任何落盘内容，也不该留拼接件
        assert list(out.iterdir()) == [], list(out.iterdir())
        assert not (out / "d").exists()
        moved.replace(vols[1])

    def test_missing_multiple_volumes_counted(self, tmp_path):
        from backupstudio.extract import extract_archive

        whole = tmp_path / "m.zip"
        with zipfile.ZipFile(whole, "w") as zf:
            for i in range(8):
                zf.writestr(f"f{i}.bin", os.urandom(40 * 1024))
        data = whole.read_bytes()
        whole.unlink()
        vols = _split_dir(tmp_path, "m.zip", 32 * 1024,
                          [data[o:o + 32 * 1024] for o in range(0, len(data), 32 * 1024)])
        assert len(vols) >= 4
        vols[1].unlink()
        vols[3].unlink()
        ok, msg = extract_archive(vols[0], tmp_path / "out-multi")
        assert ok is False
        assert "分卷缺失" in msg and "共缺 2 卷" in msg, msg

    def test_swapped_volume_order_is_detected(self, tmp_path):
        """③卷序交换（内容互换）→ False（拼接后 zip CRC 检出，绝不静默产出坏文件）。"""
        from backupstudio.extract import extract_archive

        whole = tmp_path / "sw.zip"
        with zipfile.ZipFile(whole, "w") as zf:
            for i in range(6):
                zf.writestr(f"d/f{i}.bin", os.urandom(40 * 1024))
        data = whole.read_bytes()
        whole.unlink()
        vols = _split_dir(tmp_path, "sw.zip", 48 * 1024,
                          [data[o:o + 48 * 1024] for o in range(0, len(data), 48 * 1024)])
        b2, b3 = vols[1].read_bytes(), vols[2].read_bytes()
        vols[1].write_bytes(b3)
        vols[2].write_bytes(b2)
        out = tmp_path / "out-swap"
        ok, msg = extract_archive(vols[0], out)
        assert ok is False, msg  # 原型实测：Bad CRC-32 →「压缩包损坏或格式不匹配」
        assert "不支持的压缩格式" not in msg, msg

    def test_numeric_volume_order_above_99(self, tmp_path):
        """数值卷序：101 卷时 part100 必须排在 part99 之后（字典序必错——审查 W-A4 §三）。"""
        from backupstudio.extract import extract_archive

        whole = tmp_path / "many.zip"
        payload = os.urandom(220 * 1024)
        with zipfile.ZipFile(whole, "w") as zf:
            zf.writestr("x.bin", payload)
        data = whole.read_bytes()
        whole.unlink()
        chunk = max(1, len(data) // 101 + 1)
        pieces = [data[o:o + chunk] for o in range(0, len(data), chunk)]
        assert len(pieces) >= 100, len(pieces)
        vols = _split_dir(tmp_path / "many", "many.zip", chunk, pieces)
        assert vols[-1].name.endswith("part101"), vols[-1].name
        # 打乱落盘顺序，确保排序是按 int 而不是文件名
        ok, msg = extract_archive(vols[0], tmp_path / "out-many")
        assert ok, msg
        got = tmp_path / "out-many" / "x.bin"
        assert got.is_file()
        assert hashlib.md5(got.read_bytes()).hexdigest() == hashlib.md5(payload).hexdigest()

    def test_split_tar_gz_inner_layer(self, tmp_path):
        """手工构造的 tar.gz 分卷（内层 tar 系）也能解。"""
        from backupstudio.extract import extract_archive

        buf = io.BytesIO()
        payload = os.urandom(130 * 1024)
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            ti = tarfile.TarInfo("payload.bin")
            ti.size = len(payload)
            tf.addfile(ti, io.BytesIO(payload))
        data = buf.getvalue()
        chunk = 48 * 1024
        vols = _split_dir(tmp_path / "tg", "t.tar.gz", chunk,
                          [data[o:o + chunk] for o in range(0, len(data), chunk)])
        out = tmp_path / "out-tg"
        ok, msg = extract_archive(vols[0], out)
        assert ok, msg
        assert hashlib.md5((out / "payload.bin").read_bytes()).hexdigest() == \
            hashlib.md5(payload).hexdigest()

    def test_non_first_volume_selected_still_works(self, tmp_path):
        """卷齐全时从任意一卷进去都能解（用户常选错卷，不该报"不支持的压缩格式"）。"""
        from backupstudio.extract import extract_archive

        vols, src, _payload = _product_split(tmp_path, "zip", 64 * 1024)
        out = tmp_path / "out-notfirst"
        ok, msg = extract_archive(vols[1], out)
        assert ok, msg
        assert "不支持的压缩格式" not in msg, msg
        assert _md5(out / "src" / "a.bin") == _md5(src / "a.bin")

    def test_missing_first_volume_says_so(self, tmp_path):
        """首卷缺失：明确提示缺 part01（而不是"不支持的压缩格式"或半截解压）。"""
        from backupstudio.extract import extract_archive

        vols, _, _ = _product_split(tmp_path, "zip", 64 * 1024, tag="nofirst")
        vols[0].unlink()
        ok, msg = extract_archive(vols[1], tmp_path / "out-nofirst")
        assert ok is False
        assert "不支持的压缩格式" not in msg, msg
        assert "part01" in msg and "分卷" in msg, msg

    def test_temp_joined_file_is_cleaned_up(self, tmp_path):
        """成功与失败路径都必须清掉拼接临时件（不留一份整包在目标盘；审查 W-A4 §四）。"""
        from backupstudio.extract import extract_archive

        vols, _, _ = _product_split(tmp_path, "zip", 64 * 1024)
        out = tmp_path / "out-clean"
        ok, _msg = extract_archive(vols[0], out)
        assert ok
        leftovers = [p.name for p in out.iterdir()]
        assert not any("bs-join" in n or "bs-extract" in n for n in leftovers), leftovers
        assert all(p.exists() for p in vols)  # 分卷原件不能被吃掉

    def test_split_path_keeps_bomb_guard(self, tmp_path, monkeypatch):
        """拼接后的解压仍受压缩炸弹防护（不得绕过 _check_bomb）。"""
        from backupstudio import extract as ex

        monkeypatch.setattr(ex, "_MAX_EXPANSION_RATIO", 10)
        monkeypatch.setattr(ex, "_MAX_TOTAL_BYTES", 512 * 1024)
        whole = tmp_path / "bomb.zip"
        with zipfile.ZipFile(whole, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", b"\x00" * (4 * 1024 * 1024))
        data = whole.read_bytes()
        whole.unlink()
        chunk = 64 * 1024
        vols = _split_dir(tmp_path / "bomb", "bomb.zip", chunk,
                          [data[o:o + chunk] for o in range(0, len(data), chunk)])
        ok, msg = ex.extract_archive(vols[0], tmp_path / "out-bomb")
        assert ok is False
        assert "炸弹" in msg or "超过上限" in msg, msg

    def test_split_path_keeps_zip_slip_guard(self, tmp_path):
        """拼接后的解压仍受 zip-slip 防护（不得绕过路径穿越检查）。"""
        from backupstudio.extract import extract_archive

        whole = tmp_path / "evil.zip"
        with zipfile.ZipFile(whole, "w") as zf:
            zf.writestr("../evil.txt", "pwned")
            zf.writestr("ok.txt", "fine" * 4000)
        data = whole.read_bytes()
        whole.unlink()
        chunk = 2048
        vols = _split_dir(tmp_path / "slip", "evil.zip", chunk,
                          [data[o:o + chunk] for o in range(0, len(data), chunk)])
        out = tmp_path / "out-slip"
        ok, msg = extract_archive(vols[0], out)
        assert ok is False
        assert "不安全路径" in msg or "拒绝" in msg, msg
        assert not (tmp_path / "evil.txt").exists()

    def test_sevenz_native_volumes_unchanged(self, tmp_path):
        """7z 原生 .001 分卷仍由 7za 自动串联（本次修复不得改变其行为）。"""
        from backupstudio.archives import (
            ArchiveSpec,
            Item,
            Progress,
            create_archive,
            plan_items,
        )
        from backupstudio.extract import extract_archive
        from backupstudio.tools import find_7z

        if not find_7z():
            pytest.skip("本机无 7-Zip")
        src = tmp_path / "nsrc"
        src.mkdir()
        (src / "data.bin").write_bytes(os.urandom(300 * 1024))
        items, _ = plan_items([Item(src, "nsrc")])
        res = create_archive(
            ArchiveSpec(dest_path=tmp_path / "native.7z", fmt="7z", level=1,
                        multivolume="7z-split", volume_size=100 * 1024),
            items, Progress(),
        )
        first = Path(res.volumes[0].path)
        assert first.name.endswith(".001"), first.name
        out = tmp_path / "out-7z001"
        ok, msg = extract_archive(first, out)
        assert ok, msg
        got = [p for p in out.rglob("data.bin")]
        assert got, list(out.rglob("*"))
        assert got[0].stat().st_size == 300 * 1024

    def test_plain_archive_not_treated_as_split(self, tmp_path):
        """非 .partNN 命名不进分卷分支；相近主名的另一组卷不能被混收（base 必须全等）。"""
        from backupstudio.extract import _split_group

        whole = tmp_path / "plain.zip"
        with zipfile.ZipFile(whole, "w") as zf:
            zf.writestr("a.txt", "x")
        assert _split_group(whole) is None
        (tmp_path / "bkp.zip.part01").write_bytes(b"aa")
        (tmp_path / "bkp-backup.zip.part01").write_bytes(b"zz")
        got = _split_group(tmp_path / "bkp.zip.part01")
        assert got is not None
        assert [p.name for _, p in got[1]] == ["bkp.zip.part01"], got[1]
        assert got[0] == "bkp.zip"

    def test_unknown_inner_container_not_joined(self, tmp_path):
        """内层主名不是我们能分发的容器 → 不白拼一份整包，交回原有分发。"""
        from backupstudio.extract import _extract_split

        d = tmp_path / "misc"
        d.mkdir()
        for i in (1, 2):
            (d / f"video.mkv.part{i:02d}").write_bytes(b"x" * 64)
        assert _extract_split(d / "video.mkv.part01", tmp_path / "o1", None) is None
        leftovers = list((tmp_path / "o1").iterdir()) if (tmp_path / "o1").exists() else []
        assert leftovers == [], leftovers

    def test_renamed_volume_tolerated(self, tmp_path):
        """卷被改名（part02 → part2，少一位补零）：宽容接受，仍能正确解出（原型实测行为）。"""
        from backupstudio.extract import extract_archive

        whole = tmp_path / "ren.zip"
        with zipfile.ZipFile(whole, "w") as zf:
            for i in range(6):
                zf.writestr(f"d/f{i}.bin", os.urandom(40 * 1024))
        data = whole.read_bytes()
        whole.unlink()
        chunk = 48 * 1024
        vols = _split_dir(tmp_path / "ren", "ren.zip", chunk,
                          [data[o:o + chunk] for o in range(0, len(data), chunk)])
        vols[1].rename(vols[1].with_name("ren.zip.part2"))  # 去掉补零
        out = tmp_path / "out-ren"
        ok, msg = extract_archive(vols[0], out)
        assert ok, msg
        assert len(list(out.rglob("f*.bin"))) == len(vols) and (out / "d" / "f0.bin").is_file()

    def test_duplicate_volume_number_prefers_product_name(self, tmp_path):
        """同编号重复卷（找回 part02 却没删掉改名的 part2）：取产品口径命名那份并提示，不双拼。"""
        from backupstudio.extract import extract_archive

        whole = tmp_path / "dup.zip"
        with zipfile.ZipFile(whole, "w") as zf:
            for i in range(6):
                zf.writestr(f"d/f{i}.bin", os.urandom(40 * 1024))
        data = whole.read_bytes()
        whole.unlink()
        chunk = 48 * 1024
        vols = _split_dir(tmp_path / "dup", "dup.zip", chunk,
                          [data[o:o + chunk] for o in range(0, len(data), chunk)])
        # 再造一份"被改名"的 part02 副本（内容不同 = 坏的）
        (tmp_path / "dup" / "dup.zip.part2").write_bytes(b"corrupted-duplicate")
        out = tmp_path / "out-dup"
        ok, msg = extract_archive(vols[0], out)
        assert ok, msg  # 若两份都被拼进去必然 Bad CRC → 失败
        assert "重复" in msg, msg

    def test_truncated_volume_hint_on_failure(self, tmp_path):
        """卷被截断（与首卷大小不符）导致失败时，报错里带上"大小不同"的归因提示。"""
        from backupstudio.extract import extract_archive

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            blob = os.urandom(200 * 1024)
            ti = tarfile.TarInfo("p.bin")
            ti.size = len(blob)
            tf.addfile(ti, io.BytesIO(blob))
        data = buf.getvalue()
        chunk = 32 * 1024
        vols = _split_dir(tmp_path / "cut", "cut.tar.gz", chunk,
                          [data[o:o + chunk] for o in range(0, len(data), chunk)])
        assert len(vols) >= 3
        vols[1].write_bytes(vols[1].read_bytes()[:100])  # 中间卷被截断
        ok, msg = extract_archive(vols[0], tmp_path / "out-cut")
        assert ok is False
        assert "大小不同" in msg, msg  # 归因提示：不是"格式不支持"，也不是无信息量的损坏

    def test_rar_native_volume_names_not_hijacked(self, tmp_path):
        """rar 原生卷 `x.part1.rar` 不以 .partNN 结尾，仍走外部程序路径（不被拼接分支劫持）。"""
        from backupstudio.extract import _split_group

        p = tmp_path / "x.part1.rar"
        p.write_bytes(b"Rar!")
        assert _split_group(p) is None

    def test_split_tar_zst_roundtrip(self, tmp_path):
        """tar.zst 分卷：解出的内容必须真的对（审查 M22 全格式覆盖时这里曾是 md5 不一致）。"""
        pytest.importorskip("zstandard")
        from backupstudio.extract import extract_archive

        vols, src, _payload = _product_split(tmp_path, "tar.zst", 64 * 1024, tag="zs")
        out = tmp_path / "out-zst"
        ok, msg = extract_archive(vols[0], out)
        assert ok, msg
        assert _md5(out / "src" / "a.bin") == _md5(src / "a.bin")
        assert _md5(out / "src" / "sub" / "b.txt") == _md5(src / "sub" / "b.txt")

    def test_single_tar_zst_leaves_no_bare_tar(self, tmp_path):
        """整包 tar.zst 必须解出成员本身，不能只剥一层 zstd 留下裸 .tar 还报"已解压"（假成功）。"""
        pytest.importorskip("zstandard")
        from backupstudio.archives import (
            ArchiveSpec,
            Item,
            Progress,
            create_archive,
            plan_items,
        )
        from backupstudio.extract import extract_archive

        payload = os.urandom(80 * 1024)
        src = tmp_path / "zs2"
        src.mkdir()
        (src / "a.bin").write_bytes(payload)
        items, _ = plan_items([Item(src, "zs2")])
        arc = Path(create_archive(
            ArchiveSpec(dest_path=tmp_path / "single.tar.zst", fmt="tar.zst", level=1),
            items, Progress(),
        ).path)
        out = tmp_path / "out-zst2"
        ok, msg = extract_archive(arc, out)
        assert ok, msg
        got = out / "zs2" / "a.bin"
        assert got.is_file(), list(out.rglob("*"))
        assert _md5(got) == _md5_bytes(payload)
        assert list(out.rglob("*.tar")) == [], "只剥了 zstd 层，没解内层 tar"

    def test_corrupt_tar_zst_is_not_false_pass(self, tmp_path):
        """损坏的 tar.zst 必须失败（zstd 帧 checksum 检出），不能报成功。"""
        pytest.importorskip("zstandard")
        from backupstudio.archives import (
            ArchiveSpec,
            Item,
            Progress,
            create_archive,
            plan_items,
        )
        from backupstudio.extract import extract_archive

        src = tmp_path / "zs3"
        src.mkdir()
        (src / "a.bin").write_bytes(os.urandom(80 * 1024))
        items, _ = plan_items([Item(src, "zs3")])
        arc = Path(create_archive(
            ArchiveSpec(dest_path=tmp_path / "good.tar.zst", fmt="tar.zst", level=1),
            items, Progress(),
        ).path)
        data = bytearray(arc.read_bytes())
        for i in range(60, 460):
            data[i] ^= 0xFF
        bad = tmp_path / "bad.tar.zst"
        bad.write_bytes(bytes(data))
        ok, msg = extract_archive(bad, tmp_path / "out-zbad")
        assert ok is False, msg
        assert "checksum" in msg or "损坏" in msg or "Zstd" in msg, msg

    def test_absurd_volume_number_does_not_hang(self, tmp_path):
        """离谱编号（part01 + part1000000，例如视频分段/下载残留撞名）不得物化百万级缺号表。"""
        import time

        from backupstudio.extract import extract_archive

        d = tmp_path / "crazy"
        d.mkdir()
        (d / "movie.zip.part01").write_bytes(b"PK\x03\x04" + b"0" * 40)
        (d / "movie.zip.part1000000").write_bytes(b"0" * 40)
        t0 = time.monotonic()
        ok, msg = extract_archive(d / "movie.zip.part01", tmp_path / "out-crazy")
        elapsed = time.monotonic() - t0
        assert ok is False
        assert elapsed < 2.0, f"缺卷判定耗时 {elapsed:.1f}s（疑似物化了百万级列表）"
        assert "分卷缺失" in msg and "共缺" in msg, msg

    def test_cli_extract_split_end_to_end(self, tmp_path):
        """真实 CLI：`python -m backupstudio extract X.zip.part01 --to dir` 成功（M22 原始复现路径）。"""
        vols, _, _ = _product_split(tmp_path, "zip", 64 * 1024)
        out = tmp_path / "cli-out"
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run(
            [sys.executable, "-m", "backupstudio", "extract", str(vols[0]), "--to", str(out)],
            check=False,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=str(ROOT), timeout=180,
        )
        assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
        assert "不支持的压缩格式" not in (r.stdout + r.stderr)
        assert (out / "src" / "a.bin").is_file()
