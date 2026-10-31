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
        # 新文案：不再说"无法预读…可设环境变量关闭上限"（那个开关结构上走不通），
        # 改为说清"无法读取包信息"并给出真实排查方向
        text = str(ei.value)
        assert "无法读取" in text or "无法预读" in text, text


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


# ── S1 补测（1.1.7 从 main 合并过来）────────────────────────────
# 我的 extract.py 早已有 `return total if seen else None` 的 S1 修复，
# 但缺这 3 条回归测试保护 —— 合并 main 的主要收益就在这里。

    # main 上的辅助方法：造一个「不报告内容长度」的容器用于 S1 测试
    @staticmethod
    def _make_no_size_archive(path, size=64 * 1024 * 1024):
        zstd = pytest.importorskip("zstandard", reason="需要 zstandard 构造样本")
        import zstandard as z

        path.write_bytes(z.ZstdCompressor().compress(b"\x00" * size))
        return path

    def test_declared_size_returns_none_not_zero(self, tmp_path):
        from backupstudio import extract as ex
        from backupstudio.tools import find_7z

        exe = find_7z()
        if not exe:
            pytest.skip("需要 7-Zip")
        arc = self._make_no_size_archive(tmp_path / "bomb.7z")
        # None = "读不出"，调用方据此拒绝；返回 0 就会被当成"声明 0 字节"放行
        assert ex._declared_unpacked_7z(exe, arc, None) is None


    def test_extract_refuses_no_size_container(self, tmp_path, monkeypatch):
        from backupstudio import extract as ex

        if not ex.find_7z():
            pytest.skip("需要 7-Zip")
        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, str(1024 * 1024))
        arc = self._make_no_size_archive(tmp_path / "bomb.7z")
        out = tmp_path / "out"
        ok, msg = ex.extract_archive(arc, out)
        assert ok is False, f"无长度容器被放行：{msg}"
        # 本分支文案比 main 更具体（说明原因+可能情形），行为一致：都拒绝。
        assert "无法读取" in msg or "无法预读" in msg or "炸弹" in msg, msg
        written = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) if out.exists() else 0
        assert written == 0, f"拒绝前已写入 {written} 字节"


    def test_zero_size_archive_still_allowed(self, tmp_path):
        """反向守卫：真正的 0 字节内容（纯目录包）必须继续放行。

        ``Size = 0`` 是能解析出来的合法值，不能和"解析不出"混为一谈。
        """
        from backupstudio import extract as ex
        from backupstudio.tools import find_7z

        exe = find_7z()
        if not exe:
            pytest.skip("需要 7-Zip")
        src = tmp_path / "srcdir"
        (src / "e1").mkdir(parents=True)
        (src / "e2").mkdir()
        arc = tmp_path / "dironly.7z"
        import subprocess

        subprocess.run([exe, "a", "-t7z", str(arc), str(src)], capture_output=True, check=True)
        assert ex._declared_unpacked_7z(exe, arc, None) == 0
        assert ex._count_entries_7z(exe, arc, None) is not None
        ok, msg = ex.extract_archive(arc, tmp_path / "out")
        assert ok, f"纯目录 7z 被误拒：{msg}"

