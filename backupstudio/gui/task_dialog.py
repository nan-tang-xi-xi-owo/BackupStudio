"""新建 / 编辑备份任务的对话框（来源、格式、密码、分卷、计划、保留策略、通知）。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..capabilities import (
    ENCRYPTION_LABELS,
    FORMAT_LABELS,
    MULTIVOLUME_LABELS,
    environment_report,
    format_capabilities,
    format_status_line,
    missing_requirements,
)
from ..config import (
    ENCRYPTION_MODES,
    MULTIVOLUME_MODES,
    ArchiveOptions,
    BackupTask,
    RetentionPolicy,
    ScheduleConfig,
    SourceConfig,
)
from ..engine import archive_path
from ..util import normalize_path
from .appstate import AppState
from .widgets import Badge, Card, CollapsibleSection, Combo, PathEdit, Switch, muted, title_label

log = logging.getLogger("backupstudio.gui.task_dialog")

LEVEL_TEXT = {0: "仅存储（最快）", 1: "1 · 最快", 3: "3 · 较快", 5: "5 · 标准", 7: "7 · 较好", 9: "9 · 最强"}

_WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class TaskDialog(QDialog):
    """任务编辑对话框。"""

    def __init__(self, state: AppState, task: BackupTask | None, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.state = state
        self._original = task
        self._task = BackupTask.from_dict(task.to_dict()) if task else BackupTask(name="")
        self._dark = state.palette.dark
        self._caps = format_capabilities()
        self._env = environment_report()
        self.setWindowTitle("编辑备份任务" if task else "新建备份任务")
        self.setMinimumSize(860, 720)
        self.resize(900, 780)
        self.setSizeGripEnabled(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(title_label("编辑备份任务" if task else "新建备份任务", 1), 1)
        self.head_badge = Badge("", "muted")
        self.head_badge.set_dark(self._dark)
        head.addWidget(self.head_badge)
        outer.addLayout(head)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self._build_source_tab()), "来源与位置")
        self.tabs.addTab(self._scroll(self._build_format_tab()), "压缩与加密")
        self.tabs.addTab(self._scroll(self._build_schedule_tab()), "计划与保留")
        outer.addWidget(self.tabs, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        self.summary.setWordWrap(True)
        outer.addWidget(self.summary)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_btn.setText("保存任务")
        save_btn.setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._load_task()
        self._refresh_capabilities()

    # ------------------------------------------------------------------
    @staticmethod
    def _scroll(content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        return area

    # ------------------------------------------------------------------
    # Tab 1: 来源与位置
    # ------------------------------------------------------------------
    def _build_source_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 8)
        lay.setSpacing(14)

        basic = Card()
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：工作文档 / 照片库")
        form.addRow("任务名称", self.name_edit)
        self.enabled_switch = Switch(dark=self._dark)
        row = QHBoxLayout()
        row.addWidget(self.enabled_switch)
        row.addWidget(muted("停用后不会按计划自动执行，仍可手动备份"))
        row.addStretch(1)
        form.addRow("启用任务", self._wrap(row))
        basic.add_layout(form)
        lay.addWidget(basic)

        src_card = Card()
        src_card.add(title_label("备份来源", 3))
        self.source_mode = Combo()
        self.source_mode.addItem("整个文件夹", "folder")
        self.source_mode.addItem("文件夹里指定的文件 / 子文件夹", "files")
        self.source_mode.currentIndexChanged.connect(self._on_source_mode)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("模式"))
        row2.addWidget(self.source_mode, 1)
        src_card.add_layout(row2)

        self.src_path = PathEdit(mode="dir", placeholder="要备份的文件夹", caption="选择要备份的文件夹")
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("文件夹"))
        row3.addWidget(self.src_path, 1)
        src_card.add_layout(row3)

        self.src_files_box = QWidget()
        fb = QVBoxLayout(self.src_files_box)
        fb.setContentsMargins(0, 6, 0, 0)
        fb.setSpacing(8)
        hint = muted("从上面选择的文件夹中挑选要备份的文件或子文件夹（支持多选；全选即等同整个文件夹）。")
        fb.addWidget(hint)
        self.src_files = QListWidget()
        self.src_files.setMinimumHeight(170)
        self.src_files.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        fb.addWidget(self.src_files)
        btns = QHBoxLayout()
        b_pick = QPushButton("添加文件…")
        b_pick.clicked.connect(self._pick_files)
        b_dir = QPushButton("添加子文件夹…")
        b_dir.clicked.connect(self._pick_subdirs)
        b_selall = QPushButton("全选")
        b_selall.clicked.connect(self._select_all_files)
        b_clear = QPushButton("清空")
        b_clear.clicked.connect(self._clear_files)
        for b in (b_pick, b_dir, b_selall, b_clear):
            btns.addWidget(b)
        btns.addStretch(1)
        fb.addLayout(btns)
        self.src_files_box.setVisible(False)
        src_card.add(self.src_files_box)
        lay.addWidget(src_card)

        dest_card = Card()
        dest_card.add(title_label("备份位置", 3))
        self.dest_path = PathEdit(mode="dir", placeholder="备份文件保存到哪里（可以是另一块硬盘 / 网络位置）", caption="选择备份存放目录")
        dest_card.add(self.dest_path)
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("文件名模板"))
        self.template = QLineEdit()
        self.template.setPlaceholderText("{name}_{datetime}")
        self.template.textChanged.connect(self._update_summary)
        dest_row.addWidget(self.template, 1)
        dest_card.add_layout(dest_row)
        dest_card.add(muted("可用变量：{name} 任务名 · {date} 日期 · {time} 时间 · {datetime} 日期时间"))
        self.path_preview = QLabel("")
        self.path_preview.setStyleSheet("font-family: 'Cascadia Mono','Consolas',monospace; font-size: 12px;")
        dest_card.add(self.path_preview)
        self.dest_warning = QLabel("")
        self.dest_warning.setObjectName("WarningText")
        self.dest_warning.setWordWrap(True)
        dest_card.add(self.dest_warning)
        lay.addWidget(dest_card)

        adv = CollapsibleSection("高级选项")
        adv_body = Card(flat=True)
        form2 = QFormLayout()
        form2.setVerticalSpacing(10)
        self.exclude_edit = QLineEdit()
        self.exclude_edit.setPlaceholderText("*.tmp, node_modules, .git, *.iso")
        form2.addRow("排除规则", self.exclude_edit)
        self.follow_links = QCheckBox("跟随符号链接（可能会打包到链接指向的其它目录）")
        form2.addRow("", self.follow_links)
        adv_body.add_layout(form2)
        adv.add(adv_body)
        lay.addWidget(adv)
        lay.addStretch(1)
        return page

    def _wrap(self, layout) -> QWidget:  # noqa: ANN001
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        return w

    def _on_source_mode(self) -> None:
        files_mode = self.source_mode.currentData() == "files"
        self.src_files_box.setVisible(files_mode)
        if files_mode:
            self._reload_file_list()

    def _reload_file_list(self) -> None:
        base = Path(normalize_path(self.src_path.path())) if self.src_path.path() else None
        self.src_files.clear()
        if base is None or not base.is_dir():
            return
        try:
            entries = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for p in entries[:5000]:
            item = QListWidgetItem(("📄 " if p.is_file() else "📁 ") + p.name)
            item.setData(Qt.ItemDataRole.UserRole, p.name)
            item.setToolTip(str(p))
            if p.is_dir():
                item.setForeground(Qt.GlobalColor.gray)
            self.src_files.addItem(item)

    def _pick_files(self) -> None:
        base = normalize_path(self.src_path.path())
        if not base or not Path(base).is_dir():
            QMessageBox.information(self, "提示", "请先选择要备份的文件夹。")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要备份的文件", base, "所有文件 (*)")
        for p in paths:
            try:
                rel = str(Path(p).relative_to(base)).replace("\\", "/")
            except ValueError:
                continue
            if not any(self.src_files.item(i).data(Qt.ItemDataRole.UserRole) == rel for i in range(self.src_files.count())):
                item = QListWidgetItem("📄 " + rel)
                item.setData(Qt.ItemDataRole.UserRole, rel)
                self.src_files.addItem(item)

    def _pick_subdirs(self) -> None:
        base = normalize_path(self.src_path.path())
        if not base or not Path(base).is_dir():
            QMessageBox.information(self, "提示", "请先选择要备份的文件夹。")
            return
        d = QFileDialog.getExistingDirectory(self, "选择要备份的子文件夹", base)
        if not d:
            return
        try:
            rel = str(Path(d).relative_to(base)).replace("\\", "/")
        except ValueError:
            return
        if rel in (".", ""):
            return
        if not any(self.src_files.item(i).data(Qt.ItemDataRole.UserRole) == rel for i in range(self.src_files.count())):
            item = QListWidgetItem("📁 " + rel)
            item.setData(Qt.ItemDataRole.UserRole, rel)
            self.src_files.addItem(item)

    def _select_all_files(self) -> None:
        self._reload_file_list()

    def _clear_files(self) -> None:
        self.src_files.clear()

    def _selected_files(self) -> list[str]:
        result: list[str] = []
        for i in range(self.src_files.count()):
            rel = self.src_files.item(i).data(Qt.ItemDataRole.UserRole)
            if rel:
                result.append(rel)
        return result

    # ------------------------------------------------------------------
    # Tab 2: 压缩与加密
    # ------------------------------------------------------------------
    def _build_format_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 8)
        lay.setSpacing(14)

        fmt_card = Card()
        fmt_card.add(title_label("压缩格式", 3))
        self.fmt_combo = Combo()
        for fmt, label in FORMAT_LABELS.items():
            self.fmt_combo.addItem(label, fmt)
        self.fmt_combo.currentIndexChanged.connect(self._on_format_changed)
        fr = QHBoxLayout()
        fr.addWidget(QLabel("格式"))
        fr.addWidget(self.fmt_combo, 1)
        fmt_card.add_layout(fr)
        self.fmt_status = QLabel("")
        self.fmt_status.setWordWrap(True)
        fmt_card.add(self.fmt_status)
        self.fmt_missing = QWidget()
        self.fmt_missing_lay = QVBoxLayout(self.fmt_missing)
        self.fmt_missing_lay.setContentsMargins(0, 4, 0, 0)
        self.fmt_missing_lay.setSpacing(4)
        fmt_card.add(self.fmt_missing)
        lay.addWidget(fmt_card)

        # 压缩级别
        lvl_card = Card()
        lvl_card.add(title_label("压缩强度", 3))
        lr = QHBoxLayout()
        self.level_slider = QSlider(Qt.Orientation.Horizontal)
        self.level_slider.setRange(0, 9)
        self.level_slider.setSingleStep(1)
        self.level_slider.setPageStep(2)
        self.level_slider.valueChanged.connect(self._on_level_changed)
        self.level_label = QLabel("")
        self.level_label.setMinimumWidth(120)
        lr.addWidget(self.level_slider, 1)
        lr.addWidget(self.level_label)
        lvl_card.add_layout(lr)
        lvl_card.add(muted("0 = 仅打包不压缩（最快）；9 = 最强压缩（最慢，CPU 占用高）"))
        self.level_warn = QLabel("")
        self.level_warn.setObjectName("WarningText")
        self.level_warn.setWordWrap(True)
        lvl_card.add(self.level_warn)
        lay.addWidget(lvl_card)

        # 密码
        pw_card = Card()
        pw_card.add(title_label("密码保护", 3))
        self.pw_enabled = Switch(dark=self._dark)
        row = QHBoxLayout()
        row.addWidget(self.pw_enabled)
        row.addWidget(muted("为压缩包设置口令（请务必自行保存好，忘记后无法恢复）"))
        row.addStretch(1)
        pw_card.add_layout(row)
        self.pw_widget = QWidget()
        pf = QFormLayout(self.pw_widget)
        pf.setContentsMargins(0, 6, 0, 0)
        pf.setVerticalSpacing(10)
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_edit.setPlaceholderText("口令")
        self.pw_edit2 = QLineEdit()
        self.pw_edit2.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_edit2.setPlaceholderText("再输入一次")
        show_btn = QPushButton("显示")
        show_btn.setCheckable(True)
        show_btn.toggled.connect(self._toggle_pw_visible)
        prow = QHBoxLayout()
        prow.addWidget(self.pw_edit, 1)
        prow.addWidget(show_btn)
        pf.addRow("密码", self._wrap(prow))
        pf.addRow("确认密码", self.pw_edit2)
        self.enc_combo = Combo()
        for mode in ENCRYPTION_MODES:
            self.enc_combo.addItem(ENCRYPTION_LABELS.get(mode, mode), mode)
        self.enc_combo.currentIndexChanged.connect(self._refresh_capabilities)
        pf.addRow("加密方式", self.enc_combo)
        self.enc_note = QLabel("")
        self.enc_note.setWordWrap(True)
        self.enc_note.setObjectName("Muted")
        pf.addRow("", self.enc_note)
        pw_card.add(self.pw_widget)
        self.pw_widget.setVisible(False)
        self.pw_enabled.toggled.connect(self.pw_widget.setVisible)
        lay.addWidget(pw_card)

        # 分卷
        mv_card = Card()
        mv_card.add(title_label("分卷压缩", 3))
        self.mv_enabled = Switch(dark=self._dark)
        row = QHBoxLayout()
        row.addWidget(self.mv_enabled)
        row.addWidget(muted("把压缩包拆成多个小文件（方便拷贝到 U 盘 / 网盘）"))
        row.addStretch(1)
        mv_card.add_layout(row)
        self.mv_widget = QWidget()
        mf = QFormLayout(self.mv_widget)
        mf.setContentsMargins(0, 6, 0, 0)
        mf.setVerticalSpacing(10)
        size_row = QHBoxLayout()
        self.vol_value = QSpinBox()
        self.vol_value.setRange(1, 1024 * 100)
        self.vol_value.setValue(100)
        self.vol_unit = Combo()
        for u in ("KB", "MB", "GB"):
            self.vol_unit.addItem(u, u)
        self.vol_unit.setCurrentIndex(1)
        size_row.addWidget(self.vol_value)
        size_row.addWidget(self.vol_unit)
        size_row.addStretch(1)
        mf.addRow("每卷大小", self._wrap(size_row))
        self.mv_combo = Combo()
        for mode in MULTIVOLUME_MODES:
            self.mv_combo.addItem(MULTIVOLUME_LABELS.get(mode, mode), mode)
        self.mv_combo.currentIndexChanged.connect(self._refresh_capabilities)
        mf.addRow("分卷方式", self.mv_combo)
        self.mv_note = QLabel("")
        self.mv_note.setWordWrap(True)
        self.mv_note.setObjectName("Muted")
        mf.addRow("", self.mv_note)
        mv_card.add(self.mv_widget)
        self.mv_widget.setVisible(False)
        self.mv_enabled.toggled.connect(self.mv_widget.setVisible)
        lay.addWidget(mv_card)

        # 强制
        force_card = Card()
        row = QHBoxLayout()
        self.force_switch = Switch(dark=self._dark)
        row.addWidget(self.force_switch)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(QLabel("强制压缩（忽略缺失的软件）"))
        col.addWidget(muted("当系统缺少该格式所需的压缩软件时，尽量用内置引擎生成，可能改变容器格式（会在结果中提示）"))
        row.addLayout(col, 1)
        force_card.add_layout(row)
        lay.addWidget(force_card)
        lay.addStretch(1)
        return page

    def _toggle_pw_visible(self, checked: bool) -> None:  # noqa: ANN001
        mode = QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        self.pw_edit.setEchoMode(mode)
        self.pw_edit2.setEchoMode(mode)

    def _on_level_changed(self, value: int) -> None:
        self.level_label.setText(LEVEL_TEXT.get(value, str(value)))
        heavy = value >= 8
        self.level_warn.setText(
            "⚠ 高压缩级别会占用大量 CPU 和内存（大容量 CPU 压缩可能要数小时），建议仅对重要且不常变动的数据使用。"
            if heavy else ""
        )

    def _on_format_changed(self) -> None:
        self._refresh_capabilities()

    # ------------------------------------------------------------------
    # Tab 3: 计划与保留
    # ------------------------------------------------------------------
    def _build_schedule_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 8)
        lay.setSpacing(14)

        sch = Card()
        sch.add(title_label("执行计划", 3))
        self.sched_combo = Combo()
        self.sched_combo.addItem("仅手动（不自动执行）", "manual")
        self.sched_combo.addItem("每隔一段时间", "interval")
        self.sched_combo.addItem("每天定时", "daily")
        self.sched_combo.addItem("每周定时", "weekly")
        self.sched_combo.addItem("每月定时", "monthly")
        sr = QHBoxLayout()
        sr.addWidget(QLabel("计划"))
        sr.addWidget(self.sched_combo, 1)
        sch.add_layout(sr)

        self.sched_stack = QStackedWidget()
        # 0 manual
        w0 = QWidget()
        l0 = QVBoxLayout(w0)
        l0.setContentsMargins(0, 0, 0, 0)
        l0.addWidget(muted("只有点击「立即备份」或在托盘菜单里选中时才会执行。"))
        self.sched_stack.addWidget(w0)
        # 1 interval
        w1 = QWidget()
        l1 = QHBoxLayout(w1)
        l1.setContentsMargins(0, 0, 0, 0)
        self.interval_value = QSpinBox()
        self.interval_value.setRange(1, 10080)
        self.interval_value.setValue(60)
        self.interval_unit = Combo()
        self.interval_unit.addItem("分钟", 1)
        self.interval_unit.addItem("小时", 60)
        self.interval_unit.addItem("天", 1440)
        self.interval_unit.setCurrentIndex(1)
        l1.addWidget(QLabel("每"))
        l1.addWidget(self.interval_value)
        l1.addWidget(self.interval_unit)
        l1.addStretch(1)
        self.sched_stack.addWidget(w1)
        # 2 daily
        w2 = QWidget()
        l2 = QHBoxLayout(w2)
        l2.setContentsMargins(0, 0, 0, 0)
        self.daily_time = QTimeEdit(QTime(2, 0))
        self.daily_time.setDisplayFormat("HH:mm")
        l2.addWidget(QLabel("每天"))
        l2.addWidget(self.daily_time)
        l2.addStretch(1)
        self.sched_stack.addWidget(w2)
        # 3 weekly
        w3 = QWidget()
        l3 = QHBoxLayout(w3)
        l3.setContentsMargins(0, 0, 0, 0)
        l3.setSpacing(4)
        self.week_boxes: list[QCheckBox] = []
        for i, name in enumerate(_WEEK):
            cb = QCheckBox(name)
            cb.setChecked(i < 5)
            self.week_boxes.append(cb)
            l3.addWidget(cb)
        self.weekly_time = QTimeEdit(QTime(2, 0))
        self.weekly_time.setDisplayFormat("HH:mm")
        l3.addSpacing(10)
        l3.addWidget(self.weekly_time)
        l3.addStretch(1)
        self.sched_stack.addWidget(w3)
        # 4 monthly
        w4 = QWidget()
        l4 = QHBoxLayout(w4)
        l4.setContentsMargins(0, 0, 0, 0)
        self.month_day = QSpinBox()
        self.month_day.setRange(1, 31)
        self.month_day.setValue(1)
        self.month_time = QTimeEdit(QTime(2, 0))
        self.month_time.setDisplayFormat("HH:mm")
        l4.addWidget(QLabel("每月"))
        l4.addWidget(self.month_day)
        l4.addWidget(QLabel("日"))
        l4.addWidget(self.month_time)
        l4.addStretch(1)
        self.sched_stack.addWidget(w4)
        self.sched_combo.currentIndexChanged.connect(self.sched_stack.setCurrentIndex)
        sch.add(self.sched_stack)
        self._sched_note = muted("提示：需要程序（或托盘）在运行才会执行计划；启用「开机自启」可让计划长期生效。")
        sch.add(self._sched_note)
        lay.addWidget(sch)

        ret = Card()
        ret.add(title_label("保留策略", 3))
        self.ret_enabled = Switch(dark=self._dark)
        rr = QHBoxLayout()
        rr.addWidget(self.ret_enabled)
        rr.addWidget(muted("自动清理旧的备份文件，避免磁盘被占满（只清理本任务生成的同格式压缩包）"))
        rr.addStretch(1)
        ret.add_layout(rr)
        self.ret_widget = QWidget()
        rf = QFormLayout(self.ret_widget)
        rf.setContentsMargins(0, 6, 0, 0)
        rf.setVerticalSpacing(10)
        self.ret_count = QSpinBox()
        self.ret_count.setRange(1, 9999)
        self.ret_count.setValue(30)
        rf.addRow("最多保留份数", self.ret_count)
        self.ret_days = QSpinBox()
        self.ret_days.setRange(1, 3650)
        self.ret_days.setValue(90)
        rf.addRow("最多保留天数", self.ret_days)
        self.ret_gb = QDoubleSpinBox()
        self.ret_gb.setRange(0, 100000)
        self.ret_gb.setDecimals(1)
        self.ret_gb.setSuffix(" GB")
        self.ret_gb.setSpecialValueText("不限制")
        rf.addRow("总大小上限", self.ret_gb)
        self.ret_action = Combo()
        self.ret_action.addItem("直接删除", "delete")
        self.ret_action.addItem("移动到回收目录", "move")
        self.ret_action.currentIndexChanged.connect(self._on_ret_action)
        rf.addRow("过期处理", self.ret_action)
        self.ret_dir = PathEdit(mode="dir", placeholder="回收目录", caption="选择回收目录")
        rf.addRow("回收目录", self.ret_dir)
        self.ret_dir.setVisible(False)
        ret.add(self.ret_widget)
        self.ret_widget.setVisible(False)
        self.ret_enabled.toggled.connect(self.ret_widget.setVisible)
        lay.addWidget(ret)

        notif = Card()
        notif.add(title_label("通知", 3))
        self.notif_success = QCheckBox("备份成功时通知")
        self.notif_success.setChecked(False)
        self.notif_failure = QCheckBox("备份失败时通知")
        self.notif_failure.setChecked(True)
        notif.add(self.notif_success)
        notif.add(self.notif_failure)
        self.verify_check = QCheckBox("备份完成后校验压缩包完整性（推荐）")
        self.verify_check.setChecked(True)
        notif.add(self.verify_check)
        lay.addWidget(notif)
        lay.addStretch(1)
        return page

    def _on_ret_action(self) -> None:
        self.ret_dir.setVisible(self.ret_action.currentData() == "move")

    # ------------------------------------------------------------------
    # 载入 / 保存
    # ------------------------------------------------------------------
    def _load_task(self) -> None:
        t = self._task
        self.name_edit.setText(t.name)
        self.enabled_switch.setChecked(t.enabled)
        src = t.sources[0] if t.sources else SourceConfig()
        self.src_path.edit.setText(src.path)
        self.source_mode.select_data(src.mode)
        if src.mode == "files":
            self._reload_file_list()
            for rel in src.files:
                item = QListWidgetItem("📄 " + rel)
                item.setData(Qt.ItemDataRole.UserRole, rel)
                self.src_files.addItem(item)
        self.src_files_box.setVisible(src.mode == "files")
        self.dest_path.edit.setText(t.destination)
        self.template.setText(t.options.filename_template)
        self.exclude_edit.setText(", ".join(t.options.exclude_globs))
        self.follow_links.setChecked(t.options.follow_symlinks)
        self.fmt_combo.select_data(t.options.fmt)
        self.level_slider.setValue(t.options.level)
        self._on_level_changed(t.options.level)
        self.pw_enabled.setChecked(bool(t.options.password))
        self.pw_edit.setText(t.options.password)
        self.pw_edit2.setText(t.options.password)
        self.enc_combo.select_data(t.options.encrypt_mode)
        self.pw_widget.setVisible(bool(t.options.password))
        self.mv_enabled.setChecked(bool(t.options.multivolume))
        self.mv_widget.setVisible(bool(t.options.multivolume))
        if t.options.multivolume:
            self.mv_combo.select_data(t.options.multivolume)
        self.vol_value.setValue(t.options.volume_value)
        self.vol_unit.select_data(t.options.volume_unit)
        self.force_switch.setChecked(t.options.force)

        sch = t.schedule
        self.sched_combo.select_data(sch.type)
        self.sched_stack.setCurrentIndex(max(0, self.sched_combo.currentIndex()))
        hh, mm = (sch.time or "02:00").split(":") if ":" in (sch.time or "") else ("02", "00")
        self.daily_time.setTime(QTime(int(hh) % 24, int(mm) % 60))
        self.weekly_time.setTime(QTime(int(hh) % 24, int(mm) % 60))
        self.month_time.setTime(QTime(int(hh) % 24, int(mm) % 60))
        for i, cb in enumerate(self.week_boxes):
            cb.setChecked(i in sch.days)
        self.month_day.setValue(sch.day)
        if sch.minutes % 1440 == 0:
            self.interval_value.setValue(max(1, sch.minutes // 1440))
            self.interval_unit.select_data(1440)
        elif sch.minutes % 60 == 0:
            self.interval_value.setValue(max(1, sch.minutes // 60))
            self.interval_unit.select_data(60)
        else:
            self.interval_value.setValue(sch.minutes)
            self.interval_unit.select_data(1)

        ret = t.retention
        self.ret_enabled.setChecked(ret.enabled)
        self.ret_widget.setVisible(ret.enabled)
        self.ret_count.setValue(ret.keep_last)
        self.ret_days.setValue(ret.keep_days)
        self.ret_gb.setValue(ret.max_total_gb)
        self.ret_action.select_data(ret.on_cleanup)
        self.ret_dir.edit.setText(ret.cleanup_dir)
        self.ret_dir.setVisible(ret.on_cleanup == "move")
        self.notif_success.setChecked("success" in t.notify)
        self.notif_failure.setChecked("failure" in t.notify)
        self.verify_check.setChecked(t.verify)

        for w in (self.src_path, self.dest_path):
            w.changed.connect(self._update_summary)
        self.name_edit.textChanged.connect(self._update_summary)
        self.mv_combo.currentIndexChanged.connect(self._update_summary)
        self.fmt_combo.currentIndexChanged.connect(self._update_summary)
        self._update_summary()

    def _update_summary(self, *_args) -> None:
        template = self.template.text().strip() or "{name}_{datetime}"
        name = self.name_edit.text().strip() or "备份任务"
        try:
            probe = BackupTask(name=name, options=ArchiveOptions(fmt=self.fmt_combo.currentData(), filename_template=template), sources=[SourceConfig(path=self.src_path.path())])
            probe.destination = self.dest_path.path()
            path = archive_path(probe)
            self.path_preview.setText(f"将生成：{path}")
        except Exception as exc:  # noqa: BLE001
            self.path_preview.setText(f"预览失败：{exc}")
        # 备份位置在来源内部提示
        src = normalize_path(self.src_path.path())
        dest = normalize_path(self.dest_path.path())
        warn = ""
        if src and dest:
            try:
                Path(dest).resolve().relative_to(Path(src).resolve())
                warn = "⚠ 备份位置位于来源文件夹内部：备份时会自动跳过目标目录，但仍建议把备份放到其它硬盘或目录。"
            except ValueError:
                pass
        self.dest_warning.setText(warn)

    def _refresh_capabilities(self) -> None:
        fmt = self.fmt_combo.currentData()
        entry = self._caps.get(fmt, {})
        available = entry.get("available", False)
        self.fmt_status.setText(format_status_line(fmt, entry) if entry else "")
        self.fmt_status.setObjectName("SuccessText" if available else "ErrorText")
        self.fmt_status.setStyleSheet(
            f"color: {'#16A34A' if available else '#DC2626'}; font-weight: 600;"
        )
        # 缺失软件
        while self.fmt_missing_lay.count():
            item = self.fmt_missing_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for f, hint, desc in missing_requirements(self._caps):
            if f != fmt:
                continue
            lab = QLabel(f"缺少：{hint}\n用于：{desc}")
            lab.setWordWrap(True)
            lab.setObjectName("WarningText")
            lab.setStyleSheet("background: rgba(245,158,11,0.12); border-radius: 8px; padding: 8px 10px;")
            self.fmt_missing_lay.addWidget(lab)

        # 密码选项可用性
        pw_available = []
        for mode in ENCRYPTION_MODES:
            info = entry.get("password", {}).get(mode, {})
            if info.get("available"):
                pw_available.append(mode)
        current = self.enc_combo.currentData()
        self.enc_combo.blockSignals(True)
        self.enc_combo.clear()
        for mode in ENCRYPTION_MODES:
            info = entry.get("password", {}).get(mode, {})
            if not info:
                continue
            label = ENCRYPTION_LABELS.get(mode, mode)
            if not info.get("available"):
                label += "（不可用）"
            self.enc_combo.addItem(label, mode)
        self.enc_combo.select_data(current if current in pw_available else (pw_available[0] if pw_available else current))
        self.enc_combo.blockSignals(False)
        if not pw_available:
            self.pw_enabled.setChecked(False)
            self.pw_enabled.setEnabled(False)
            self.enc_note.setText(f"当前格式暂不支持加密：{entry.get('reason', '')}")
        else:
            self.pw_enabled.setEnabled(True)
            mode = self.enc_combo.currentData()
            info = entry.get("password", {}).get(mode, {})
            note = info.get("note", "")
            if not info.get("safe", True):
                note += "（⚠ 安全性较低）"
            self.enc_note.setText(note)

        # 分卷
        modes = [m for m in MULTIVOLUME_MODES if entry.get("multivolume", {}).get(m, {}).get("available")]
        cur_mv = self.mv_combo.currentData()
        self.mv_combo.blockSignals(True)
        self.mv_combo.clear()
        for mode in MULTIVOLUME_MODES:
            label = MULTIVOLUME_LABELS.get(mode, mode)
            if mode not in modes:
                label += "（不可用）"
            self.mv_combo.addItem(label, mode)
        self.mv_combo.select_data(cur_mv if cur_mv in modes else (modes[0] if modes else cur_mv))
        self.mv_combo.blockSignals(False)
        self.mv_combo.setEnabled(bool(modes))
        if modes:
            self.mv_note.setText(entry["multivolume"][self.mv_combo.currentData()].get("note", ""))
        else:
            self.mv_note.setText("该格式当前无法分卷")
            self.mv_enabled.setChecked(False)
            self.mv_enabled.setEnabled(False)

        # 强制提示
        if not available:
            self.head_badge.set_state("缺少组件", "bad")
        elif fmt == "rar" and not entry.get("engine"):
            self.head_badge.set_state("强制模式", "warn")
        else:
            self.head_badge.set_state(f"{FORMAT_LABELS.get(fmt, fmt)} 可用", "ok")

    # ------------------------------------------------------------------
    def _collect(self) -> BackupTask | None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "需要任务名称", "请填写任务名称。")
            self.tabs.setCurrentIndex(0)
            return None
        src_path = normalize_path(self.src_path.path())
        if not src_path:
            QMessageBox.warning(self, "缺少来源", "请选择要备份的文件夹。")
            self.tabs.setCurrentIndex(0)
            return None
        if not Path(src_path).exists():
            QMessageBox.warning(self, "来源不存在", f"文件夹不存在：\n{src_path}")
            self.tabs.setCurrentIndex(0)
            return None
        mode = self.source_mode.currentData()
        files = self._selected_files() if mode == "files" else []
        if mode == "files" and not files:
            ret = QMessageBox.question(
                self, "未选择文件", "没有选择任何文件，将改为备份整个文件夹，是否继续？"
            )
            if ret != QMessageBox.StandardButton.Yes:
                self.tabs.setCurrentIndex(0)
                return None
            mode = "folder"
        dest = normalize_path(self.dest_path.path())
        if not dest:
            QMessageBox.warning(self, "缺少备份位置", "请选择备份存放目录。")
            self.tabs.setCurrentIndex(0)
            return None
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "无法创建目录", f"备份位置不可用：\n{dest}\n\n{exc}")
            return None

        password = self.pw_edit.text() if self.pw_enabled.isChecked() else ""
        if self.pw_enabled.isChecked():
            if not password:
                QMessageBox.warning(self, "密码为空", "已开启密码保护，请填写密码。")
                self.tabs.setCurrentIndex(1)
                return None
            if password != self.pw_edit2.text():
                QMessageBox.warning(self, "密码不一致", "两次输入的密码不一致。")
                self.tabs.setCurrentIndex(1)
                return None
        fmt = self.fmt_combo.currentData()
        entry = self._caps.get(fmt, {})
        force = self.force_switch.isChecked()
        if not entry.get("available", True) and not force:
            ret = QMessageBox.question(
                self,
                "缺少压缩组件",
                f"{FORMAT_LABELS.get(fmt, fmt)} 在当前系统不可用：\n{entry.get('reason', '')}\n\n"
                "是否启用「强制压缩」并继续保存？\n（会用内置引擎尝试生成，结果中会提示实际容器）",
            )
            if ret != QMessageBox.StandardButton.Yes:
                self.tabs.setCurrentIndex(1)
                return None
            force = True
            self.force_switch.setChecked(True)

        mv = self.mv_combo.currentData() if self.mv_enabled.isChecked() else ""
        if mv and mv not in [m for m in MULTIVOLUME_MODES if entry.get("multivolume", {}).get(m, {}).get("available")]:
            if not force:
                QMessageBox.warning(self, "分卷不可用", "所选分卷方式在当前环境不可用，请更换或启用强制压缩。")
                self.tabs.setCurrentIndex(1)
                return None

        options = ArchiveOptions(
            fmt=fmt,
            level=self.level_slider.value(),
            password=password,
            encrypt_mode=self.enc_combo.currentData() or "aes256",
            multivolume=mv or "",
            volume_value=self.vol_value.value(),
            volume_unit=self.vol_unit.currentData() or "MB",
            filename_template=self.template.text().strip() or "{name}_{datetime}",
            exclude_globs=[g.strip() for g in self.exclude_edit.text().replace("，", ",").split(",") if g.strip()],
            follow_symlinks=self.follow_links.isChecked(),
            force=force,
        )

        # 计划
        sched_type = self.sched_combo.currentData()
        minutes = self.interval_value.value() * int(self.interval_unit.currentData() or 1)
        time_text = self.daily_time.time().toString("HH:mm")
        days = [i for i, cb in enumerate(self.week_boxes) if cb.isChecked()]
        if sched_type == "weekly" and not days:
            QMessageBox.warning(self, "未选择星期", "每周计划至少要选择一天。")
            self.tabs.setCurrentIndex(2)
            return None
        if sched_type == "weekly":
            time_text = self.weekly_time.time().toString("HH:mm")
        elif sched_type == "monthly":
            time_text = self.month_time.time().toString("HH:mm")
        schedule = ScheduleConfig(type=sched_type, time=time_text, days=days, day=self.month_day.value(), minutes=minutes)

        retention = RetentionPolicy(
            enabled=self.ret_enabled.isChecked(),
            keep_last=self.ret_count.value(),
            keep_days=self.ret_days.value(),
            max_total_gb=self.ret_gb.value(),
            on_cleanup=self.ret_action.currentData() or "delete",
            cleanup_dir=normalize_path(self.ret_dir.edit.text()) if self.ret_enabled.isChecked() else "",
        )
        if retention.enabled and retention.on_cleanup == "move" and not retention.cleanup_dir:
            QMessageBox.warning(self, "缺少回收目录", "选择「移动到回收目录」时必须设置回收目录。")
            self.tabs.setCurrentIndex(2)
            return None

        notify = []
        if self.notif_success.isChecked():
            notify.append("success")
        if self.notif_failure.isChecked():
            notify.append("failure")

        task = BackupTask(
            id=self._task.id,
            name=name,
            enabled=self.enabled_switch.isChecked(),
            sources=[SourceConfig(path=src_path, mode=mode, files=files)],
            destination=dest,
            options=options,
            schedule=schedule,
            retention=retention,
            verify=self.verify_check.isChecked(),
            notify=notify,
            last_run=self._task.last_run,
            last_status=self._task.last_status,
            last_message=self._task.last_message,
            last_archive=self._task.last_archive,
            last_duration=self._task.last_duration,
            created_at=self._task.created_at,
        )
        return task

    def _on_accept(self) -> None:
        task = self._collect()
        if task is None:
            return
        self._result_task = task
        self.accept()

    def result_task(self) -> BackupTask | None:
        return getattr(self, "_result_task", None)
