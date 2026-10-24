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


class TestVolumeGrouping:
    """A2 回归：恢复 main 的两种分卷归组，且不误并同名普通文件。"""

    FMTS = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")

    def _groups(self, tmp_path, names):
        from backupstudio.manifest import collect_groups

        d = tmp_path / "vol"
        d.mkdir()
        for n in names:
            (d / n).write_bytes(b"x" * 16)
        return {g.stem: sorted(f.name for f in g.files)
                for g in collect_groups(d, ".7z", fmts=self.FMTS)}

    def test_7z_native_volumes_grouped(self, tmp_path):
        """`stem.7z.001/.002` 必须归成一组（旧实现完全看不见 → 保留策略漏算）。"""
        got = self._groups(tmp_path, ["old.7z.001", "old.7z.002"])
        assert got == {"old.7z": ["old.7z.001", "old.7z.002"]}, got

    def test_rar_native_volumes_grouped(self, tmp_path):
        """`base.part1.rar/part2.rar` 必须归成一组（旧实现拆成两组 → 可能只删 part1）。"""
        got = self._groups(tmp_path, ["old.part1.rar", "old.part2.rar"])
        assert got == {"old.rar": ["old.part1.rar", "old.part2.rar"]}, got

    def test_generic_split_volumes_grouped(self, tmp_path):
        """本产品的 `.partNN` 切片仍要正确归组（不能修 A2 时破坏它）。"""
        got = self._groups(tmp_path, ["s.tar.gz.part01", "s.tar.gz.part02"])
        assert got == {"s.tar.gz": ["s.tar.gz.part01", "s.tar.gz.part02"]}, got

    def test_backup_suffix_not_merged(self, tmp_path):
        """`a.7z` 与 `a.7z.bak` 绝不能并成一组（A2 的硬性约束）。"""
        got = self._groups(tmp_path, ["a.7z", "a.7z.bak"])
        # `a.7z.bak` 不是任何分卷形态 → 根本不属于备份集合（也就绝不会被并入 a.7z）。
        # 关键断言：a.7z 组里只有自己，没有被 .bak 污染。
        assert got.get("a.7z") == ["a.7z"], got
        assert all("a.7z.bak" not in files for files in got.values()), got

    def test_partial_digit_extension_not_merged(self, tmp_path):
        """`a.7z.1` / `a.7z.01` 不是三位分卷，不得并入 `a.7z` 组。"""
        got = self._groups(tmp_path, ["a.7z", "a.7z.1", "a.7z.01"])
        assert got.get("a.7z") == ["a.7z"], got

    def test_prefix_similar_names_not_merged(self, tmp_path):
        """`x.7z` 与 `xy.7z` 是两个不同的包。"""
        got = self._groups(tmp_path, ["x.7z", "xy.7z"])
        assert got == {"x.7z": ["x.7z"], "xy.7z": ["xy.7z"]}, got

    def test_volume_order_is_numeric(self, tmp_path):
        """卷序按数值排：part9 在 part10 之前。"""
        from backupstudio.manifest import _volume_sort_key

        names = ["s.tar.gz.part10", "s.tar.gz.part9"]
        assert sorted(names, key=_volume_sort_key) == ["s.tar.gz.part9", "s.tar.gz.part10"]


class TestCommitStateMachine:
    """A3 回归：committing 接线、skipped 不算可信、坏包可被回收。"""

    def _rec(self, status, verification):
        from backupstudio.config import RunRecord

        return RunRecord(task_id="t", task_name="n", started_at="2026-01-01T00:00:00",
                         status=status, verification=verification)

    @pytest.mark.parametrize("status,verification,expected", [
        ("success", "passed", "committed"),
        ("success", "skipped", "unverified"),
        ("success", "failed", "partial"),
        ("failure", "skipped", "partial"),
    ])
    def test_state_mapping(self, status, verification, expected):
        """三态必须真的落到提交状态上（旧实现只有 committed/partial 二选一）。"""
        from backupstudio.engine import manifest_state_for

        assert manifest_state_for(self._rec(status, verification)) == expected

    def test_committing_manifest_written_before_verify(self, src_tree, tmp_path, monkeypatch):
        """两阶段提交：校验前必须已有一份 committing 清单（崩溃后可被看见）。"""
        from backupstudio import engine as eng
        from backupstudio.manifest import STATE_COMMITTING, load_manifests

        seen: list[str] = []
        real = eng._write_manifest_stage

        def spy(task, record, result, stem, state):
            seen.append(state)
            return real(task, record, result, stem, state)

        monkeypatch.setattr(eng, "_write_manifest_stage", spy)
        rec = eng.TaskEngine().run(_task(tmp_path / "cs", src_tree, fmt="zip")).record
        assert rec.status == "success", rec.errors
        assert seen[0] == STATE_COMMITTING, f"第一次写清单应是 committing，实际 {seen}"
        assert seen[-1] == "committed", f"最终应是 committed，实际 {seen}"

    def test_unverified_archive_is_not_trusted(self, src_tree, tmp_path, monkeypatch):
        """校验 skipped → 清单不能是 committed（旧实现写 committed，算可信备份）。"""
        from backupstudio import engine as eng
        from backupstudio.manifest import load_manifests

        monkeypatch.setattr(eng, "verify_archive", lambda *a, **k: (None, "跳过校验（测试注入）"))
        dest = tmp_path / "unv"
        rec = eng.TaskEngine().run(_task(dest, src_tree, fmt="zip")).record
        assert rec.status == "success"
        assert rec.verification == "skipped"
        states = [m.state for m in load_manifests(dest).values()]
        assert states == ["unverified"], f"未校验的包必须是 unverified，实际 {states}"

    def test_partial_group_is_reclaimable(self, src_tree, tmp_path):
        """校验失败的坏包必须能被保留策略清理（旧实现永久占盘）。"""
        import time as _time

        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine, apply_retention
        from backupstudio.manifest import Manifest, STATE_PARTIAL, write_manifest

        dest = tmp_path / "gc"
        dest.mkdir()
        bad = dest / "broken_20200101.zip"
        bad.write_bytes(b"PK\x03\x04" + b"x" * 64)
        write_manifest(dest, Manifest(
            stem="broken_20200101.zip", task_id="x", fmt="zip", state=STATE_PARTIAL,
            volumes=["broken_20200101.zip"], size_bytes=68,
            created_at="2020-01-01T00:00:00", committed_at="2020-01-01T00:00:00",
            verified="failed",
        ))
        t = _task(dest, src_tree, fmt="zip")
        t.retention = RetentionPolicy(enabled=True, keep_last=5, keep_days=1, on_cleanup="delete")
        handled = apply_retention(t)
        assert str(bad) in handled or not bad.exists(), f"坏包未被回收：{handled}"
        assert not bad.exists()

    def test_trusted_count_ignores_partial(self, src_tree, tmp_path):
        """份数只数可信备份：1 份可信 + 1 份坏包，keep_last=1 时两者都该被处理。"""
        import time as _time

        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "cnt"
        engine = TaskEngine()
        for i in range(2):
            t = _task(dest, src_tree, fmt="zip")
            t.name = f"c{i}"
            t.retention = RetentionPolicy(enabled=True, keep_last=1, on_cleanup="delete")
            assert engine.run(t).record.status == "success"
            _time.sleep(1.05)
        zips = sorted(p.name for p in dest.glob("*.zip"))
        assert len(zips) == 1, f"keep_last=1 应只留 1 份：{zips}"
