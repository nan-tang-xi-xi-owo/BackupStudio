"""pytest 用例：调度、配置、引擎、压缩后端（不依赖 GUI）。"""

from __future__ import annotations

import os
import sys
import tarfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(cfg))
    return cfg


@pytest.fixture()
def sample_tree(tmp_path):
    src = tmp_path / "data"
    (src / "sub" / "deep").mkdir(parents=True)
    (src / "a.txt").write_text("hello world\n" * 500, encoding="utf-8")
    (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 400)
    (src / "sub" / "deep" / "c.log").write_text("line\n" * 2000, encoding="utf-8")
    (src / "empty").mkdir()
    (src / "Thumbs.db").write_bytes(b"junk")
    return src


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------

class TestSchedule:
    def test_daily_due(self):
        from backupstudio.config import BackupTask, ScheduleConfig

        now = datetime(2026, 1, 1, 3, 0, 0)
        task = BackupTask(name="t", schedule=ScheduleConfig(type="daily", time="02:00"))
        last = datetime(2025, 12, 31, 2, 0, 0)
        from backupstudio.schedule import due, next_run

        assert due(task, now, last) is True
        assert due(task, now, datetime(2026, 1, 1, 2, 0, 0)) is False
        nxt = next_run(task.schedule, now, last)
        assert nxt == datetime(2026, 1, 2, 2, 0, 0)

    def test_weekly_and_manual(self):
        from backupstudio.config import ScheduleConfig
        from backupstudio.schedule import next_run

        weekly = ScheduleConfig(type="weekly", time="02:00", days=[0])  # 周一
        monday = datetime(2026, 1, 5, 2, 0)  # 2026-01-05 是周一
        assert next_run(weekly, datetime(2026, 1, 5, 1, 0), None) == monday
        manual = ScheduleConfig(type="manual")
        assert next_run(manual, datetime.now(), None) is None

    def test_interval(self):
        from backupstudio.config import ScheduleConfig
        from backupstudio.schedule import next_run

        now = datetime(2026, 1, 1, 12, 0, 0)
        sched = ScheduleConfig(type="interval", minutes=30)
        assert next_run(sched, now, datetime(2026, 1, 1, 11, 45)) == datetime(2026, 1, 1, 12, 15)

    def test_monthly_clamps_days(self):
        from backupstudio.config import ScheduleConfig
        from backupstudio.schedule import next_run

        sched = ScheduleConfig(type="monthly", day=31, time="01:00")
        nxt = next_run(sched, datetime(2026, 2, 1, 0, 0), None)
        assert nxt is not None
        assert nxt.month in (3, 5)  # 2 月没有 31 号，向后找到有 31 号的月份

    def test_missed_grace(self):
        from backupstudio.config import BackupTask, ScheduleConfig
        from backupstudio.schedule import due

        task = BackupTask(name="t", schedule=ScheduleConfig(type="daily", time="02:00"))
        # 首次启用且已错过很久：当天不再补跑
        assert due(task, datetime(2026, 1, 1, 23, 0), None) is False


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

class TestConfig:
    def test_roundtrip(self):
        from backupstudio.config import ArchiveOptions, BackupTask, RetentionPolicy, ScheduleConfig, SourceConfig

        task = BackupTask(
            name="测试",
            sources=[SourceConfig(path="/tmp/x", mode="files", files=["a.txt"])],
            destination="/tmp/y",
            options=ArchiveOptions(fmt="7z", password="pw", multivolume="7z-split", volume_value=10),
            schedule=ScheduleConfig(type="weekly", time="03:15", days=[1, 3]),
            retention=RetentionPolicy(enabled=True, keep_last=5),
        )
        data = task.to_dict()
        again = BackupTask.from_dict(data)
        assert again.name == task.name
        assert again.options.fmt == "7z"
        assert again.options.password == "pw"
        assert again.options.volume_size_bytes() == 10 * 1024 * 1024
        assert again.schedule.days == [1, 3]
        assert again.schedule.time == "03:15"
        assert again.retention.keep_last == 5

    def test_normalize_bad_values(self):
        from backupstudio.config import ArchiveOptions, ScheduleConfig

        opts = ArchiveOptions(fmt="bogus", level=99, encrypt_mode="nope", volume_unit="XX")
        assert opts.fmt == "7z"
        assert opts.level == 9
        assert opts.encrypt_mode == "aes256"
        assert opts.volume_unit == "MB"
        sched = ScheduleConfig(type="daily", time="99:99", minutes=-5)
        assert sched.time == "02:00"
        assert sched.minutes == 1

    def test_store_persistence(self, tmp_config):
        from backupstudio.store import TaskStore

        store = TaskStore(tmp_config / "tasks.json")
        t = store.upsert(__import__("backupstudio.config", fromlist=["BackupTask"]).BackupTask(name="A"))
        store.save()
        again = TaskStore(tmp_config / "tasks.json")
        assert len(again.list_tasks()) == 1
        assert again.get(t.id) is not None
        assert again.duplicate(t.id).name.endswith("（副本）")


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

class TestUtil:
    def test_template_and_sanitize(self):
        from backupstudio.util import sanitize_component, template_context, render_template

        assert sanitize_component('a<b>c:d') == "a_b_c_d"
        assert sanitize_component("CON") == "_CON"
        ctx = template_context("我的 备份/项目")
        out = render_template("{name}_{date}", ctx)
        assert "/" not in out
        assert ctx["date"] in out

    def test_human_size(self):
        from backupstudio.util import human_size

        assert human_size(0) == "0 B"
        assert human_size(1536).startswith("1.50 KB")
        assert human_size(None) == "-"

    def test_path_inside(self, tmp_path):
        from backupstudio.util import is_path_inside

        assert is_path_inside(tmp_path / "a" / "b", tmp_path)
        assert not is_path_inside(tmp_path.parent / "other", tmp_path)


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------

class TestPlanning:
    def test_excludes_and_dir_sizes(self, sample_tree):
        from backupstudio.archives import Item, plan_items

        items, skipped = plan_items([Item(sample_tree, "data")], exclude_globs=["*.log"])
        names = [i.arcname for i in items]
        assert "data/a.txt" in names
        assert not any(n.endswith(".log") for n in names)
        assert not any(n.endswith("Thumbs.db") for n in names)
        assert skipped >= 2
        root_item = next(i for i in items if i.arcname == "data")
        assert root_item.size > 0 and root_item.is_dir

    def test_specific_files(self, sample_tree, tmp_path):
        from backupstudio.config import BackupTask, SourceConfig
        from backupstudio.engine import build_sources

        task = BackupTask(name="t", sources=[SourceConfig(path=str(sample_tree), mode="files", files=["a.txt", "sub/b.bin"])])
        items, warnings = build_sources(task)
        arcs = {i.arcname for i in items}
        assert "data/a.txt" in arcs
        assert "data/sub/b.bin" in arcs
        assert not warnings


# --------------------------------------------------------------------------
# 引擎 + 各格式
# --------------------------------------------------------------------------

def _make_task(fmt, dest, src, **opt_kwargs):
    from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

    return BackupTask(
        name=f"t-{fmt}",
        sources=[SourceConfig(path=str(src))],
        destination=str(dest),
        options=ArchiveOptions(fmt=fmt, **opt_kwargs),
    )


class TestEngine:
    @pytest.mark.parametrize("fmt", ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst"])
    def test_python_formats(self, fmt, sample_tree, tmp_path, tmp_config):
        from backupstudio.engine import TaskEngine

        dest = tmp_path / f"out-{fmt.replace('.', '_')}"
        # force=True：外部程序（如 7z）在此测试环境被系统限制时自动回退内置引擎
        result = TaskEngine().run(_make_task(fmt, dest, sample_tree, force=True))
        assert result.record.status == "success", result.record.errors
        assert Path(result.record.archive).exists()
        files = sorted(p.name for p in dest.iterdir())
        assert any(f.startswith("t-") for f in files)
        # 能重新打开并读到内容
        if fmt == "zip":
            with zipfile.ZipFile(result.record.archive) as zf:
                assert "data/a.txt" in zf.namelist()
        elif fmt.startswith("tar"):
            with tarfile.open(result.record.archive) as tf:
                assert "data/a.txt" in tf.getnames()

    def test_7z_native_or_fallback(self, sample_tree, tmp_path, tmp_config):
        from backupstudio.engine import TaskEngine

        opts = dict(password="secret1", force=True)
        result = TaskEngine().run(_make_task("7z", tmp_path / "out7z", sample_tree, **opts))
        assert result.record.status == "success", result.record.errors
        assert Path(result.record.archive).stat().st_size > 0

    def test_small_volume_split(self, sample_tree, tmp_path, tmp_config):
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "split"
        task = _make_task("tar.gz", dest, sample_tree, multivolume="split", volume_value=1, volume_unit="KB")
        result = TaskEngine().run(task)
        assert result.record.status == "success", result.record.errors
        parts = sorted(p.name for p in dest.glob("*.part*"))
        assert len(parts) >= 2
        # 拼接后能正常解压
        data = b"".join((dest / p).read_bytes() for p in parts)
        joined = tmp_path / "joined.tar.gz"
        joined.write_bytes(data)
        with tarfile.open(joined) as tf:
            assert "data/a.txt" in tf.getnames()

    def test_missing_source_is_skipped(self, tmp_path, tmp_config):
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        task = BackupTask(
            name="bad", sources=[SourceConfig(path=str(tmp_path / "nope"))],
            destination=str(tmp_path / "out"), options=ArchiveOptions(fmt="zip"),
        )
        result = TaskEngine().run(task)
        assert result.record.status == "skipped"
        assert "不存在" in (result.record.errors[0] + "".join(result.record.errors))

    def test_no_destination_is_skipped(self, sample_tree, tmp_config):
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        task = BackupTask(name="nodest", sources=[SourceConfig(path=str(sample_tree))], options=ArchiveOptions(fmt="zip"))
        result = TaskEngine().run(task)
        assert result.record.status == "skipped"

    def test_retention_keeps_last(self, sample_tree, tmp_path, tmp_config):
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "ret"
        engine = TaskEngine()
        for _ in range(4):
            task = _make_task("zip", dest, sample_tree, force=True)
            task.retention = RetentionPolicy(enabled=True, keep_last=2)
            # 让文件名模板含秒，避免重名
            task.options.filename_template = "{name}_{datetime}"
            engine.run(task)
            time.sleep(1.05)
        assert len(list(dest.glob("*.zip"))) == 2

    def test_rar_unavailable_message(self, sample_tree, tmp_path, tmp_config):
        from backupstudio.archives import rar_available

        if rar_available():
            pytest.skip("本机 RAR 可创建压缩包")
        from backupstudio.engine import TaskEngine

        result = TaskEngine().run(_make_task("rar", tmp_path / "rar", sample_tree))
        assert result.record.status == "failure"
        assert "RAR" in result.message

    def test_forced_rar_falls_back(self, sample_tree, tmp_path, tmp_config):
        from backupstudio.archives import rar_available

        if rar_available():
            pytest.skip("本机 RAR 可创建压缩包")
        from backupstudio.engine import TaskEngine

        result = TaskEngine().run(_make_task("rar", tmp_path / "rar2", sample_tree, force=True))
        assert result.record.status == "success", result.record.errors
        assert result.record.forced is True
        assert result.record.warnings


# --------------------------------------------------------------------------
# 能力矩阵
# --------------------------------------------------------------------------

class TestCapabilities:
    def test_matrix_shape(self):
        from backupstudio.archives import SUPPORTED_FORMATS
        from backupstudio.capabilities import format_capabilities

        caps = format_capabilities()
        assert set(caps) == set(SUPPORTED_FORMATS)
        for entry in caps.values():
            assert "available" in entry
            assert "multivolume" in entry
            assert "password" in entry

    def test_platform_info(self):
        from backupstudio.platform_info import describe_environment, get_platform_info

        info = get_platform_info()
        assert info.label
        assert describe_environment()

    def test_daemon_status_offline(self, tmp_config):
        from backupstudio.daemon import daemon_status

        st = daemon_status()
        assert st.get("running") in (True, False)


# --------------------------------------------------------------------------
# 通知 / 自启动（只读检查）
# --------------------------------------------------------------------------

class TestNotify:
    def test_quiet_hours(self):
        from backupstudio.notify import in_quiet_hours

        assert in_quiet_hours(False, "23:00", "07:00") is False
        # 只要函数不抛异常即可（结果取决于当前时间）
        assert isinstance(in_quiet_hours(True, "00:00", "23:59"), bool)

    def test_backend_description(self):
        from backupstudio.notify import describe_backend

        assert describe_backend()


# --------------------------------------------------------------------------
# 测试员补充：取消、中文/emoji 文件名、强制 ZIP 真实性
# --------------------------------------------------------------------------

def _cjk_tree(tmp_path):
    src = tmp_path / "src"
    (src / "子目录").mkdir(parents=True)
    (src / "子目录" / "报告 2026😀.txt").write_text("报告\n" * 200, encoding="utf-8")
    (src / ".hidden").write_text("h", encoding="utf-8")
    (src / "big.bin").write_bytes(b"\x00" * (6 * 1024 * 1024))
    return src


class TestTesterFindings:
    def test_zip_forced_is_real_zip_with_cjk(self, tmp_path, tmp_config, monkeypatch):
        """强制 ZIP 必须是真 ZIP（不是 tar），且中文/emoji 文件名保留。"""
        from backupstudio import archives
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        monkeypatch.setattr(archives, "find_7z", lambda: None)  # 模拟没有 7-Zip
        src = _cjk_tree(tmp_path)
        dest = tmp_path / "out"
        task = BackupTask(
            name="中文", sources=[SourceConfig(str(src))], destination=str(dest),
            options=ArchiveOptions(fmt="zip", force=True),
        )
        result = TaskEngine().run(task)
        assert result.record.status == "success", result.record.errors
        arc = Path(result.record.archive)
        assert zipfile.is_zipfile(arc), "强制 ZIP 产出的不是真正的 ZIP"
        with zipfile.ZipFile(arc) as zf:
            names = zf.namelist()
            assert any("报告 2026😀" in n for n in names), names
            assert any(".hidden" in n for n in names), names

    def test_cancel_during_write(self, tmp_path, tmp_config):
        """写入阶段取消：状态为 skipped，且不留下半成品。"""
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        src = _cjk_tree(tmp_path)
        dest = tmp_path / "out"
        task = BackupTask(
            name="取消", sources=[SourceConfig(str(src))], destination=str(dest),
            options=ArchiveOptions(fmt="7z", force=True),
        )
        flag = {"cancel": False}
        engine = TaskEngine()

        def cancel_cb() -> bool:
            return flag["cancel"]

        # 在第一个进度回调后立刻取消
        def progress(payload: dict) -> None:
            if payload.get("done_bytes", 0) > 0:
                flag["cancel"] = True

        result = engine.run(task, progress_cb=progress, cancel=cancel_cb)
        assert result.record.status == "skipped"
        leftovers = [p.name for p in dest.glob("*") if p.name != "last-run.json"] if dest.exists() else []
        assert not leftovers, leftovers
