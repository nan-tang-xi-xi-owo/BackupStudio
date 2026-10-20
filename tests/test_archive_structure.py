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
