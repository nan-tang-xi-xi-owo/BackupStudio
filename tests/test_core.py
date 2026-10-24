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
            # 无论走 7-Zip 还是内置 zipfile，产物都必须是真 ZIP 且含源文件
            assert zipfile.is_zipfile(result.record.archive), "zip 产物不是有效 ZIP"
            with zipfile.ZipFile(result.record.archive) as zf:
                assert "data/a.txt" in zf.namelist()
        elif fmt.startswith("tar") and fmt != "tar.zst":
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
        # 必须复用**同一个 task 对象（同一个 task.id）**连跑：这才是"同一任务保留最新
        # N 份"的产品语义（任务存在 tasks.json 里，id 固定）。
        # 反例：每轮 _make_task(...) 新建对象 → id 是 default_factory=_new_id，每轮都不同；
        # M10 修复后保留策略按 task_id 认领备份组，不同 id 之间不得互相淘汰
        # （否则 A 任务的 keep_last 会删掉 B 任务的包 = 数据丢失）。
        # 用不同 id 构造本用例，测到的是"跨任务互删"这一 bug 行为，而非单任务保留。
        task = _make_task("zip", dest, sample_tree, force=True)
        task.retention = RetentionPolicy(enabled=True, keep_last=2)
        # 让文件名模板含秒，避免重名
        task.options.filename_template = "{name}_{datetime}"
        for _ in range(4):
            result = engine.run(task)
            assert result.record.status == "success", result.record.errors
            time.sleep(1.05)
        zips = sorted(dest.glob("*.zip"))
        assert len(zips) == 2, zips
        # 淘汰的是最旧两轮：留下的文件名应排在最后
        assert zips[-1].stat().st_mtime >= zips[-2].stat().st_mtime

    def test_retention_isolated_across_tasks(self, sample_tree, tmp_path, tmp_config):
        """M10 回归锁（数据丢失级）：不同任务共享同一备份目录时，保留策略互不越界。

        真实场景：任务 A/B/C 都备份到同一个移动硬盘目录，各自 keep_last=2。
        修复前 find_backup_blobs 不按 task_id 过滤 → 交错跑 6 轮后总共只剩 2 份，
        B/C 的可用备份被 A 的策略删光。
        """
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "shared"
        engine = TaskEngine()
        names = ("m10a", "m10b", "m10c")
        tasks = []
        for name in names:
            t = _make_task("zip", dest, sample_tree, force=True)
            t.name = name
            t.options.filename_template = "{name}_{datetime}"
            t.retention = RetentionPolicy(enabled=True, keep_last=2)
            tasks.append(t)
        # 交错跑两轮（6 次备份）
        for _round in range(2):
            for t in tasks:
                assert engine.run(t).record.status == "success"
                time.sleep(1.05)
        remaining = sorted(p.name for p in dest.glob("*.zip"))
        by_task = {n: [p for p in remaining if p.startswith(n + "_")] for n in names}
        # 每个任务各留 2 份（共 6 份）：谁也没被别的任务删掉，也没多留
        assert all(len(v) == 2 for v in by_task.values()), by_task

    def test_retention_skipped_when_verification_fails(self, sample_tree, tmp_path, tmp_config, monkeypatch):
        """W-A1 回归锁（数据丢失级）：本轮校验失败时**不得**执行保留策略清理。

        修复前坏包会以 protected 身份占住 keep_last 配额，把上一轮已 committed 的
        好包挤掉——keep_last=1 的用户一次校验失败就丢光最后一份可恢复备份。
        """
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import TaskEngine

        dest = tmp_path / "wa1"
        t = _make_task("zip", dest, sample_tree, force=True)
        t.options.filename_template = "{name}_{datetime}"
        t.retention = RetentionPolicy(enabled=True, keep_last=1, on_cleanup="delete")
        eng = TaskEngine()

        # 第一轮：verify=False，产出唯一一份"好包"
        t.verify = False
        good = eng.run(t).record.archive
        assert good and Path(good).exists()
        good_name = Path(good).name
        time.sleep(1.05)

        # 第二轮：强制校验失败（模拟压缩包损坏/读不出来）
        import backupstudio.engine as E
        monkeypatch.setattr(E, "verify_archive", lambda *a, **k: (False, "模拟损坏"))
        t.verify = True
        bad = eng.run(t).record
        assert bad.status == "failure", (bad.status, bad.errors)
        assert bad.verification == "failed"
        assert bad.cleaned == [], f"失败轮次竟然执行了清理：{bad.cleaned}"

        # 上一轮的好包必须还在——这是"最后一份可恢复备份"
        assert (dest / good_name).exists(), (
            f"W-A1 复发：校验失败把唯一的好包 {good_name} 挤掉了（剩余 {sorted(p.name for p in dest.glob('*.zip'))}）"
        )

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


class TestNotify:
    def test_quiet_hours(self):
        from backupstudio.notify import in_quiet_hours

        assert in_quiet_hours(False, "23:00", "07:00") is False
        # 只要函数不抛异常即可（结果取决于当前时间）
        assert isinstance(in_quiet_hours(True, "00:00", "23:59"), bool)

    def test_backend_description(self):
        from backupstudio.notify import describe_backend

        assert describe_backend()


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
        # .backupstudio 是锁与清单的合法目录（非半成品残留）
        leftovers = [
            p.name for p in dest.glob("*")
            if p.name not in ("last-run.json", ".backupstudio")
        ] if dest.exists() else []
        assert not leftovers, leftovers

    def test_forced_rar_fallback_verifies_as_zip_container(self, sample_tree, tmp_path, tmp_config):
        """强制 RAR 降级产物是 **ZIP 容器**（扩展名 .rar），校验必须按真实容器走。

        背景：``find_unrar()`` 修好之后（此前它找不到随包的 ``tools/win32/rar/UnRAR.exe``），
        校验会拿 ``unrar t`` 去打这个 ZIP 文件 → "RAR 测试失败（退出码 13）"，
        把好好的备份判成损坏。``ArchiveResult.container_fmt`` 负责账实相符。
        """
        from backupstudio.archives import rar_available
        from backupstudio.engine import TaskEngine

        if rar_available():
            pytest.skip("本机 RAR 可创建压缩包（走原生路径，不涉及降级）")

        dest = tmp_path / "rarzip"
        result = TaskEngine().run(_make_task("rar", dest, sample_tree, force=True))
        rec = result.record
        assert rec.status == "success", rec.errors
        assert rec.verification == "passed", rec.verification_message

        # 产物确实是 ZIP 容器（magic PK），但扩展名保持 .rar
        arc = Path(rec.archive)
        assert arc.suffix.lower() == ".rar", arc
        assert arc.read_bytes()[:2] == b"PK", "强制 RAR 降级应产出 ZIP 容器"
        # 真实容器格式如实记录
        assert result.result is not None
        assert result.result.container_fmt == "zip", result.result.container_fmt


class TestP1BStem:
    """W-A2-2（S1）：manifest 的 stem 必须由后端正向给出，不得从产物名反推。"""

    def test_product_stem_tail_suffix_only(self):
        """主名推导只做**尾部**卷后缀剥离：任务名含扩展名时 stem = 完整文件名。"""
        from backupstudio.archives import product_stem

        cases = {
            # 任务名本身含扩展名（模板 {name}_{datetime}）→ 必须整名唯一，不得截成 "foo.zip"
            "foo.zip_20261023_042116.zip": "foo.zip_20261023_042116.zip",
            "notes.tar.gz_20261023_042116.zip": "notes.tar.gz_20261023_042116.zip",
            "v1.0_20261023.zip": "v1.0_20261023.zip",
            # 常规整包：stem 就是文件名
            "Documents_20261023_042116.zip": "Documents_20261023_042116.zip",
            # 分卷：剥掉**尾部**卷号，主名保留容器扩展名
            "X.zip.001": "X.zip",
            "X.7z.012": "X.7z",
            "X.zip.part01": "X.zip",
            "X.tar.gz.part02": "X.tar.gz",
        }
        assert {k: product_stem(k) for k in cases} == cases

    def test_manifest_one_per_archive_when_task_name_has_extension(self, sample_tree, tmp_path, tmp_config):
        """复现 W-A2-2 实测场景：任务名 `foo.zip` 连跑 2 次 → 2 个独立 manifest、各 volumes=1。

        修复前：engine 用 `str.find(ext)` 反推主名，命中的是任务名里的第一个 `.zip`
        → 两轮都写 `foo.zip.json`，第二轮覆盖第一轮（实测：2 产物 1 清单）。
        """
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine
        from backupstudio.manifest import load_manifests

        dest = tmp_path / "stem"
        task = BackupTask(
            name="foo.zip",
            sources=[SourceConfig(path=str(sample_tree))],
            destination=str(dest),
            options=ArchiveOptions(fmt="zip", force=True, filename_template="{name}_{datetime}"),
        )
        eng = TaskEngine()
        names = []
        for _ in range(2):
            rec = eng.run(task).record
            assert rec.status == "success", rec.errors
            names.append(Path(rec.archive).name)
            time.sleep(1.05)
        assert len(set(names)) == 2, names

        ms = load_manifests(dest)
        assert len(ms) == 2, f"manifest 数应为 2（一包一清单），实际 {sorted(ms)}"
        assert all(len(m.volumes) == 1 for m in ms.values()), [m.volumes for m in ms.values()]
        # 清单键（stem）等于产物文件名本身——而不是被扩展名截断的任务名
        assert set(ms) == set(names), f"{sorted(ms)} vs {sorted(names)}"
        assert sorted(p.name for p in dest.glob("*.zip")) == sorted(names)

    def test_manifest_write_failure_warning_is_actionable(self, sample_tree, tmp_path, tmp_config, monkeypatch):
        """W-A2-4（S3）：清单写入失败的告警必须说清后果（无清单 → 保留策略不会自动清理）。"""
        import backupstudio.manifest as MF
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        def boom(*a, **k):
            raise PermissionError("模拟写清单失败")

        monkeypatch.setattr(MF, "write_manifest", boom)
        dest = tmp_path / "mfwarn"
        task = BackupTask(
            name="warn", sources=[SourceConfig(path=str(sample_tree))], destination=str(dest),
            options=ArchiveOptions(fmt="zip", force=True),
        )
        rec = TaskEngine().run(task).record
        assert rec.status == "success", rec.errors  # 备份本身不受影响（语义不变）
        joined = " ".join(rec.warnings)
        assert "写入备份清单失败" in joined, rec.warnings
        assert "无清单" in joined and "不会自动清理" in joined, f"告警不可行动：{rec.warnings}"


class TestP1BRetention:
    """M11（S2）：本轮新组必须参与 keep_last 计数（7z 原生分卷稳态多留 1 组）。"""

    def test_protected_group_counts_toward_keep_last(self, tmp_path):
        """合成"上一轮已提交 + 本轮新卷无清单"场景：keep_last=1 → 只剩 1 组。

        修复前：`.001` 命名的本轮新组既没有 manifest（保留策略早于清单提交）、又不被
        collect_groups 的后缀启发式识别 → blobs 少算一组 → `len(blobs) > keep_last`
        不成立 → 旧组留着，稳态 2 组。
        """
        from datetime import datetime, timedelta

        from backupstudio.config import ArchiveOptions, BackupTask, RetentionPolicy, SourceConfig
        from backupstudio.engine import apply_retention
        from backupstudio.manifest import STATE_COMMITTED, Manifest, load_manifests, write_manifest

        dest = tmp_path / "m11"
        dest.mkdir()
        # 上一轮：committed manifest 组（native 分卷 3 卷）
        old_vols = ["nv_old.7z.001", "nv_old.7z.002", "nv_old.7z.003"]
        old_ts = (datetime.now().astimezone() - timedelta(days=3)).isoformat(timespec="seconds")
        for n in old_vols:
            p = dest / n
            p.write_bytes(b"a" * 64)
        write_manifest(
            dest,
            Manifest(stem="nv_old.7z", task_id="m11-task", task_name="nv", fmt="7z",
                     state=STATE_COMMITTED, volumes=old_vols, committed_at=old_ts),
        )
        # 本轮新组：文件已就位，manifest 还没写（引擎此刻尚未进入步骤 8）
        new_vols = ["nv_new.7z.001", "nv_new.7z.002"]
        for n in new_vols:
            (dest / n).write_bytes(b"b" * 64)

        t = BackupTask(
            name="nv", sources=[SourceConfig(str(tmp_path / "src"))], destination=str(dest),
            options=ArchiveOptions(fmt="7z", force=True, filename_template="{name}_{datetime}"),
        )
        t.id = "m11-task"
        t.retention = RetentionPolicy(enabled=True, keep_last=1, on_cleanup="delete")

        handled = apply_retention(t, protected=[str(dest / n) for n in new_vols])
        handled_names = {Path(h).name for h in handled}
        assert handled_names == set(old_vols), f"旧组应整组淘汰（本轮新组占配额）：{handled}"
        for n in old_vols:
            assert not (dest / n).exists(), n
        for n in new_vols:
            assert (dest / n).exists(), f"protected 的新卷被删了：{n}"
        # 清单同步清掉，不留"清单在、卷不在"的空组
        assert load_manifests(dest) == {}

    def test_keep_last_one_end_to_end_three_rounds(self, tmp_path):
        """端到端稳态：keep_last=1 连跑 3 轮 → 目录里只剩 1 组（修复不得反向多删）。

        合成用例证明"差一被消除"，本用例证明"没改成多删一组"：protected 自身必须存活。
        """
        from backupstudio.config import ArchiveOptions, BackupTask, RetentionPolicy, SourceConfig
        from backupstudio.engine import TaskEngine

        src = tmp_path / "data"
        src.mkdir()
        (src / "a.bin").write_bytes(os.urandom(40 * 1024))
        dest = tmp_path / "m11e2e"
        t = BackupTask(
            name="e2e", sources=[SourceConfig(path=str(src))], destination=str(dest),
            options=ArchiveOptions(fmt="zip", force=True, filename_template="{name}_{datetime}"),
        )
        t.retention = RetentionPolicy(enabled=True, keep_last=1, on_cleanup="delete")
        eng = TaskEngine()
        for i in range(3):
            rec = eng.run(t).record
            assert rec.status == "success", rec.errors
            time.sleep(1.05)
        zips = sorted(p.name for p in dest.glob("*.zip"))
        assert len(zips) == 1, f"keep_last=1 稳态应只剩 1 组：{zips}"

    def test_protected_already_visible_group_not_double_counted(self, tmp_path):
        """同一组已在 blobs 里可见时，不得重复计数导致多删一组。"""
        from backupstudio.config import ArchiveOptions, BackupTask, RetentionPolicy, SourceConfig
        from backupstudio.engine import apply_retention

        dest = tmp_path / "m11b"
        dest.mkdir()
        old = time.time() - 3 * 86400
        for n in ("keep_a.zip", "keep_b.zip"):
            p = dest / n
            p.write_bytes(b"PK\x03\x04" + b"x" * 64)
            os.utime(p, (old, old))
        t = BackupTask(
            name="keep", sources=[SourceConfig(str(tmp_path))], destination=str(dest),
            options=ArchiveOptions(fmt="zip", force=True),
        )
        t.retention = RetentionPolicy(enabled=True, keep_last=2, on_cleanup="delete")
        # protected 指向其中一包所属的组（无清单旧包路径）：include_legacy=False → 不淘汰
        handled = apply_retention(t, protected=[str(dest / "keep_a.zip")])
        assert handled == [], f"候选只有 2 组、keep_last=2，不该删任何东西：{handled}"
        assert (dest / "keep_a.zip").exists() and (dest / "keep_b.zip").exists()


class TestP1BSweepOrphans:
    """M23（S2）：`.bs-tmp-*` 与 `bs-stage-*` 崩溃残留的启动清扫。"""

    def test_sweep_removes_dead_pid_only(self, tmp_path):
        import backupstudio.engine as E
        from backupstudio.engine import sweep_orphans

        import psutil

        dest = tmp_path / "bak"
        dest.mkdir()
        # ① 死 pid 的残包：动态找一个"当前确实不存在"的 pid（固定值有极低概率撞上活进程，
        #    撞上就会让用例随机失败——实证优先，不靠猜）
        dead_pid = 999999
        for cand in (999999, 999998, 888888, 777777, 400000, 300000):
            if not psutil.pid_exists(cand):
                dead_pid = cand
                break
        dead = dest / f"T.zip.bs-tmp-{dead_pid}"
        dead.write_bytes(b"x" * 128)
        old = time.time() - 600  # 已过 5 分钟闸门
        os.utime(dead, (old, old))
        # ② 本进程 pid 的活体残包：绝不能删（正在写的备份）
        alive = dest / f"T2.zip{E.TMP_MARK}{os.getpid()}"
        alive.write_bytes(b"y" * 128)
        # ③ 归档本体 / 清单目录 / 用户文件：一个都不许碰
        archive = dest / "good.zip"
        archive.write_bytes(b"PK\x03\x04" + b"z" * 128)
        mdir = dest / ".backupstudio" / "manifests"
        mdir.mkdir(parents=True)
        (mdir / "good.zip.json").write_text('{"stem": "good.zip"}', encoding="utf-8")
        other = dest / "readme.txt"
        other.write_text("keep me", encoding="utf-8")

        removed = sweep_orphans([dest])
        assert [p.name for p in removed] == [dead.name], removed
        assert not dead.exists()
        assert alive.exists(), "活 pid 的临时产物被删（会毁掉正在进行的备份）"
        assert archive.exists() and other.exists() and (mdir / "good.zip.json").exists()

    def test_sweep_stage_dirs_by_age(self, tmp_path):
        from backupstudio.engine import sweep_orphans

        dest = tmp_path / "bak2"
        dest.mkdir()
        fresh_stage = dest / "bs-stage-fresh01"
        fresh_stage.mkdir()
        (fresh_stage / "staging.tar").write_bytes(b"x" * 32)
        old_stage = dest / "bs-stage-old99"
        old_stage.mkdir()
        (old_stage / "staging.tar").write_bytes(b"y" * 32)
        very_old = time.time() - 25 * 3600
        os.utime(old_stage, (very_old, very_old))

        removed = sweep_orphans([dest])
        assert [p.name for p in removed] == [old_stage.name], removed
        assert not old_stage.exists()
        assert dest.exists(), "备份目录本体不能被删"
        assert fresh_stage.exists(), "未满 24h 的暂存目录不得删除（可能是别的进程正在写）"

    def test_sweep_covers_bs_staging_subdir(self, tmp_path):
        """staging_parent 的第二落点 `<dest>/.bs-staging/` 也必须扫（最常见崩溃残留处）。"""
        from backupstudio.engine import sweep_orphans

        dest = tmp_path / "bak4"
        sub = dest / ".bs-staging"
        sub.mkdir(parents=True)
        old_stage = sub / "bs-stage-deadbeef"
        old_stage.mkdir()
        (old_stage / "staging.tar").write_bytes(b"x" * 32)
        very_old = time.time() - 25 * 3600
        os.utime(old_stage, (very_old, very_old))
        # 同目录里放一个未满 24h 的：不许动
        fresh = sub / "bs-stage-live1234"
        fresh.mkdir()
        (fresh / "unpacked").mkdir()
        (fresh / "unpacked" / "f.txt").write_text("z", encoding="utf-8")

        removed = sweep_orphans([dest])
        assert [q.name for q in removed] == [old_stage.name], removed
        assert not old_stage.exists()
        assert fresh.exists()
        assert (dest / ".bs-staging").exists()

    def test_sweep_covers_system_temp_stage_only(self, tmp_path, monkeypatch):
        """系统 temp（mkdtemp 的兜底落点）只清 `bs-stage-*`，不碰 `.bs-tmp-*` 命名的他人文件。"""
        import tempfile as _tf

        from backupstudio.engine import TMP_MARK, sweep_orphans

        fake_tmp = tmp_path / "systmp"
        fake_tmp.mkdir()
        bak = tmp_path / "bak"
        bak.mkdir()
        stage = fake_tmp / "bs-stage-systmp01"
        stage.mkdir()
        (stage / "staging.tar").write_bytes(b"x" * 16)
        os.utime(stage, (time.time() - 25 * 3600, time.time() - 25 * 3600))
        # 别的程序在系统 temp 里的同名形态文件：必须原样保留
        stranger = fake_tmp / f"someapp.dat{TMP_MARK}999999"
        stranger.write_bytes(b"keep")
        os.utime(stranger, (time.time() - 25 * 3600, time.time() - 25 * 3600))
        monkeypatch.setattr(_tf, "gettempdir", lambda: str(fake_tmp))

        removed = sweep_orphans([bak])
        assert [q.name for q in removed] == [stage.name], removed
        assert stranger.exists(), "系统 temp 里非本产品的文件被删了"

    def test_sweep_tolerates_bad_paths(self, tmp_path):
        """清扫必须"永不抛"：不存在的路径、空值、文件路径都不能炸启动流程。"""
        from backupstudio.engine import sweep_orphans

        f = tmp_path / "afile"
        f.write_text("x", encoding="utf-8")
        assert sweep_orphans([tmp_path / "nope", "", None, f]) == []

    def test_sweep_never_touches_manifests_or_archives(self, tmp_path):
        """安全边界：.backupstudio（清单/锁目录）与任何归档名都不动。"""
        from backupstudio.engine import sweep_orphans

        dest = tmp_path / "bak3"
        (dest / ".backupstudio" / "manifests").mkdir(parents=True)
        (dest / ".backupstudio" / "manifests" / "x.json").write_text("{}", encoding="utf-8")
        # 故意做成"超龄 + 看起来像残包"的文件名，但不在标记内
        for n in ("archive.zip", "foo.txt", "last-run.json", "data.7z.001"):
            p = dest / n
            p.write_bytes(b"x" * 16)
            os.utime(p, (time.time() - 30 * 86400, time.time() - 30 * 86400))
        assert sweep_orphans([dest]) == []
        assert all((dest / n).exists() for n in ("archive.zip", "foo.txt", "last-run.json", "data.7z.001"))
        assert (dest / ".backupstudio" / "manifests" / "x.json").exists()
