"""S2 回归：幽灵组（清单在、卷全丢）不得占用 keep_last 配额。

## 缺陷（数据丢失级）

三个条件叠加，让「清单在、但一个卷都不在盘上」的组被当成**一份可信备份**：

1. ``manifest.collect_groups`` 对卷全丢的 stem 仍建组（``files=[]``、``complete=False``）；
2. ``BackupGroup.managed`` 只判断 ``manifest.is_committed``，**完全忽略 ``complete``**；
3. ``engine.apply_retention`` 的 ``trusted`` 桶用 ``b.managed`` 取候选，从不读 ``complete``
   （全文件 ``complete`` 只有定义/传递/赋值三处，无任何判断）。

后果：``keep_last=2`` 且目录里有 1 个幽灵组时，``trusted`` 数是 3（2 真 + 1 幽灵），
淘汰 ``len-keep_last = 1`` 个 → **删掉 1 份真备份**，稳态只剩 1 份。这是静默的数据丢失。

## 修复后的两种语义必须同时成立

* **不占配额**：幽灵组与缺卷残包不是「一份完整备份」，不参与 ``keep_last/keep_days/
  max_total_gb`` 的计数（否则会挤掉真备份）；
* **仍可回收**：幽灵组只剩一份孤儿清单，不清理就永久占盘（"空 manifest 僵屍"）。

本文件把这两条分别钉死，另外钉住「不得反向多删」和「不得误伤瞬时不可读的盘」。
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


def _ts(offset_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + offset_seconds))


def _task(dest: Path, *, keep_last: int, name: str = "gh", on_cleanup: str = "delete"):
    from backupstudio.config import ArchiveOptions, BackupTask, RetentionPolicy, SourceConfig

    t = BackupTask(
        name=name,
        sources=[SourceConfig(path=str(dest))],
        destination=str(dest),
        options=ArchiveOptions(force=True, fmt="zip"),
    )
    t.retention = RetentionPolicy(
        enabled=True, keep_last=keep_last, keep_days=36500, on_cleanup=on_cleanup
    )
    return t


def _real_backup(dest: Path, task, name: str, offset: float, *, size: int = 1024) -> Path:
    """造一份「卷齐全、状态 committed」的真备份（文件 + 配套清单）。"""
    from backupstudio.manifest import STATE_COMMITTED, Manifest, write_manifest

    p = dest / name
    p.write_bytes(b"PK\x03\x04" + b"x" * size)
    write_manifest(
        dest,
        Manifest(
            stem=name,
            task_id=task.id,
            task_name=task.name,
            fmt="zip",
            state=STATE_COMMITTED,
            volumes=[name],
            size_bytes=p.stat().st_size,
            created_at=_ts(offset),
            committed_at=_ts(offset),
            verified="passed",
        ),
    )
    return p


def _ghost(dest: Path, task, name: str, offset: float) -> None:
    """造一个幽灵组：清单 committed、但清单里列的卷**一个都不在盘上**。"""
    from backupstudio.manifest import STATE_COMMITTED, Manifest, write_manifest

    write_manifest(
        dest,
        Manifest(
            stem=name,
            task_id=task.id,
            task_name=task.name,
            fmt="zip",
            state=STATE_COMMITTED,
            volumes=[name],
            size_bytes=0,
            created_at=_ts(offset),
            committed_at=_ts(offset),
            verified="passed",
        ),
    )


class TestGhostGroupDoesNotConsumeQuota:
    """核心：幽靈組不占配额，真备份按 keep_last 全数保留。"""

    def test_keep_last_two_survives_one_ghost(self, tmp_path):
        """``keep_last=2`` + 1 幽靈組 → 2 份真備份**都必須在**。

        修复前实测：``handled=['gh_real0.zip']``，只剩 ``gh_real1.zip`` 一份。
        """
        from backupstudio.engine import apply_retention

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=2)
        _real_backup(dest, t, "gh_real0.zip", -300)
        _real_backup(dest, t, "gh_real1.zip", -240)
        # 幽靈組刻意做成**最新**的：最容易占住"保留最新 N 份"里的一个席位
        _ghost(dest, t, "gh_ghost.zip", -180)

        handled = apply_retention(t)

        remaining = sorted(p.name for p in dest.glob("*.zip"))
        assert remaining == ["gh_real0.zip", "gh_real1.zip"], (
            f"幽靈組擠掉了真備份（S2 數據丟失）：剩 {remaining}，handled={handled}"
        )
        assert handled == [], f"keep_last=2 且只有 2 份真備份，不該淘汰任何文件：{handled}"

    def test_ghost_manifest_is_reclaimed(self, tmp_path):
        """幽靈組的 manifest 必須被清掉（否則空清單永久占盤）。"""
        from backupstudio.engine import apply_retention
        from backupstudio.manifest import load_manifests

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=2)
        _real_backup(dest, t, "gc_real0.zip", -300)
        _ghost(dest, t, "gc_ghost.zip", -100)

        apply_retention(t)

        left = load_manifests(dest)
        assert "gc_ghost.zip" not in left, f"幽靈組清單未被清理（僵屍殘留）：{sorted(left)}"
        assert "gc_real0.zip" in left, f"真備份的清單被誤刪：{sorted(left)}"

    def test_ghost_alone_with_room_to_spare(self, tmp_path):
        """只有幽靈組、真備份未超配額：幽靈被清、真備份一個不動。

        這是 mantifest 泄漏最典型的形态：每次崩溃留下一份 committed 清单但卷没了，
        长期积累后占满 keep_last，把用户所有真备份挤光。
        """
        from backupstudio.engine import apply_retention
        from backupstudio.manifest import load_manifests

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=3)
        for i in range(2):
            _real_backup(dest, t, f"gr_real{i}.zip", -300 + i * 10)
        for i in range(3):
            _ghost(dest, t, f"gr_ghost{i}.zip", -200 + i * 10)

        apply_retention(t)

        assert sorted(p.name for p in dest.glob("*.zip")) == ["gr_real0.zip", "gr_real1.zip"]
        left = load_manifests(dest)
        assert set(left) == {"gr_real0.zip", "gr_real1.zip"}, f"幽靈清單殘留：{sorted(left)}"

    def test_many_ghosts_never_evict_real_backups(self, tmp_path):
        """極端：10 個幽靈組 + keep_last=2 + 2 份真備份 → 真備份仍全在。

        修复前這裡會把 2 份真備份**全刪光**（trusted 數 = 12，淘汰 10 個最舊的，
        而幽靈組的 committed_at 可以很新，排序後真備份反而落在淘汰區間）。
        """
        from backupstudio.engine import apply_retention

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=2)
        _real_backup(dest, t, "gm_real0.zip", -900)
        _real_backup(dest, t, "gm_real1.zip", -840)
        for i in range(10):
            _ghost(dest, t, f"gm_ghost{i}.zip", -800 + i * 10)

        apply_retention(t)

        remaining = sorted(p.name for p in dest.glob("*.zip"))
        assert remaining == ["gm_real0.zip", "gm_real1.zip"], (
            f"大量幽靈組導致真備份被刪（S2）：剩 {remaining}"
        )


class TestPartialGroupDoesNotConsumeQuota:
    """缺卷殘包（``complete=False`` 但還有部分卷）同樣不占配額。"""

    def test_partial_volumes_group_not_counted(self, tmp_path):
        """清單聲明 3 卷、盤上只有 1 卷 → 不是一份完整備份，不占 keep_last 配額。"""
        from backupstudio.engine import apply_retention, find_backup_blobs
        from backupstudio.manifest import STATE_COMMITTED, Manifest, write_manifest

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=2)
        _real_backup(dest, t, "gp_real0.zip", -300)
        _real_backup(dest, t, "gp_real1.zip", -240)
        # 殘包：清單 3 卷，只落了第 1 卷
        (dest / "gp_broken.7z.001").write_bytes(b"a" * 64)
        write_manifest(
            dest,
            Manifest(
                stem="gp_broken.7z",
                task_id=t.id,
                task_name=t.name,
                fmt="zip",
                state=STATE_COMMITTED,
                volumes=["gp_broken.7z.001", "gp_broken.7z.002", "gp_broken.7z.003"],
                created_at=_ts(-180),
                committed_at=_ts(-180),
                verified="passed",
            ),
        )

        blobs = {b.stem: b for b in find_backup_blobs(t)}
        broken = blobs.get("gp_broken.7z")
        assert broken is not None, f"缺卷組未被看見：{sorted(blobs)}"
        assert broken.complete is False, "缺卷組的 complete 應為 False"
        assert broken.countable is False, "缺卷組不應計入 keep_last 配額"

        apply_retention(t)
        remaining = sorted(p.name for p in dest.glob("*.zip"))
        assert remaining == ["gp_real0.zip", "gp_real1.zip"], (
            f"缺卷殘包擠掉了真備份：剩 {remaining}"
        )


class TestNoReverseOverDeletion:
    """反向红线：修復不得變成「多刪」或「一律不刪」。"""

    def test_keep_last_one_with_ghost_still_evicts_oldest_real(self, tmp_path):
        """``keep_last=1`` + 幽靈：仍要淘汰較舊的真備份（幽靈不占位 ≠ 不淘汰）。"""
        from backupstudio.engine import apply_retention

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=1)
        _real_backup(dest, t, "ko_old.zip", -300)
        _real_backup(dest, t, "ko_new.zip", -100)
        _ghost(dest, t, "ko_ghost.zip", -50)

        handled = apply_retention(t)
        names = {Path(h).name for h in handled}

        assert names == {"ko_old.zip"}, f"keep_last=1 應只淘汰最舊那份，實際 {names}"
        assert (dest / "ko_new.zip").exists(), "最新的真備份被誤刪"
        assert not (dest / "ko_old.zip").exists()

    def test_healthy_single_backup_untouched(self, tmp_path):
        """只有一份健康備份 + 幽靈：真備份必須完好（最容易被幽靈擠掉的情形）。"""
        from backupstudio.engine import apply_retention

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=2)
        _real_backup(dest, t, "kh_only.zip", -300)
        _ghost(dest, t, "kh_ghost.zip", -50)

        handled = apply_retention(t)
        assert (dest / "kh_only.zip").exists(), f"唯一一份真備份被刪：{handled}"

    def test_ghost_with_stale_timestamp_still_cleaned(self, tmp_path):
        """幽靈組很舊也照樣清（回收判據與 commit 時間無關）。"""
        from backupstudio.engine import apply_retention
        from backupstudio.manifest import load_manifests

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=5)
        _real_backup(dest, t, "ks_real.zip", -100)
        _ghost(dest, t, "ks_ghost.zip", -100000)  # 很舊

        apply_retention(t)
        assert (dest / "ks_real.zip").exists()
        assert "ks_ghost.zip" not in load_manifests(dest), "舊幽靈組清單殘留"

    def test_ghost_cleanup_respects_move_mode(self, tmp_path):
        """``on_cleanup=move`` 時幽靈組清單走回收目錄路徑，真備份不受影響。"""
        from backupstudio.engine import apply_retention

        dest = tmp_path / "d"
        dest.mkdir()
        trash = tmp_path / "trash"
        t = _task(dest, keep_last=2, on_cleanup="move")
        t.retention.cleanup_dir = str(trash)
        _real_backup(dest, t, "km_real0.zip", -300)
        _real_backup(dest, t, "km_real1.zip", -200)
        _ghost(dest, t, "km_ghost.zip", -100)

        apply_retention(t)
        assert sorted(p.name for p in dest.glob("*.zip")) == ["km_real0.zip", "km_real1.zip"]

    def test_unverified_group_still_counted(self, tmp_path):
        """P0 反向回归：``unverified``（没验过但卷齐全）**仍要**计入配额。

        S2 的修法只排除 ``complete=False``，绝不能顺手把 unverified 一起排除——
        那会让 ``verify=False`` 的用户每次备份都丢掉上一份（P0 数据丢失复发）。
        """
        from backupstudio.engine import apply_retention
        from backupstudio.manifest import STATE_UNVERIFIED, Manifest, write_manifest

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=1)
        for i, off in enumerate((-300, -200)):
            name = f"ku_unv{i}.zip"
            (dest / name).write_bytes(b"PK\x03\x04" + b"x" * 512)
            write_manifest(
                dest,
                Manifest(
                    stem=name,
                    task_id=t.id,
                    task_name=t.name,
                    fmt="zip",
                    state=STATE_UNVERIFIED,
                    volumes=[name],
                    created_at=_ts(off),
                    committed_at=_ts(off),
                    verified="skipped",
                ),
            )

        apply_retention(t)
        remaining = sorted(p.name for p in dest.glob("*.zip"))
        assert remaining == ["ku_unv1.zip"], (
            f"keep_last=1 且兩份 unverified 都完整，應只留最新一份：{remaining}"
        )


class TestUnreachableVolumeIsNotTreatedAsGhost:
    """健全性红线：**所有**卷同时"看不见"时不得把清单全删。

    背景（本修复引入的新风险，必须钉住）：``collect_groups`` 用 ``Path.is_file()``
    判定卷在不在，而 pathlib 在 ``OSError``（网络盘掉线、介质未挂载、权限瞬时收紧）
    时**返回 False 而不抛异常**。那一刻目录里所有清单的卷会"同时消失"，看起来
    和"一堆幽灵组"一模一样。

    如果这时照样把清单删掉：
    * 备份会退化成**无清单旧包** → 从此受 E5 迁移红线保护 → 永不自动清理（磁盘悄悄涨）；
    * 全部元数据（task_id / committed_at / 校验状态）丢失。

    因此回收幽灵清单前要求「目录里至少有一份看得见的完整备份」——这等价于
    "此刻这个卷确实读得出来"。本用例把不变量钉死。
    """

    def test_all_manifests_kept_when_no_complete_backup_visible(self, tmp_path):
        from backupstudio.engine import apply_retention
        from backupstudio.manifest import load_manifests

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=5)
        # 三份"正常备份"的清单，但盘上一个卷都没有（模拟整卷瞬时不可读）
        for i in range(3):
            _ghost(dest, t, f"uv_g{i}.zip", -300 + i * 10)

        apply_retention(t)

        left = load_manifests(dest)
        assert len(left) == 3, (
            "所有卷同时不可见时不得回收清单（可能是网络盘掉线/介质未挂载，"
            f"删了就永久丢失元数据且再也不会被自动清理）：剩 {sorted(left)}"
        )

    def test_ghost_cleaned_once_a_complete_backup_is_visible(self, tmp_path):
        """反向：目录里**有**一份可见的完整备份时，幽灵清单正常回收（不能因噎废食）。"""
        from backupstudio.engine import apply_retention
        from backupstudio.manifest import load_manifests

        dest = tmp_path / "d"
        dest.mkdir()
        t = _task(dest, keep_last=5)
        _real_backup(dest, t, "uv_real.zip", -500)
        _ghost(dest, t, "uv_ghost.zip", -100)

        apply_retention(t)

        left = load_manifests(dest)
        assert set(left) == {"uv_real.zip"}, (
            f"有可见完整备份时幽灵清单应被回收：剩 {sorted(left)}"
        )
        assert (dest / "uv_real.zip").exists()
