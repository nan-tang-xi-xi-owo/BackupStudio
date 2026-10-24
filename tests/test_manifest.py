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
        """keep_last=2 连跑 3 次：保留最新 2 组（manifest 组），删最旧组。

        必须复用**同一个 task 对象（同一个 task.id）**：产品里"同一任务连跑 N 次"的
        id 固定（存 tasks.json）。原先每轮 _task(...) 新建对象 + 改 name，
        id 每次都是新的 default_factory=_new_id；M10 修复（manifest 按 task_id
        认领组）后各次运行互不相认 → 每组都是独立任务的唯一组 → 谁也不被淘汰。
        旧断言能过，恰恰依赖 M10 修复前"跨任务互删"的数据丢失 bug。
        """
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "ret"
        engine = TaskEngine()
        t = _task(dest, src_tree, fmt="zip")
        t.retention = RetentionPolicy(enabled=True, keep_last=2)
        for _ in range(3):
            rec = engine.run(t).record
            assert rec.status == "success", rec.errors
            time.sleep(1.05)
        zips = sorted(p.name for p in dest.glob("*.zip"))
        assert len(zips) == 2, zips
        # 被删的那组，其 manifest 也必须一并清掉（不留"清单在、卷不在"的空组）
        from backupstudio.manifest import load_manifests

        assert set(load_manifests(dest)) == set(zips), (
            f"清单与卷不一致：{sorted(load_manifests(dest))} vs {zips}"
        )

    def test_split_group_survives_retention_as_unit(self, tmp_path):
        """分卷组按组淘汰：不拆散、不残留（C11 回归）。

        同 test_retention_groups_by_manifest_keep_last：必须用同一个 task 对象
        （同一个 id）连跑，否则测的是跨任务隔离（M10）而非同任务的分卷组淘汰。
        """
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine

        src = tmp_path / "data"
        src.mkdir()
        (src / "a.bin").write_bytes(os.urandom(50 * 1024))  # 50KB 不可压 → 必定分卷
        dest = tmp_path / "split"
        engine = TaskEngine()
        t = _task(dest, src, fmt="tar.gz", multivolume="split", volume_value=20, volume_unit="KB")
        t.retention = RetentionPolicy(enabled=True, keep_last=1)
        first: list[str] = []
        for i in range(2):
            rec = engine.run(t).record
            assert rec.status == "success", rec.errors
            if i == 0:
                first = sorted(p.name for p in dest.glob("*.part*"))
            time.sleep(1.05)
        parts = sorted(p.name for p in dest.glob("*.part*"))
        assert parts, "未产出分卷（测试前提失败）"
        # 旧组的所有卷整组消失（不残留任何一片），新组完整保留
        assert not any(p in parts for p in first), f"旧组残留：{parts}（旧组 {first}）"
        # 新组仍是合法分卷序列（第一卷 + 至少一卷）
        assert any(p.endswith(".part01") for p in parts), parts
        assert len(parts) >= 2, parts

    def test_legacy_archives_not_auto_deleted(self, src_tree, tmp_path):
        """迁移红线（E5）：无清单旧包默认不参与自动淘汰——升级不误删。

        旧包名必须用**本任务的任务名**（_task 的名字是 "mt" → mt_N.zip）作前缀：
        若用 old_N.zip 之类与任务名不符的名字，M10 的归属过滤会先一步把它们排除，
        本用例就会"因为别的原因"而通过，E5 红线（include_legacy=False）实际没被测到。
        """
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine, find_backup_blobs

        dest = tmp_path / "legacy"
        dest.mkdir()
        # 预置 3 个"旧包"（无清单，模拟升级前产物；mtime 很旧；前缀属于本任务）
        old = time.time() - 40 * 86400
        for i in range(3):
            p = dest / f"mt_legacy{i}.zip"
            p.write_bytes(b"PK\x03\x04" + b"x" * 100)
            os.utime(p, (old + i, old + i))
        t = _task(dest, src_tree, fmt="zip")
        t.retention = RetentionPolicy(enabled=True, keep_last=1)
        # 前提确认：这些旧包确实**归本任务认领**（能进候选集），只是 managed=False
        claimed = find_backup_blobs(t)
        assert len(claimed) == 3, f"旧包未被本任务认领，红线前提失效：{claimed}"
        assert not all(b.managed for b in claimed), "旧包不应有 manifest"
        # 跑一次新备份（keep_last=1）：旧包应原样保留，只有新清单组参与
        rec = TaskEngine().run(t).record
        assert rec.status == "success", rec.errors
        remaining = sorted(p.name for p in dest.glob("mt_legacy*.zip"))
        assert remaining == ["mt_legacy0.zip", "mt_legacy1.zip", "mt_legacy2.zip"], (
            f"旧包被自动删除了！（E5 红线破坏）剩余：{remaining}"
        )

    def test_legacy_can_be_adopted_explicitly(self, src_tree, tmp_path):
        """显式接管（include_legacy=True）后**本任务的**旧包才参与淘汰。

        旧包名用本任务任务名（mt_legacyN.zip）：产品里旧包由 {name}_{datetime} 模板生成，
        主名恒等于任务名，前缀必然匹配。
        """
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import apply_retention

        dest = tmp_path / "adopt"
        dest.mkdir()
        old = time.time() - 40 * 86400
        for i in range(3):
            p = dest / f"mt_legacy{i}.zip"
            p.write_bytes(b"PK\x03\x04" + b"x" * 100)
            os.utime(p, (old + i, old + i))
        t = _task(dest, src_tree, fmt="zip")
        t.retention = RetentionPolicy(enabled=True, keep_last=2)
        handled = apply_retention(t, include_legacy=True)
        # 3 个旧包 keep_last=2 → 删 1 个
        assert len(handled) == 1, handled
        assert Path(handled[0]).name == "mt_legacy0.zip", handled

    def test_adopted_legacy_stays_task_scoped(self, src_tree, tmp_path):
        """M10 × E5 交叉回归：接管**不得**越界删别的任务的旧包（数据丢失级）。

        显式接管只是"本任务的旧包可以参与淘汰"的授权，不是"整个目录归我管"。
        若哪天有人为了让接管认领陌生包而放开前缀过滤，本用例即失败。
        """
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import apply_retention

        dest = tmp_path / "adopt_shared"
        dest.mkdir()
        old = time.time() - 40 * 86400
        for i in range(3):
            p = dest / f"mt_legacy{i}.zip"  # 本任务的旧包
            p.write_bytes(b"PK\x03\x04" + b"x" * 100)
            os.utime(p, (old + i, old + i))
        # 别的任务的旧包：故意做成**最旧**的一个，这样一旦归属过滤被放开，
        # 它就会落进 keep_last 的淘汰区间而被删（本用例才有牙齿）；
        # 若把它排成最新，即使不过滤也"碰巧"不会被删，变异体就测不出来了。
        other = dest / "other_task.zip"
        other.write_bytes(b"PK\x03\x04" + b"x" * 100)
        os.utime(other, (old - 10, old - 10))
        t = _task(dest, src_tree, fmt="zip")  # 任务名 "mt"
        t.retention = RetentionPolicy(enabled=True, keep_last=1)
        handled = apply_retention(t, include_legacy=True)
        handled_names = {Path(h).name for h in handled}
        # 本任务 3 个旧包、keep_last=1 → 淘汰自己最旧的 2 个
        assert handled_names == {"mt_legacy0.zip", "mt_legacy1.zip"}, handled_names
        assert "other_task.zip" not in handled_names, f"越界删了别的任务的包：{handled_names}"
        assert other.exists(), "别的任务的旧包被删除（M10 在接管路径复发）"

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


class TestP1BManifestReader:
    """W-A2-3 / W-A2-4（S3）：读者侧 PermissionError 重试 + stem 含扩展名时的清单命名。"""

    def test_read_manifest_retries_on_permission_error(self, tmp_path, monkeypatch):
        """清单被杀软/写者瞬时占用（PermissionError）时重试而非直接当"无清单"。

        旧实现：一次 PermissionError → None → 该组降级为 managed=False 旧包 →
        本轮 keep_last 少算一组（实测并发读 31% 撞 PermissionError）。
        """
        import builtins

        from backupstudio.manifest import Manifest, read_manifest, write_manifest

        dest = tmp_path / "d"
        dest.mkdir()
        m = Manifest(stem="rt_20260101.zip", task_id="t1", fmt="zip", volumes=["rt_20260101.zip"])
        p = write_manifest(dest, m)

        real_open = builtins.open
        hits = {"n": 0}

        def flaky(file, *a, **k):
            if str(file) == str(p):
                hits["n"] += 1
                if hits["n"] <= 2:
                    raise PermissionError(13, "模拟共享冲突", str(file))
            return real_open(file, *a, **k)

        monkeypatch.setattr(builtins, "open", flaky)
        got = read_manifest(p)
        assert got is not None, "前两次 PermissionError 应通过重试恢复"
        assert got.stem == "rt_20260101.zip"
        assert hits["n"] >= 3, f"应至少重试到第 3 次才成功：{hits}"

    def test_read_manifest_gives_up_after_three_attempts(self, tmp_path, monkeypatch):
        """持续不可读 → 返回 None（不抛，不阻塞调用方），且只试 3 次。"""
        import builtins

        from backupstudio.manifest import read_manifest

        p = tmp_path / "always.json"
        p.write_text('{"stem": "always"}', encoding="utf-8")
        real_open = builtins.open
        hits = {"n": 0}

        def always_denied(file, *a, **k):
            if str(file) == str(p):
                hits["n"] += 1
                raise PermissionError(13, "永久占用", str(file))
            return real_open(file, *a, **k)

        monkeypatch.setattr(builtins, "open", always_denied)
        assert read_manifest(p) is None
        assert hits["n"] == 3, hits

    def test_corrupt_json_not_retried(self, tmp_path, monkeypatch):
        """确定性损坏（JSONDecodeError）不做无谓重试。"""
        import builtins

        from backupstudio.manifest import read_manifest

        p = tmp_path / "bad2.json"
        p.write_text("{ nope", encoding="utf-8")
        real_open = builtins.open
        hits = {"n": 0}

        def counting_open(file, *a, **k):
            if str(file) == str(p):
                hits["n"] += 1
            return real_open(file, *a, **k)

        monkeypatch.setattr(builtins, "open", counting_open)
        assert read_manifest(p) is None
        assert hits["n"] == 1, hits

    def test_stem_containing_extension_maps_to_own_json(self, tmp_path):
        """stem=`foo.zip_...zip` 时清单文件名为 `<stem>.json`，一包一清单、互不覆盖。"""
        from backupstudio.manifest import (
            STATE_COMMITTED,
            Manifest,
            collect_groups,
            load_manifests,
            write_manifest,
        )

        dest = tmp_path / "multi"
        dest.mkdir()
        stems = ["foo.zip_20260101.zip", "foo.zip_20260102.zip"]
        for stem in stems:
            (dest / stem).write_bytes(b"PK\x03\x04" + b"x" * 32)
            write_manifest(dest, Manifest(stem=stem, task_id="T", fmt="zip",
                                          state=STATE_COMMITTED, volumes=[stem]))
        ms = load_manifests(dest)
        assert set(ms) == set(stems), sorted(ms)
        groups = collect_groups(dest, ".zip", fmts=(".zip", ".7z", ".rar", ".tar"))
        assert len(groups) == 2, [g.stem for g in groups]
        assert all(g.managed and g.complete for g in groups)

    def test_legacy_grouping_does_not_merge_distinct_archives(self, tmp_path):
        """无清单旧包：任务名含扩展名的多个包不得被启发式并进同一组（_stem_of 同类缺陷）。

        修复前 `_stem_of` 用 `find()` 首匹配 → `foo.zip_20260101.zip` 与
        `foo.zip_20260102.zip` 主名都是 `foo.zip` → 合成一组（一旦接管就是整组删除）。
        """
        from backupstudio.manifest import collect_groups

        dest = tmp_path / "legacy"
        dest.mkdir()
        names = ["foo.zip_20260101.zip", "foo.zip_20260102.zip", "foo.zip_20260103.zip"]
        for i, n in enumerate(names):
            p = dest / n
            p.write_bytes(b"PK\x03\x04" + b"x" * 32)
            os.utime(p, (time.time() - (40 - i) * 86400, time.time() - (40 - i) * 86400))
        groups = collect_groups(dest, ".zip", fmts=(".zip", ".7z", ".rar", ".tar"))
        assert len(groups) == 3, [g.stem for g in groups]
        assert all(len(g.files) == 1 for g in groups), [g.files for g in groups]

    def test_native_volume_stem_matches_product_stem(self, tmp_path):
        """`.001` 分卷的主名与 archives.product_stem(dest.name) 一致（清单↔启发式对表）。"""
        from backupstudio.archives import product_stem
        from backupstudio.manifest import _stem_of

        exts = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip", ".7z", ".rar", ".tar")
        for main, vol in [("X.7z", "X.7z.001"), ("Y.zip", "Y.zip.part02"),
                          ("Z.tar.gz", "Z.tar.gz.part03"), ("W.rar", "W.rar.part1")]:
            assert _stem_of(vol, exts) == main == product_stem(main), (vol, _stem_of(vol, exts), main)


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
        t = _task(dest, src_tree, fmt="zip")
        # 清单必须带**本任务**的 task_id：上游 M10 修复后 find_backup_blobs 按 task_id
        # 认领组，写死 "x" 会让这个坏包被判定为"别的任务的包"→ 保留策略不认领它。
        # 那测到的是 M10 的隔离，而不是"坏包可被回收"。
        write_manifest(dest, Manifest(
            stem="broken_20200101.zip", task_id=t.id, fmt="zip", state=STATE_PARTIAL,
            volumes=["broken_20200101.zip"], size_bytes=68,
            created_at="2020-01-01T00:00:00", committed_at="2020-01-01T00:00:00",
            verified="failed",
        ))
        t.retention = RetentionPolicy(enabled=True, keep_last=5, keep_days=1, on_cleanup="delete")
        handled = apply_retention(t)
        assert str(bad) in handled or not bad.exists(), f"坏包未被回收：{handled}"
        assert not bad.exists()

    def test_trusted_count_ignores_partial(self, src_tree, tmp_path):
        """份数只数可信备份：1 份可信 + 1 份坏包，keep_last=1 时两者都该被处理。

        必须复用**同一个 task 对象（同一个 task.id）**：M10 修复后保留策略按 task_id
        认领备份组，每轮新建对象会让每组都属于"另一个任务"，于是谁也不会被淘汰
        —— 那测到的是跨任务隔离，不是"份数只数可信备份"。
        """
        import time as _time

        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "cnt"
        engine = TaskEngine()
        t = _task(dest, src_tree, fmt="zip")
        t.retention = RetentionPolicy(enabled=True, keep_last=1, on_cleanup="delete")
        for _ in range(2):
            assert engine.run(t).record.status == "success"
            _time.sleep(1.05)
        zips = sorted(p.name for p in dest.glob("*.zip"))
        assert len(zips) == 1, f"keep_last=1 应只留 1 份：{zips}"


class TestUnverifiedNotReclaimed:
    """P0 回归：``unverified`` 是「没验过」，不是「不是备份」，不得被保留策略回收。

    背景：``manifest.RECLAIMABLE_STATES`` 含 ``STATE_UNVERIFIED``，而
    ``apply_retention`` 把所有 reclaimable 组当成残包无条件回收。
    ``verify=False`` 的用户每次备份都会产出 unverified → **每次备份都把上一份删掉**。
    实测 keep_last=30 连跑 3 次只剩 1 份（数据丢失）。
    """

    @staticmethod
    def _policy(**kw):
        from backupstudio.config import RetentionPolicy

        base = dict(enabled=True, keep_last=30, keep_days=3650, on_cleanup="delete")
        base.update(kw)
        return RetentionPolicy(**base)

    def test_unverified_survives_next_run(self, src_tree, tmp_path):
        """P0 核心：verify=False 连跑 3 次，keep_last=30 时 3 份必须都还在。"""
        import time as _t

        from backupstudio.engine import TaskEngine
        from backupstudio.manifest import STATE_UNVERIFIED, collect_groups

        dest = tmp_path / "keepunv"
        eng = TaskEngine()
        for i in range(3):
            t = _task(dest, src_tree, fmt="zip")
            t.verify = False
            t.name = f"u{i}"
            t.retention = self._policy()
            assert eng.run(t).record.status == "success"
            _t.sleep(1.05)

        zips = sorted(p.name for p in dest.glob("*.zip"))
        assert len(zips) == 3, f"unverified 历史备份被误删（P0）：{zips}"

        states = [g.manifest.state for g in collect_groups(dest, ".zip") if g.manifest]
        assert states and set(states) == {STATE_UNVERIFIED}, f"状态应为 unverified，实际 {states}"

    def test_unverified_survives_with_verify_true(self, src_tree, tmp_path):
        """verify=True + on_cleanup=delete 组合同样不能丢历史份。"""
        import time as _t

        from backupstudio.engine import TaskEngine

        dest = tmp_path / "delchk"
        eng = TaskEngine()
        for i in range(3):
            t = _task(dest, src_tree, fmt="zip")
            t.verify = True
            t.name = f"v{i}"
            t.retention = self._policy()
            assert eng.run(t).record.status == "success"
            _t.sleep(1.05)
        assert len(list(dest.glob("*.zip"))) == 3, "verify=True 连跑 3 次也应保留 3 份"

    def test_unverified_survives_with_move_mode(self, src_tree, tmp_path):
        """on_cleanup=move 下 unverified 也不能被挪进 trash。"""
        import time as _t

        from backupstudio.engine import TaskEngine

        dest = tmp_path / "movechk"
        eng = TaskEngine()
        for i in range(3):
            t = _task(dest, src_tree, fmt="zip")
            t.verify = False
            t.name = f"m{i}"
            t.retention = self._policy(on_cleanup="move")
            assert eng.run(t).record.status == "success"
            _t.sleep(1.05)
        assert len(list(dest.glob("*.zip"))) == 3, "move 模式下 unverified 也不得被回收"
        trash = dest / ".backupstudio" / "trash"
        moved = list(trash.rglob("*.zip")) if trash.is_dir() else []
        assert not moved, f"unverified 被挪进回收站：{moved}"

    def test_partial_junk_still_reclaimed(self, src_tree, tmp_path):
        """反向守卫：真残包（partial）必须仍被回收 —— 修 P0 不能把 junk 一起保住。"""
        from backupstudio.engine import TaskEngine
        from backupstudio.manifest import Manifest, STATE_PARTIAL, write_manifest

        dest = tmp_path / "junk"
        dest.mkdir()
        junk = dest / "junk.zip"
        junk.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        t = _task(dest, src_tree, fmt="zip")
        # task_id 必须归属本任务（M10 按 task_id 认领组），否则测到的是跨任务隔离
        write_manifest(dest, Manifest(
            stem="junk.zip", task_id=t.id, fmt="zip", state=STATE_PARTIAL,
            volumes=["junk.zip"], size_bytes=22,
        ))
        t.retention = self._policy()
        assert TaskEngine().run(t).record.status == "success"
        assert not junk.exists(), "partial 残包应被回收（否则残包永久占盘）"

    def test_unverified_still_counts_for_keep_last(self, src_tree, tmp_path):
        """反向守卫：unverified 计入份数并参与淘汰，不是"永不删除"。

        复用同一个 task 对象（同一 task.id）连跑：M10 修复后跨任务不得互删，
        因此"保留最新 N 份"只能在同一任务id 内测。
        """
        import time as _t

        from backupstudio.engine import TaskEngine

        dest = tmp_path / "keep2"
        eng = TaskEngine()
        t = _task(dest, src_tree, fmt="zip")
        t.verify = False
        t.retention = self._policy(keep_last=2)
        for _ in range(3):
            assert eng.run(t).record.status == "success"
            _t.sleep(1.05)
        zips = sorted(p.name for p in dest.glob("*.zip"))
        assert len(zips) == 2, f"keep_last=2 时 unverified 应淘汰到 2 份：{zips}"
