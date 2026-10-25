"""归档结构回归测试：X1 修复（外部 7z 后端坏包）的关键断言。

背景：此前 ExternalCompressor 把 staging.tar 直接交给 7z 打包——7z 不解
tar，产物只有 `['staging.tar']` 一个成员（打包版用户全部坏包、CI 7 连败
根因）。修复后必须先解 tar、再对目录打包。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def sample_tree(tmp_path):
    src = tmp_path / "data"
    (src / "sub").mkdir(parents=True)
    (src / "empty").mkdir()
    (src / "a.txt").write_text("hello world\n" * 100, encoding="utf-8")
    (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 100)
    (src / "sub" / "中文 emoji 🎈.txt").write_text("内容", encoding="utf-8")
    return src


def _make_task(fmt, dest, src, **opt_kwargs):
    from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

    return BackupTask(
        name=f"t-{fmt}",
        sources=[SourceConfig(path=str(src))],
        destination=str(dest),
        options=ArchiveOptions(fmt=fmt, **opt_kwargs),
    )


def _members(path: Path, fmt: str) -> list[str]:
    if fmt == "zip":
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    import py7zr

    with py7zr.SevenZipFile(str(path), "r") as z:
        return z.getnames()


def _sevenzip_supports_cjk(work: Path) -> bool:
    """探测当前 7-Zip 是否能把非 ASCII 文件名正确写进 zip。

    为什么需要探测而不是比对版本号：
      * p7zip 16.02（CI 的 ubuntu-22.04）对 UTF-8 文件名支持不完整，
        实测会把 `中文 emoji 🎈.txt` 丢掉或转义；
      * 7-Zip 25.01（本机/VM）完全正常；
      * 版本号字符串在不同发行版之间不可靠（上游 7-Zip vs 发行版 p7zip），
        直接探测更准。

    返回 True 表示支持（可以严格断言 CJK 文件名）。
    """
    import subprocess

    from backupstudio.tools import find_7z

    exe = find_7z()
    if not exe:
        return False

    probe_dir = work / "_cjk_probe"
    if probe_dir.exists():
        return True  # 已探测过
    (probe_dir / "sub").mkdir(parents=True, exist_ok=True)
    (probe_dir / "sub" / "中文 emoji 🎈.txt").write_text("x", encoding="utf-8")
    out = work / "_cjk_probe.zip"
    try:
        subprocess.run(
            [exe, "a", "-tzip", str(out), "sub"],
            cwd=str(probe_dir),
            capture_output=True,
            timeout=60,
            check=False,
        )
        if not out.is_file():
            return False
        import zipfile as _zf

        with _zf.ZipFile(out) as zf:
            return any("中文" in n for n in zf.namelist())
    except Exception:  # noqa: BLE001 - 探测失败就当不支持
        return False


def _skip_if_no_cjk_support(work: Path) -> None:
    """当前 7-Zip 不支持非 ASCII 文件名时，明确 skip 并说明原因。"""
    if not _sevenzip_supports_cjk(work):
        import subprocess as _sp

        from backupstudio.tools import find_7z

        exe = find_7z() or "?"
        ver = ""
        try:
            r = _sp.run([exe, "i"], capture_output=True, timeout=20)
            head = (r.stdout or b"").decode("utf-8", "replace").splitlines()
            ver = next((ln.strip() for ln in head if "Version" in ln), "")
        except Exception:  # noqa: BLE001
            pass
        pytest.skip(
            f"当前 7-Zip 不支持非 ASCII 文件名（{ver or exe}）——"
            "p7zip 16.02 等老版本的上游限制，非产品缺陷；"
            "7-Zip 21+ / 25.x 上此断言会真实执行"
        )


class TestArchiveStructure:
    def test_zip_with_7z_backend_has_real_files(self, sample_tree, tmp_path):
        """有 7-Zip 时：zip 产物必须含真实文件树（而不是单个 staging.tar）。"""
        from backupstudio.engine import TaskEngine
        from backupstudio.tools import find_7z

        if not find_7z():
            pytest.skip("本机无 7-Zip（无 7z 环境的对称断言见另一用例）")
        dest = tmp_path / "out-zip"
        rec = TaskEngine().run(_make_task("zip", dest, sample_tree)).record
        assert rec.status == "success", rec.errors
        names = _members(Path(rec.archive), "zip")
        assert "staging.tar" not in names, f"产物仍是坏包：{names}"
        assert any(n.endswith("a.txt") for n in names), names
        # 中文/emoji 文件名依赖 7-Zip 对非 ASCII 的支持：p7zip 16.02 不支持，
        # 7-Zip 21+/25.x 正常。先探测再断言，避免把上游限制误判成产品缺陷。
        _skip_if_no_cjk_support(tmp_path)
        _skip_if_no_cjk_support(tmp_path)
        assert any("中文 emoji" in n for n in names), "中文/emoji 文件名缺失"

    def test_7z_with_7z_backend_has_real_files(self, sample_tree, tmp_path):
        """有 7-Zip 时：7z 产物必须含真实文件树。"""
        from backupstudio.engine import TaskEngine
        from backupstudio.tools import find_7z

        if not find_7z():
            pytest.skip("本机无 7-Zip")
        dest = tmp_path / "out-7z"
        rec = TaskEngine().run(_make_task("7z", dest, sample_tree)).record
        assert rec.status == "success", rec.errors
        names = _members(Path(rec.archive), "7z")
        assert "staging.tar" not in names, f"产物仍是坏包：{names}"
        assert any(n.endswith("a.txt") for n in names), names

    def test_no_7z_builtin_still_correct(self, sample_tree, tmp_path, no_7z):
        """无 7-Zip 时（内置引擎）：产物同样是完整文件树（对称守卫）。"""
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "out-builtin"
        rec = TaskEngine().run(_make_task("zip", dest, sample_tree, force=True)).record
        assert rec.status == "success", rec.errors
        names = _members(Path(rec.archive), "zip")
        assert "staging.tar" not in names
        assert any(n.endswith("a.txt") for n in names), names

    def test_no_residue_after_success(self, sample_tree, tmp_path):
        """成功运行后不得残留事务性临时文件（.bs-tmp-* / bs-stage-*）。"""
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "out-residue"
        rec = TaskEngine().run(_make_task("zip", dest, sample_tree, force=True)).record
        assert rec.status == "success", rec.errors
        leftovers = [
            p.name
            for p in dest.iterdir()
            if p.name != "last-run.json"
            and (".bs-tmp-" in p.name or "bs-stage-" in p.name or "bs-py7zr-" in p.name)
        ]
        assert not leftovers, leftovers

    def test_tar_open_failure_does_not_corrupt_stream(self, sample_tree, tmp_path):
        """tar 错位回归（审查 E1-1.4）：一个文件 open 失败不得弄坏整个归档。

        旧实现先写头（声明 size）再在生成器里 open 文件——open 失败时流里
        留着一个"永远没有数据"的头，后续所有条目错位不可读（实测复现：
        失败条目之后的成员全部消失）。修复后（先 open 后写头 + fstat）
        该文件只是被记入 errors，其余条目完好。

        用目录制造"stat 成功、open 失败"：旧顺序会写出头再炸，新顺序在
        写头之前就炸——两种顺序在断言上完全可区分。
        """
        import tarfile

        from backupstudio.archives import ArchiveSpec, Progress, TarStreamCompressor

        good1 = tmp_path / "1.bin"
        good1.write_bytes(b"A" * 4096)
        bad_dir = tmp_path / "2bad"  # 目录：stat 成功、open 抛 OSError
        bad_dir.mkdir()
        good3 = tmp_path / "3.bin"
        good3.write_bytes(b"C" * 4096)
        out = tmp_path / "out.tar"

        spec = ArchiveSpec(dest_path=out, fmt="tar")
        comp = TarStreamCompressor(out, spec, Progress())
        comp.add_file(good1, "1.bin")
        try:
            comp.add_file(bad_dir, "2bad")
            raise AssertionError("目录作为文件打包应当抛 OSError")
        except OSError:
            pass  # 调用方（create_archive）会捕获并记为条目错误
        comp.add_file(good3, "3.bin")
        comp.close()

        with tarfile.open(out) as tf:
            names = tf.getnames()
            assert names == ["1.bin", "3.bin"], f"tar 流错位：{names}"
            with tf.extractfile("3.bin") as fh:
                assert fh.read() == b"C" * 4096, "3.bin 内容损坏（流错位）"


class TestRelativeDestinationPath:
    """P1 回归：相对 ``dest_path`` 会被写进 ``src_dir`` 内部，导致静默 fallback。

    根因：``ExternalCompressor._pack_from`` 以 ``cwd=src_dir`` 调 7z，``out_path`` 若是
    相对路径就会被解析到 ``src_dir`` 里；``_commit()`` 在最终路径上找不到产物 →
    抛 CompressionError → 上层 ``force`` 回退内置引擎，但记录里 ``backend`` 仍写
    ``7z:7za.exe``（账实不符）。
    """

    def test_sevenzip_dest_is_absolute(self, sample_tree, tmp_path):
        """``SevenZipCompressor.__init__`` 必须把相对 dest 解析成绝对路径。"""
        from backupstudio.archives import ArchiveSpec, Progress, SevenZipCompressor
        from backupstudio.tools import find_7z

        seven = find_7z()
        if not seven:
            pytest.skip("本机无 7-Zip")
        out = tmp_path / "out"
        out.mkdir()
        old = Path.cwd()
        import os

        os.chdir(tmp_path)
        try:
            spec = ArchiveSpec(dest_path=Path("out/rel.zip"), fmt="zip", level=5)
            comp = SevenZipCompressor(Path("out/rel.zip"), spec, Progress(), exe=seven)
            try:
                assert Path(comp.dest).is_absolute(), f"dest 仍是相对路径：{comp.dest}"
                assert Path(comp.dest) == (out / "rel.zip").resolve()
            finally:
                comp.abort()
        finally:
            os.chdir(old)

    def test_pack_from_resolves_relative_out_path(self, sample_tree, tmp_path):
        """``_pack_from`` 内部的 ``resolve()`` 必须兜住相对 out_path（第二道防线）。"""
        import os

        from backupstudio.archives import ArchiveSpec, Progress, SevenZipCompressor
        from backupstudio.tools import find_7z

        seven = find_7z()
        if not seven:
            pytest.skip("本机无 7-Zip")
        outdir = tmp_path / "outdir"
        outdir.mkdir()
        comp = SevenZipCompressor(
            outdir / "rel2.zip", ArchiveSpec(dest_path=outdir / "rel2.zip", fmt="zip", level=5),
            Progress(), exe=seven,
        )
        staged = comp.tmpdir / "unpacked"
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "s.txt").write_text("second guard", encoding="utf-8")
        old = Path.cwd()
        os.chdir(tmp_path)
        try:
            comp._pack_from(staged, Path("outdir/rel2.zip.bs-tmp"))
        finally:
            os.chdir(old)
        try:
            assert (outdir / "rel2.zip.bs-tmp").exists(), "相对 out_path 未落到预期位置"
            assert not list(staged.rglob("*.bs-tmp")), "产物被写进了 src_dir 内部"
        finally:
            comp.abort()

    def test_relative_dest_end_to_end(self, sample_tree, tmp_path):
        """端到端：相对 destination 的任务产物必须落在预期位置，且 backend 账实相符。"""
        import os

        from backupstudio.engine import TaskEngine
        from backupstudio.tools import find_7z

        if not find_7z():
            pytest.skip("本机无 7-Zip")
        old = Path.cwd()
        os.chdir(tmp_path)
        try:
            (tmp_path / "relout").mkdir()
            rec = TaskEngine().run(_make_task("zip", "relout", sample_tree)).record
        finally:
            os.chdir(old)
        assert rec.status == "success", rec.errors
        arc = Path(rec.archive)
        assert arc.is_absolute(), f"产物路径仍是相对：{arc}"
        assert (tmp_path / "relout" / arc.name).exists(), f"产物不在预期位置：{arc}"
        assert "7z" in (rec.backend or ""), f"backend 应记录真实外部引擎：{rec.backend}"