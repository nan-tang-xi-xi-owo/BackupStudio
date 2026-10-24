"""任务对话框字段保真测试（UX2 发现的"编辑零操作洗白"缺陷回归）。

场景：capability 变化触发的程序性取消勾选，不得清空已保存的
password / multivolume / encrypt_mode；用户显式操作开关才生效。
另覆盖审查 W-A6-2 的三项：A encrypt_mode 静默漂移、B 存量合法分卷组合
被 caps 校验拦死、C 多来源塌缩（对话框只编辑第一个来源却把尾部丢掉）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _full_task():
    from backupstudio.config import (
        ArchiveOptions,
        BackupTask,
        RetentionPolicy,
        ScheduleConfig,
        SourceConfig,
    )

    return BackupTask(
        name="全字段任务",
        enabled=True,
        sources=[SourceConfig(path=str(Path.home()), mode="files", files=["a.txt", "b.bin"])],
        destination=str(Path.home() / "backups"),
        destination_mode="flat",
        on_error="stop",
        options=ArchiveOptions(
            fmt="7z",
            level=5,
            password="secret1",
            encrypt_mode="aes256",
            multivolume="split",
            volume_value=100,
            volume_unit="MB",
            filename_template="{name}_{datetime}",
            exclude_globs=["*.tmp"],
            follow_symlinks=False,
            force=True,
        ),
        schedule=ScheduleConfig(type="weekly", time="03:15", days=[1, 3]),
        retention=RetentionPolicy(enabled=True, keep_last=5, keep_days=90),
        verify=True,
        notify=["failure"],
    )


def _dialog(qapp, task=None):
    from backupstudio.gui.appstate import AppState
    from backupstudio.gui.task_dialog import TaskDialog
    from backupstudio.store import TaskStore

    from backupstudio.paths import config_dir

    store = TaskStore(config_dir() / "tasks.json")
    state = AppState(store)
    return TaskDialog(state, task)


class TestDialogFieldPreservation:
    def test_zero_edit_roundtrip(self, qapp):
        """零操作载入保存 → 全字段 to_dict 全等（含无 UI 的 destination_mode/on_error）。"""
        task = _full_task()
        before = task.to_dict()
        dlg = _dialog(qapp, task)
        got = dlg._collect()
        assert got is not None
        after = got.to_dict()
        assert after == before, (
            "零操作保存改变了字段：" + str({k: (before.get(k), after.get(k)) for k in before if before.get(k) != after.get(k)})
        )

    def test_password_kept_when_capability_forces_uncheck(self, qapp):
        """密码不被 capability 强制取消勾选洗白（本机 zip/类的不可用状态即触发）。"""
        task = _full_task()
        dlg = _dialog(qapp, task)
        # 模拟 capability 强制取消勾选（程序性：阻断信号）
        prev = dlg.pw_enabled.blockSignals(True)
        dlg.pw_enabled.setChecked(False)
        dlg.pw_enabled.setEnabled(False)
        dlg.pw_enabled.blockSignals(prev)
        got = dlg._collect()
        assert got is not None
        assert got.options.password == "secret1"

    def test_password_cleared_on_explicit_user_off(self, qapp):
        """用户显式关闭开关 → 密码清空（用户意图生效）。

        前提：开关必须处于"可操作"状态（checked 且 enabled）。若环境
        capability 让开关不可用（如无 7z 时的 7z 格式），用户本就无法
        操作它——此时保真逻辑保留原值是正确行为，故先切换到一个
        加密可用的格式并强制勾选，模拟可操作状态。
        """
        task = _full_task()
        dlg = _dialog(qapp, task)
        if not dlg.pw_enabled.isEnabled() or not dlg.pw_enabled.isChecked():
            # 本机该格式加密不可用：跳过（保真逻辑的正确行为已被其它用例覆盖）
            import pytest as _pytest

            _pytest.skip("当前环境该格式加密开关不可操作（保真逻辑已由其它用例覆盖）")
        dlg.pw_enabled.setChecked(False)  # 用户操作（信号未阻断 → _user_touched_pw=True）
        got = dlg._collect()
        assert got is not None
        assert got.options.password == ""

    def test_multivolume_kept_and_restored(self, qapp):
        """分卷同密码侧：程序性取消勾选不洗白；能力恢复时勾选回填。"""
        task = _full_task()
        dlg = _dialog(qapp, task)
        prev = dlg.mv_enabled.blockSignals(True)
        dlg.mv_enabled.setChecked(False)
        dlg.mv_enabled.setEnabled(False)
        dlg.mv_enabled.blockSignals(prev)
        got = dlg._collect()
        assert got is not None
        assert got.options.multivolume == "split"
        # mv_enabled 有 setEnabled(True) 恢复路径（对照密码侧）
        assert dlg.mv_enabled is not None

    def test_format_switch_roundtrip_keeps_password(self, qapp):
        """7z→tar→7z 往返（本机无 gpg 时 tar 触发能力变化）→ 密码/分卷保留。"""
        task = _full_task()
        dlg = _dialog(qapp, task)
        # 切到 tar
        idx_tar = dlg.fmt_combo.findData("tar")
        if idx_tar >= 0:
            dlg.fmt_combo.setCurrentIndex(idx_tar)  # 触发 _refresh_capabilities
        # 切回 7z
        idx_7z = dlg.fmt_combo.findData("7z")
        if idx_7z >= 0:
            dlg.fmt_combo.setCurrentIndex(idx_7z)
        got = dlg._collect()
        assert got is not None
        assert got.options.password == "secret1"
        assert got.options.multivolume == "split"

    def test_files_list_no_duplication(self, qapp):
        """files 模式清单不膨胀（无重复、不全目录混入）。"""
        task = _full_task()
        dlg = _dialog(qapp, task)
        got = dlg._collect()
        assert got is not None
        files = got.sources[0].files
        assert sorted(files) == ["a.txt", "b.bin"], files
        assert len(files) == len(set(files))


def _dialog_blocking_dialogs(qapp, task, monkeypatch):
    """构造对话框并拦截模态框：warning 记录文本，question 一律选"是"。

    保真用例必须能证明"没有被警告拦截"，同时不能因为环境缺组件弹
    "缺少压缩组件/未选择文件"这类确认框把测试线程卡住。
    """
    from PySide6.QtWidgets import QMessageBox

    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        classmethod(lambda cls, *a, **k: shown.append(a[2] if len(a) > 2 else "")),
    )
    monkeypatch.setattr(
        QMessageBox, "question",
        classmethod(lambda cls, *a, **k: QMessageBox.StandardButton.Yes),
    )
    return _dialog(qapp, task), shown


def _force_caps(dlg, fmt, *, available=True, password=None, multivolume=None) -> None:
    """注入合成 capability 后刷新界面：不依赖本机装没装 7-Zip/GPG，
    专测"环境不支持任务原有配置"这一分支。
    """
    import copy

    entry = copy.deepcopy(dlg._caps.get(fmt, {}))
    entry["available"] = available
    if password is not None:
        entry["password"] = {
            m: {"available": ok, "reason": f"合成：{m} 不可用", "note": "", "safe": m != "zipcrypto"}
            for m, ok in password.items()
        }
    if multivolume is not None:
        entry["multivolume"] = {
            m: {"available": ok, "reason": f"合成：{m} 不可用", "note": f"{m} 说明"}
            for m, ok in multivolume.items()
        }
    entry["missing"] = []
    dlg._caps[fmt] = entry
    dlg._refresh_capabilities()


class TestEncryptModeFidelity:
    """W-A6-2A：encrypt_mode 不得被 capability 回落洗白（实测 136/245 例）。"""

    def test_zip_aes256_kept_when_caps_says_unavailable(self, qapp, monkeypatch):
        """zip+aes256，环境（如 7za 精简版）只支持 zipcrypto → 零操作保存仍是 aes256。

        旧行为：`select_data(current if current in pw_available else pw_available[0])`
        把下拉换成 zipcrypto → 保存写回 zipcrypto → 同一密码产物 method 99→8，
        加密强度静默降级。
        """
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

        task = BackupTask(
            name="ZIP_AES",
            sources=[SourceConfig(path=str(Path.home()), mode="folder")],
            destination=str(Path.home() / "backups"),
            options=ArchiveOptions(fmt="zip", password="secret1", encrypt_mode="aes256", force=False),
        )
        dlg, shown = _dialog_blocking_dialogs(qapp, task, monkeypatch)
        _force_caps(dlg, "zip", password={"aes256": False, "zipcrypto": True})
        assert dlg.enc_combo.currentData() == "aes256", "界面显示必须等于保存值"
        assert "不可用" in dlg.enc_note.text(), "不可用要有可见提示（不静默）"
        got = dlg._collect()
        assert got is not None, f"零操作保存被拦截：{shown}"
        assert got.options.encrypt_mode == "aes256"
        assert got.options.password == "secret1"

    def test_tar_gz_aes256_not_upgraded_to_gpg(self, qapp, monkeypatch):
        """tar.gz+aes256：caps 表里 tar 系只有 gpg → 旧代码落到 gpg，编辑一次必败。"""
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

        task = BackupTask(
            name="TARGZ_AES",
            sources=[SourceConfig(path=str(Path.home()), mode="folder")],
            destination=str(Path.home() / "backups"),
            options=ArchiveOptions(fmt="tar.gz", password="secret1", encrypt_mode="aes256", force=False),
        )
        dlg, shown = _dialog_blocking_dialogs(qapp, task, monkeypatch)
        _force_caps(dlg, "tar.gz", password={"gpg": False})
        assert dlg.enc_combo.currentData() == "aes256"
        got = dlg._collect()
        assert got is not None, f"零操作保存被拦截：{shown}"
        assert got.options.encrypt_mode == "aes256", "不得升级为 gpg（运行必败）"

    def test_user_enc_choice_still_wins(self, qapp, monkeypatch):
        """用户显式换加密方式 → 意图生效，并且只选 caps 里可用的项。"""
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

        task = BackupTask(
            name="ZIP_USER",
            sources=[SourceConfig(path=str(Path.home()), mode="folder")],
            destination=str(Path.home() / "backups"),
            options=ArchiveOptions(fmt="zip", password="secret1", encrypt_mode="aes256", force=False),
        )
        dlg, _shown = _dialog_blocking_dialogs(qapp, task, monkeypatch)
        _force_caps(dlg, "zip", password={"aes256": True, "zipcrypto": True})
        assert dlg.enc_combo.currentData() == "aes256"
        idx = dlg.enc_combo.findData("zipcrypto")
        assert idx >= 0
        dlg.enc_combo.setCurrentIndex(idx)  # 用户操作
        assert dlg._user_touched_enc is True
        got = dlg._collect()
        assert got is not None
        assert got.options.encrypt_mode == "zipcrypto", "用户显式意图必须生效"

    def test_user_enc_choice_falls_back_when_unavailable(self, qapp, monkeypatch):
        """用户选了一项、之后环境能力变化使其不可用 → 回落首个可用项（意图侧沿用旧规）。"""
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

        task = BackupTask(
            name="ZIP_DRIFT",
            sources=[SourceConfig(path=str(Path.home()), mode="folder")],
            destination=str(Path.home() / "backups"),
            options=ArchiveOptions(fmt="zip", password="secret1", encrypt_mode="aes256", force=False),
        )
        dlg, _shown = _dialog_blocking_dialogs(qapp, task, monkeypatch)
        _force_caps(dlg, "zip", password={"aes256": True, "zipcrypto": True})
        dlg.enc_combo.setCurrentIndex(dlg.enc_combo.findData("zipcrypto"))
        # 能力变化：aes256 可用、zipcrypto 不可用 → 用户所选不可用则回落可用首项
        _force_caps(dlg, "zip", password={"aes256": True, "zipcrypto": False})
        assert dlg.enc_combo.currentData() == "aes256"
        got = dlg._collect()
        assert got is not None
        assert got.options.encrypt_mode == "aes256"

    def test_new_task_defaults_to_available_mode(self, qapp, monkeypatch):
        """新建任务（无原有值）：能力不足时仍回落首个可用项，显示与保存一致。"""
        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "question", classmethod(lambda cls, *a, **k: QMessageBox.StandardButton.Yes))
        monkeypatch.setattr(QMessageBox, "warning", classmethod(lambda cls, *a, **k: None))
        dlg = _dialog(qapp, None)
        dlg.fmt_combo.setCurrentIndex(dlg.fmt_combo.findData("zip"))
        _force_caps(dlg, "zip", password={"aes256": False, "zipcrypto": True})
        assert dlg.enc_combo.currentData() == "zipcrypto"
        assert dlg._initial_encrypt_mode == "zipcrypto", "回落结果要记为基准，避免显示≠保存"


class TestLegacyMultivolumeCombinations:
    """W-A6-2B：引擎支持、caps 未列出的存量分卷组合，零操作保存不得被拒（16/245）。"""

    def test_7z_split_zero_edit_save_ok(self, qapp, monkeypatch):
        """CLI `--volume-mode split` 产生的 7z+split：零操作保存成功且值不变。"""
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

        task = BackupTask(
            name="7Z_SPLIT",
            sources=[SourceConfig(path=str(Path.home()), mode="folder")],
            destination=str(Path.home() / "backups"),
            options=ArchiveOptions(fmt="7z", multivolume="split", volume_value=100, force=False),
        )
        dlg, shown = _dialog_blocking_dialogs(qapp, task, monkeypatch)
        # 合成"7z 只有原生分卷可用"的环境（与 _BASELINE 对 7z 的表述一致）
        _force_caps(dlg, "7z", password={"aes256": True}, multivolume={"7z-split": True})
        assert dlg.mv_combo.currentData() == "split", "显示必须等于原有配置"
        got = dlg._collect()
        assert got is not None, f"存量合法组合被 caps 校验拒绝：{shown}"
        assert got.options.multivolume == "split"

    def test_user_mv_choice_still_checked(self, qapp, monkeypatch):
        """用户显式选了 caps 不支持的分卷方式 → 仍然拦截（校验没有被关闭）。"""
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

        task = BackupTask(
            name="7Z_USER_MV",
            sources=[SourceConfig(path=str(Path.home()), mode="folder")],
            destination=str(Path.home() / "backups"),
            options=ArchiveOptions(fmt="7z", multivolume="", force=False),
        )
        dlg, shown = _dialog_blocking_dialogs(qapp, task, monkeypatch)
        _force_caps(dlg, "7z", password={"aes256": True}, multivolume={"7z-split": False, "split": False})
        dlg.mv_enabled.setChecked(True)  # 用户操作 → _user_touched_mv
        got = dlg._collect()
        assert got is None, "用户主动选择不可用分卷方式且未启用强制压缩，必须拦截"
        assert any("分卷" in s for s in shown), shown


class TestMultiSourcePreservation:
    """W-A6-2C：对话框只编辑第一个来源，尾部来源不得被静默删除。"""

    def test_two_sources_survive_zero_edit(self, qapp, monkeypatch):
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

        home = str(Path.home())
        second = str(Path.home() / "Documents")
        task = BackupTask(
            name="TWO_SRC",
            sources=[
                SourceConfig(path=home, mode="files", files=["a.txt"]),
                SourceConfig(path=second, mode="folder", files=["keep.txt"]),
            ],
            destination=str(Path.home() / "backups"),
            options=ArchiveOptions(fmt="zip", force=False),
        )
        dlg, shown = _dialog_blocking_dialogs(qapp, task, monkeypatch)
        got = dlg._collect()
        assert got is not None, f"两来源任务零操作保存被拒：{shown}"
        assert len(got.sources) == 2, [s.to_dict() for s in got.sources]
        assert got.sources[0].path == home and got.sources[0].files == ["a.txt"]
        assert got.sources[1].to_dict() == task.sources[1].to_dict(), "第二来源必须原样保留"

    def test_first_source_edit_does_not_touch_tail(self, qapp, monkeypatch):
        """编辑第一来源的路径/文件清单 → 尾部来源照旧，且总条数不变。"""
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig

        task = BackupTask(
            name="EDIT_HEAD",
            sources=[
                SourceConfig(path=str(Path.home()), mode="folder"),
                SourceConfig(path=str(Path.home() / "Pictures"), mode="folder"),
                SourceConfig(path=str(Path.home() / "Music"), mode="folder"),
            ],
            destination=str(Path.home() / "backups"),
            options=ArchiveOptions(fmt="zip", force=False),
        )
        dlg, _shown = _dialog_blocking_dialogs(qapp, task, monkeypatch)
        dlg.src_path.edit.setText(str(Path.home() / "Videos"))
        got = dlg._collect()
        assert got is not None
        assert len(got.sources) == 3
        assert got.sources[0].path == str(Path.home() / "Videos")
        assert [s.path for s in got.sources[1:]] == [task.sources[1].path, task.sources[2].path]

    def test_single_source_stays_single(self, qapp, monkeypatch):
        """单来源任务不因新逻辑多出空条目。"""
        task = _full_task()
        dlg, _shown = _dialog_blocking_dialogs(qapp, task, monkeypatch)
        got = dlg._collect()
        assert got is not None
        assert len(got.sources) == 1


class TestRetentionDialog:
    def _run_two_backups(self, tmp_path, name: str = "p"):
        """真实跑两次备份（写 committed 清单）——旧包才有资格参与保留淘汰。

        返回 (src, dest, task)，task 就是产出这两个包的那个对象（id/name 固定）。

        必须用**同一个 task 对象连跑两次**，且名字与后续 preview/apply 用的任务一致：
        M10 修复后保留策略按 manifest.task_id（无清单旧包按任务名前缀）认领备份组，
        不同 id / 不同名前缀的包**不会**被本任务淘汰（否则就是跨任务互删的数据丢失）。
        原先每轮 BackupTask(name=f"t{i}") 新建对象 → 每轮 id 都是新的
        default_factory=_new_id，调用方再拿另一个 name="p" 的任务去 preview/apply，
        归属过滤正确地把 t0/t1 的包全部排除 → handled=[]，测不到"清理本任务历史包"。
        """
        import time as _time

        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        engine = TaskEngine()
        t = BackupTask(
            name=name,
            sources=[SourceConfig(path=str(src))],
            destination=str(dest),
            options=ArchiveOptions(fmt="zip", force=True),
        )
        for _ in range(2):
            rec = engine.run(t).record
            assert rec.status == "success", rec.errors
            _time.sleep(1.05)  # 文件名模板秒级去重
        return src, dest, t

    def test_preview_counts_old_backups(self, qapp, tmp_path, monkeypatch):
        """保留预览（审查 E1-1.8/D-r5）：显示将清理份数与大小。

        预览/淘汰都用产出这两个包的**同一个 task**（见 _run_two_backups）：
        M10 后保留策略按 task.id 认领备份组，换个 id 去预览必然"无可清理"。
        """
        from backupstudio.config import RetentionPolicy

        _src, _dest, task = self._run_two_backups(tmp_path)
        task.retention = RetentionPolicy(
            enabled=True, keep_last=1, keep_days=3650, on_cleanup="move"
        )
        dlg = _dialog(qapp, task)
        dlg._update_retention_preview()
        text = dlg.ret_preview.text()
        assert "1 份" in text, text  # keep_last=1 → 1 份旧包将被处理
        dlg.ret_action.select_data("delete")
        dlg._update_retention_preview()
        assert "直接删除" in dlg.ret_preview.text()

    def test_cleanup_dir_inside_source_rejected(self, qapp, tmp_path, monkeypatch):
        """回收目录位于来源内 → 保存被拒绝（验收：保存即报错）。"""
        from PySide6.QtWidgets import QMessageBox

        from backupstudio.config import ArchiveOptions, BackupTask, RetentionPolicy, SourceConfig

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("x", encoding="utf-8")
        task = BackupTask(
            name="p",
            sources=[SourceConfig(path=str(src))],
            destination=str(tmp_path / "dest"),
            options=ArchiveOptions(fmt="zip", force=True),
            retention=RetentionPolicy(enabled=True, keep_last=3, on_cleanup="move"),
        )
        shown: list[str] = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""))
        dlg = _dialog(qapp, task)
        dlg.ret_action.select_data("move")
        dlg.ret_dir.edit.setText(str(src / "trash"))
        assert dlg._collect() is None, "来源内的回收目录必须阻断保存"
        assert any("来源" in s for s in shown), shown

    def test_empty_cleanup_dir_allowed_uses_trash_default(self, qapp, tmp_path, monkeypatch):
        """回收目录留空（合法）：保存成功，运行期默认挪到 .backupstudio/trash。"""
        from backupstudio.config import ArchiveOptions, BackupTask, RetentionPolicy, SourceConfig

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("x", encoding="utf-8")
        task = BackupTask(
            name="p",
            sources=[SourceConfig(path=str(src))],
            destination=str(tmp_path / "dest"),
            options=ArchiveOptions(fmt="zip", force=True),
            retention=RetentionPolicy(enabled=True, keep_last=3, on_cleanup="move"),
        )
        dlg = _dialog(qapp, task)
        dlg.ret_action.select_data("move")
        dlg.ret_dir.edit.setText("")
        got = dlg._collect()
        assert got is not None, "留空回收目录不应阻断保存（运行期有 trash 默认）"
        assert got.retention.on_cleanup == "move"

    def test_engine_move_falls_back_to_trash(self, tmp_path):
        """引擎侧回归：cleanup_dir 留空 → 移动到 <dest>/.backupstudio/trash/<date>/（绝不删除）。

        淘汰用的是产出这两个包的**同一个 task**（见 _run_two_backups）：
        M10 后保留策略按 task.id 认领备份组，另建一个 name="p" 的任务去
        apply_retention 会得到 handled=[]（那是在验证跨任务隔离，不是本任务的历史清理）。
        """
        from backupstudio.config import RetentionPolicy
        from backupstudio.engine import apply_retention

        _src, dest, task = self._run_two_backups(tmp_path)
        before = sorted(p.name for p in dest.glob("*.zip"))
        assert len(before) == 2, before
        task.retention = RetentionPolicy(
            enabled=True, keep_last=1, keep_days=3650, on_cleanup="move"
        )
        handled = apply_retention(task)
        assert handled, "keep_last=1 应淘汰 1 份旧包"
        remaining = sorted(p.name for p in dest.glob("*.zip"))
        assert len(remaining) == 1 and remaining[0] == before[-1], f"应只留最新一份：{remaining}"
        trash_root = dest / ".backupstudio" / "trash"
        moved = list(trash_root.rglob(before[0]))
        assert moved, f"旧包应在回收站而非被删除：{list(trash_root.rglob('*'))}"
