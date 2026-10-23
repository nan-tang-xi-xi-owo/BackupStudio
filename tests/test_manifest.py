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
