"""manifest（备份身份层）测试：提交标记、卷组归组、迁移红线。

覆盖审查 E1/E2/E5 的关键断言：
* 成功备份写 committed 清单；失败写 partial；
* 保留策略按组淘汰（分卷不拆散）；
* **迁移红线**：无清单旧包默认不参与自动淘汰（升级不误删）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def src_tree(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    (src / "a.txt").write_text("hello\n" * 200, encoding="utf-8")
    return src


def _task(dest, src, **opt_kwargs):
    from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

    return BackupTask(
        name="mt",
        sources=[SourceConfig(path=str(src))],
        destination=str(dest),
        options=ArchiveOptions(force=True, **opt_kwargs),
    )


class TestManifestModule:
    def test_roundtrip(self, tmp_path):
        from backupstudio.manifest import Manifest, read_manifest, write_manifest

        dest = tmp_path / "d"
        dest.mkdir()
        m = Manifest(stem="x_20260101", task_id="t1", fmt="zip", volumes=["x_20260101.zip"], size_bytes=10)
        write_manifest(dest, m)
        got = read_manifest(tmp_path / "d" / ".backupstudio" / "manifests" / "x_20260101.json")
        assert got is not None
        assert got.stem == "x_20260101"
        assert got.volumes == ["x_20260101.zip"]

    def test_corrupt_manifest_returns_none(self, tmp_path):
        from backupstudio.manifest import read_manifest

        p = tmp_path / "bad.json"
        p.write_text("{ not json", encoding="utf-8")
        assert read_manifest(p) is None


class TestManifestIntegration:
    def test_success_writes_committed(self, src_tree, tmp_path):
        from backupstudio.engine import TaskEngine
        from backupstudio.manifest import load_manifests

        dest = tmp_path / "out"
        rec = TaskEngine().run(_task(dest, src_tree, fmt="zip")).record
        assert rec.status == "success"
        manifests = load_manifests(dest_path := dest)
        assert len(manifests) == 1
        m = next(iter(manifests.values()))
        assert m.state == "committed"
        assert m.volumes and (dest_path / m.volumes[0]).exists()
        assert m.verified == "passed"

    def test_retention_groups_by_manifest_keep_last(self, src_tree, tmp_path):
        """keep_last=2 连跑 3 次：保留最新 2 组（manifest 组），删最旧组。"""
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "ret"
        engine = TaskEngine()
        for i in range(3):
            t = _task(dest, src_tree, fmt="zip")
            t.name = f"t{i}"  # 不同 stem
            t.retention = RetentionPolicy(enabled=True, keep_last=2)
            rec = engine.run(t).record
            assert rec.status == "success", rec.errors
            time.sleep(1.05)
        zips = sorted(p.name for p in dest.glob("*.zip"))
        assert len(zips) == 2, zips

    def test_split_group_survives_retention_as_unit(self, tmp_path):
        """分卷组按组淘汰：不拆散、不残留（C11 回归）。"""
        from backupstudio.config import ArchiveOptions, RetentionPolicy
        from backupstudio.engine import TaskEngine

        src = tmp_path / "data"
        src.mkdir()
        (src / "a.bin").write_bytes(os.urandom(50 * 1024))  # 50KB 不可压 → 必定分卷
        dest = tmp_path / "split"
        engine = TaskEngine()
        for i in range(2):
            t = _task(dest, src,
                      fmt="tar.gz", multivolume="split", volume_value=20, volume_unit="KB")
            t.name = f"s{i}"
            t.retention = RetentionPolicy(enabled=True, keep_last=1)
            rec = engine.run(t).record
            assert rec.status == "success", rec.errors
            time.sleep(1.05)
        parts = sorted(p.name for p in dest.glob("*.part*"))
        assert parts, "未产出分卷（测试前提失败）"
        # 旧组（s0）全灭（不残留任何卷），新组（s1）完整
        assert not any(p.startswith("s0") for p in parts), parts
        assert any(p.startswith("s1") for p in parts), parts

    def test_legacy_archives_not_auto_deleted(self, src_tree, tmp_path):
        """迁移红线（E5）：无清单旧包默认不参与自动淘汰——升级不误删。"""
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "legacy"
        dest.mkdir()
        # 预置 3 个"旧包"（无清单，模拟升级前产物；mtime 很旧）
        old = time.time() - 40 * 86400
        legacy = []
        for i in range(3):
            p = dest / f"old_{i}.zip"
            p.write_bytes(b"PK\x03\x04" + b"x" * 100)
            os.utime(p, (old + i, old + i))
            legacy.append(p)
        # 跑一次新备份（keep_last=1）：旧包应原样保留，只有新清单组参与
        t = _task(dest, src_tree, fmt="zip")
        t.retention = RetentionPolicy(enabled=True, keep_last=1)
        rec = TaskEngine().run(t).record
        assert rec.status == "success", rec.errors
        remaining = sorted(p.name for p in dest.glob("old_*.zip"))
        assert remaining == ["old_0.zip", "old_1.zip", "old_2.zip"], (
            f"旧包被自动删除了！（E5 红线破坏）剩余：{remaining}"
        )

    def test_legacy_can_be_adopted_explicitly(self, src_tree, tmp_path):
        """显式接管（include_legacy=True）后旧包才参与淘汰。"""
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine, apply_retention

        dest = tmp_path / "adopt"
        dest.mkdir()
        old = time.time() - 40 * 86400
        for i in range(3):
            p = dest / f"old_{i}.zip"
            p.write_bytes(b"PK\x03\x04" + b"x" * 100)
            os.utime(p, (old + i, old + i))
        t = _task(dest, src_tree, fmt="zip")
        t.retention = RetentionPolicy(enabled=True, keep_last=2)
        handled = apply_retention(t, include_legacy=True)
        # 3 个旧包 keep_last=2 → 删 1 个
        assert len(handled) == 1, handled

    def test_plan_adoption_reports_only(self, tmp_path):
        """plan_adoption 只报告不动文件。"""
        from backupstudio.manifest import plan_adoption

        dest = tmp_path / "report"
        dest.mkdir()
        (dest / "user_manual.zip").write_bytes(b"PK\x03\x04" + b"y" * 50)
        reports = plan_adoption(dest, ".zip")
        assert len(reports) == 1
        assert reports[0].stem == "user_manual.zip"
        assert (dest / "user_manual.zip").exists(), "plan_adoption 不得删除任何文件"
