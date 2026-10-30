"""备份任务页：任务列表 + 详情 + 立即备份/进度。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..config import BackupTask
from ..engine import volume_glob_paths
from ..schedule import describe_next
from ..util import human_duration, human_size, reveal_file
from .appstate import AppState
from .icons import line_icon
from .widgets import Badge, Card, ElideLabel, StatusPill, muted, title_label

log = logging.getLogger("backupstudio.gui.tasks")

STATUS_MAP = {
    "success": ("成功", "ok"),
    "failure": ("失败", "bad"),
    # **1.1.2 修复**：`engine.py` 会产出 `status="partial"`（有文件被占用/无权限，
    # 备份完成了但不完整）。这里原本没有这个键 → `.get()` 回落到默认值
    # 「未运行过」，即**部分失败的备份在列表里显示成"从没跑过"**，
    # 用户会以为定时任务没生效，实际是备份不完整需要处理。
    "partial": ("部分完成", "warn"),
    "skipped": ("已跳过", "warn"),
    "running": ("运行中", "info"),
    "": ("未运行过", "muted"),
}

# 「改用内置引擎重试」弹窗的判定关键词（审查 W-A7-1 / W-A7-3）。
# 只有命中 _FORCE_RETRY_HINTS（外部压缩程序写不出包、或缺少外部程序，内置引擎确实能补位）
# 才提示；命中 _FORCE_RETRY_EXCLUDES（密码/参数/格式类，内置引擎同样无解）则一律不提示。
# 旧判据里的 "7-Zip" 子串过宽——密码类失败的 errors 也含它，于是把用户引向
# 「目标目录被限制」的错误归因，并且强制重试必然再失败、再弹窗（实测 2 分钟 467 次）。
_FORCE_RETRY_HINTS = (
    "拒绝访问",
    "没有写入权限",
    "未能写入压缩包",
    "无法写入压缩包",
    "无法写入目标文件",
    "分卷失败",
    "分卷无效",
    "未找到 7-zip",
    "缺少 7-zip",
    "cannot open",
    "access is denied",
    "permission denied",
)
_FORCE_RETRY_EXCLUDES = (
    "参数错误",
    "密码",
    "password",
    "passwordrequired",
    "不支持加密",
    "ascii",
    "不支持的格式",
    "不支持压缩格式",
    "加密方式",
    "尚未实现",
)


class TaskItemWidget(QWidget):
    """列表中的任务条目（1.1.2 重做）。

    改动：
    - 标题改用 `ElideLabel`（长任务名不再撑破/硬截断）
    - 副标题只显示「格式 · 下次时间」，**去掉重复的路径**
      （路径在右侧详情里已有，重复显示既挤又乱）
    - 左侧加状态色圆点，一眼看出成功/失败/未运行
    """

    def __init__(self, task: BackupTask, dark: bool = False) -> None:
        super().__init__()
        self.task = task
        self._dark = dark
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(9)

        # 状态圆点（用文本字符画，无需额外资源）
        self.dot = QLabel("●")
        self.dot.setFixedWidth(10)
        lay.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(3)
        col.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.title = ElideLabel(task.name)
        self.title.setStyleSheet("font-weight: 600; font-size: 11pt;")
        self.badge = Badge("", "muted")
        self.badge.set_dark(dark)
        top.addWidget(self.title, 1)
        top.addWidget(self.badge, 0)
        col.addLayout(top)

        self.sub = ElideLabel("")
        self.sub.setObjectName("Muted")
        self.sub.setStyleSheet("font-size: 9pt;")
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        self.update_task(task)

    def update_task(self, task: BackupTask) -> None:
        self.task = task
        self.title.setText(task.name)
        text, kind = STATUS_MAP.get(task.last_status, ("未运行过", "muted"))
        self.badge.set_state(text, kind)
        self._set_dot(kind)
        sched = task.schedule.describe()
        when = describe_next(task, datetime.now())
        fmt = (task.options.fmt or "").upper()
        self.sub.setText(f"{fmt} · {sched} · 下次 {when}")
        self.sub.setToolTip(f"{sched} · 下次 {when}\n{task.destination or '未设置位置'}")

    def _set_dot(self, kind: str) -> None:
        from . import DARK as _D
        from . import LIGHT as _L

        pal = _D if self._dark else _L
        color = {
            "ok": pal.success,
            "bad": pal.danger,
            "warn": pal.warning,
            "info": pal.accent,
        }.get(kind, pal.border_strong)
        self.dot.setStyleSheet(f"color: {color}; font-size: 11pt;")

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self.badge.set_dark(dark)
        self.update_task(self.task)


class TasksPage(QWidget):
    """任务页面。"""

    edit_requested = Signal(str)

    def __init__(self, state: AppState, window) -> None:  # noqa: ANN001
        super().__init__()
        self.state = state
        self.window = window
        self._dark = False
        self._items: dict[str, TaskItemWidget] = {}
        self._current_id: str = ""
        self._progress: dict[str, dict] = {}
        # 已问过「改用内置引擎重试」的任务：本轮 GUI 会话每个任务最多问一次
        # （审查 W-A7-1：强制重试仍失败会再次弹窗，形成无限循环）
        self._force_offered: set[str] = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        header = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(title_label("备份任务", 1))
        col.addWidget(muted("按计划或手动把文件夹/指定文件压缩到自定义位置"))
        header.addLayout(col, 1)
        self.btn_new = QPushButton(" 新建任务")
        self.btn_new.setObjectName("Primary")
        self.btn_new.setIcon(line_icon("plus", "#FFFFFF", 16))
        self.btn_new.setIconSize(QSize(16, 16))
        self.btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new.clicked.connect(self.new_task)
        header.addWidget(self.btn_new)
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # 左：任务列表
        left = Card()
        left.setMinimumWidth(300)
        left.setMaximumWidth(430)
        self.search = QLabel("")
        self.list = QListWidget()
        self.list.setSpacing(2)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setStyleSheet("QListWidget { background: transparent; border: none; }")
        self.list.currentItemChanged.connect(self._on_select)
        self.list.itemDoubleClicked.connect(lambda _: self.edit_current())
        # **1.1.2 一键删除**：列表右键菜单 + Delete 键。
        # 之前只有右侧详情区一个「删除」按钮、且必须先选中任务才可用，
        # 用户反馈"备份任务里没有一键删除"。
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_list_menu)
        self.list.installEventFilter(self)
        left.add(self.list, 1)
        empty = muted("还没有任务。点击右上角「新建任务」开始。")
        left.add(empty)
        self._empty_hint = empty
        splitter.addWidget(left)

        # 右：详情
        right = Card()
        rl = QVBoxLayout()
        rl.setSpacing(12)
        top = QHBoxLayout()
        self.detail_title = ElideLabel("选择一个任务")
        self.detail_title.setStyleSheet("font-size: 12pt; font-weight: 600;")
        self.detail_badge = Badge("", "muted")
        self.detail_badge.set_dark(self._dark)
        top.addWidget(self.detail_title, 1)
        top.addWidget(self.detail_badge, 0)
        rl.addLayout(top)

        # 1.1.2：副标题改为「上次运行」摘要 —— 原来这里显示来源路径，
        # 与下面「来源」卡片完全重复（同一路径在详情里出现两次）
        self.detail_sub = ElideLabel("")
        self.detail_sub.setObjectName("Muted")
        self.detail_sub.setStyleSheet("font-size: 9pt;")
        rl.addWidget(self.detail_sub)

        from PySide6.QtWidgets import QGridLayout

        info_grid = QGridLayout()
        info_grid.setSpacing(10)
        self.info_cards: dict[str, tuple[Card, ElideLabel]] = {}
        for idx, (key, label) in enumerate((
            ("source", "来源"),
            ("dest", "备份位置"),
            ("format", "压缩格式"),
            ("schedule", "计划"),
        )):
            c = Card(flat=True, margins=(14, 10, 14, 10))
            cap = QLabel(label)
            cap.setObjectName("CardCap")
            # 路径可能很长：用省略号 + tooltip，不再硬截断
            val = ElideLabel("—")
            val.setObjectName("CardVal")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            c.add(cap)
            c.add(val)
            c.setMinimumWidth(150)
            info_grid.addWidget(c, idx // 2, idx % 2)
            info_grid.setColumnStretch(idx % 2, 1)
            self.info_cards[key] = (c, val)
        rl.addLayout(info_grid)

        self.last_run = StatusPill()
        rl.addWidget(self.last_run)

        # 进度区
        self.progress_box = Card(flat=True, margins=(14, 10, 14, 10))
        pb = QHBoxLayout()
        self.progress_label = QLabel("")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)
        self.progress_bar.setMinimumWidth(220)
        pb.addWidget(self.progress_label, 1)
        pb.addWidget(self.progress_bar)
        self.progress_box.add_layout(pb)
        rl.addWidget(self.progress_box)
        self.progress_box.hide()

        # 操作按钮
        actions = QHBoxLayout()
        self.btn_run = QPushButton(" 立即备份")
        self.btn_run.setObjectName("Primary")
        self.btn_run.setIcon(line_icon("run", "#FFFFFF", 15))
        self.btn_run.setIconSize(QSize(15, 15))
        self.btn_run.clicked.connect(self.run_current)
        self.btn_stop = QPushButton(" 停止")
        self.btn_stop.setIcon(line_icon("stop", "#EF4444", 15))
        self.btn_stop.setIconSize(QSize(15, 15))
        self.btn_stop.clicked.connect(self.stop_current)
        self.btn_stop.hide()
        self.btn_edit = QPushButton("编辑")
        self.btn_edit.clicked.connect(self.edit_current)
        self.btn_dup = QPushButton("复制")
        self.btn_dup.clicked.connect(self.duplicate_current)
        self.btn_del = QPushButton("删除")
        self.btn_del.setObjectName("Danger")
        self.btn_del.clicked.connect(self.delete_current)
        actions.addWidget(self.btn_run)
        actions.addWidget(self.btn_stop)
        actions.addStretch(1)
        actions.addWidget(self.btn_edit)
        actions.addWidget(self.btn_dup)
        actions.addWidget(self.btn_del)
        rl.addLayout(actions)

        # 最近一次结果
        res_head = QHBoxLayout()
        res_head.addWidget(title_label("最近一次结果", 3))
        res_head.addStretch(1)
        self.btn_open = QPushButton("打开所在文件夹")
        self.btn_open.setObjectName("Link")
        self.btn_open.clicked.connect(self.open_archive)
        self.btn_reveal = QPushButton("定位文件")
        self.btn_reveal.setObjectName("Link")
        self.btn_reveal.clicked.connect(self.reveal_archive)
        res_head.addWidget(self.btn_open)
        res_head.addWidget(self.btn_reveal)
        rl.addLayout(res_head)
        self.result_text = QLabel("—")
        self.result_text.setWordWrap(True)
        self.result_text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.result_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result_text.setStyleSheet(
            "font-family: 'Cascadia Mono','Consolas','Menlo',monospace; font-size: 9pt;"
            "background: transparent; padding: 4px 2px;"
        )
        result_holder = QWidget()
        result_holder.setObjectName("Page")
        result_lay = QVBoxLayout(result_holder)
        result_lay.setContentsMargins(0, 0, 0, 0)
        result_lay.addWidget(self.result_text, 0, Qt.AlignmentFlag.AlignTop)
        result_lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; } QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(result_holder)
        self.result_scroll = scroll
        rl.addWidget(scroll, 1)

        # 1.1.2：空状态 —— 一张"还没有结果"的引导面板，替代原来孤零零的「—」
        self.empty_box = QWidget()
        ev = QVBoxLayout(self.empty_box)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setSpacing(8)
        ev.addStretch(1)
        self.empty_icon = QLabel()
        self.empty_icon.setObjectName("EmptyIcon")
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.empty_title = QLabel("还没有备份记录")
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.empty_hint = QLabel("点上面的「立即备份」跑第一次，或编辑任务设置自动计划")
        self.empty_hint.setObjectName("EmptyHint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.empty_hint.setWordWrap(True)
        for w in (self.empty_icon, self.empty_title, self.empty_hint):
            ev.addWidget(w)
        ev.addStretch(1)
        rl.addWidget(self.empty_box, 1)
        self.empty_box.hide()

        right.add_layout(rl)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 760])
        root.addWidget(splitter, 1)

        state.tasks_changed.connect(self.refresh)
        state.task_updated.connect(self._on_task_updated)
        state.run_started.connect(self._on_run_started)
        state.run_finished.connect(self._on_run_finished)
        state.progress_changed.connect(self._on_progress)

        self._tick = QTimer(self)
        self._tick.setInterval(5000)
        self._tick.timeout.connect(self._refresh_subtitles)
        self._tick.start()

        self.refresh()

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        current = self._current_id
        self.list.blockSignals(True)
        self.list.clear()
        self._items.clear()
        tasks = self.state.tasks()
        for t in tasks:
            item = QListWidgetItem()
            widget = TaskItemWidget(t, self._dark)
            item.setSizeHint(QSize(280, 58))
            item.setData(Qt.ItemDataRole.UserRole, t.id)
            self.list.addItem(item)
            self.list.setItemWidget(item, widget)
            self._items[t.id] = widget
        self.list.blockSignals(False)
        self._empty_hint.setVisible(not tasks)
        if tasks:
            idx = next((i for i, t in enumerate(tasks) if t.id == current), 0)
            self.list.setCurrentRow(idx)
        else:
            self._current_id = ""
            self._show_empty()
        self._update_buttons()

    def _refresh_subtitles(self) -> None:
        for tid, widget in self._items.items():
            task = self.state.get(tid)
            if task is not None and tid not in self._progress:
                widget.update_task(task)

    def _on_select(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:  # noqa: ANN001
        if current is None:
            return
        self._current_id = current.data(Qt.ItemDataRole.UserRole)
        self._show_task(self._current_id)
        self._update_buttons()

    def selected_task_id(self) -> str:
        return self._current_id

    def _show_empty(self) -> None:
        self.detail_title.setText("选择一个任务")
        self.detail_badge.setVisible(False)
        self.detail_sub.setText("左侧选一个任务，这里会显示它的来源、目标与最近一次结果")
        for _, val in self.info_cards.values():
            val.setText("—")
        self.result_text.setText("")
        self.result_scroll.hide()
        self.empty_box.hide()
        self.last_run.set_state("muted", "")
    def _show_task(self, task_id: str) -> None:
        task = self.state.get(task_id)
        if task is None:
            self._show_empty()
            return
        self.detail_title.setText(task.name)
        text, kind = STATUS_MAP.get(task.last_status, ("未运行过", "muted"))
        self.detail_badge.setVisible(True)
        self.detail_badge.set_state(text, kind)
        self.detail_sub.setText(
            (
                f"上次运行 {self._fmt_time(task.last_run)} · 用时 {human_duration(task.last_duration)}"
                if task.last_run else "还没有运行记录 —— 点「立即备份」跑第一次"
            )
        )
        self.info_cards["source"][1].setText(self._source_short(task))
        self.info_cards["dest"][1].setText(task.destination or "未设置")
        from ..capabilities import FORMAT_LABELS

        fmt = FORMAT_LABELS.get(task.options.fmt, task.options.fmt)
        extras = []
        if task.options.password:
            extras.append("加密")
        if task.options.multivolume:
            extras.append(f"分卷 {task.options.volume_value}{task.options.volume_unit}")
        if task.options.force:
            extras.append("强制")
        if extras:
            fmt += " · " + " · ".join(extras)
        self.info_cards["format"][1].setText(fmt)
        self.info_cards["schedule"][1].setText(f"{task.schedule.describe()}｜下次 {describe_next(task)}")
        # 上次结果
        if task.last_status == "running":
            self.last_run.set_state("run", "正在备份…")
        elif task.last_run:
            kind2 = {"success": "ok", "failure": "bad", "partial": "warn", "skipped": "warn"}.get(task.last_status, "muted")
            self.last_run.set_state(kind2, f"{self._fmt_time(task.last_run)} · {task.last_message or STATUS_MAP.get(task.last_status, ('', ''))[0]}")
        else:
            self.last_run.set_state("muted", "尚未运行")
        self._render_result(task)
        if task.id in self._progress:
            self._apply_progress(task.id, self._progress[task.id])

    @staticmethod
    def _fmt_time(iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso)
            delta = datetime.now().astimezone() - dt
            secs = int(delta.total_seconds())
            if secs < 60:
                return "刚刚"
            if secs < 3600:
                return f"{secs // 60} 分钟前"
            if secs < 86400:
                return f"{secs // 3600} 小时前"
            return dt.strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            return iso or "-"

    def _describe_sources(self, task: BackupTask) -> str:
        parts = []
        for s in task.sources:
            if not s.path:
                continue
            if s.mode == "files" and s.files:
                parts.append(f"{s.path}（指定 {len(s.files)} 个条目）")
            else:
                parts.append(s.path)
        return "；".join(parts) or "未设置来源"

    def _source_short(self, task: BackupTask) -> str:
        paths = [s.path for s in task.sources if s.path]
        if not paths:
            return "未设置"
        if len(paths) == 1:
            return paths[0]
        return f"{paths[0]} 等 {len(paths)} 处"

    def _render_result(self, task: BackupTask) -> None:
        lines = []
        if task.last_archive:
            p = Path(task.last_archive)
            lines.append(f"压缩包：{p.name}")
            if p.exists():
                files = volume_glob_paths(p)
                total = sum(f.stat().st_size for f in files) if files else p.stat().st_size
                lines.append(f"大小：{human_size(total)}（{len(files)} 个文件）")
        if task.last_duration:
            lines.append(f"耗时：{human_duration(task.last_duration)}")
        if task.last_message:
            lines.append(f"信息：{task.last_message}")
        self.result_text.setText("\n".join(lines))
        # 1.1.2：没有结果时显示引导面板，而不是一片空白 + 一行小字
        has = bool(lines)
        self.result_scroll.setVisible(has)
        self.empty_box.setVisible(not has)
        if not has:
            from . import DARK as _D
            from . import LIGHT as _L

            pal = _D if self._dark else _L
            running = task.last_status == "running"
            # 用绘制的图形图标，比字符符号干净（原来是一个半圆字符，很怪）
            self.empty_icon.setPixmap(
                line_icon("run" if running else "folder", pal.border_strong, 44).pixmap(44, 44)
            )
            self.empty_title.setText("正在备份…" if running else "还没有备份记录")
            self.empty_hint.setText(
                "完成后这里会显示压缩包名、大小和耗时"
                if running else "点上面的「立即备份」跑第一次，或编辑任务设置自动计划"
            )

    # ------------------------------------------------------------------
    # 进度
    # ------------------------------------------------------------------
    def _on_run_started(self, task_id: str) -> None:
        self._progress[task_id] = {"phase": "prepare", "done_bytes": 0, "total_bytes": 0, "done_items": 0, "total_items": 0}
        if task_id == self._current_id:
            self.progress_box.show()
            self._apply_progress(task_id, self._progress[task_id])
            self._show_task(task_id)
        self._update_buttons()

    def _on_progress(self, task_id: str, payload: dict) -> None:
        self._progress[task_id] = payload
        if task_id == self._current_id:
            self._apply_progress(task_id, payload)

    def _apply_progress(self, task_id: str, payload: dict) -> None:
        phase = payload.get("phase", "write")
        total = max(1, payload.get("total_bytes") or 0)
        done = payload.get("done_bytes") or 0
        name = {"prepare": "准备中", "write": "正在写入", "compress": "正在压缩", "verify": "正在校验", "done": "完成"}.get(phase, phase)
        if phase == "compress":
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText("正在压缩（此阶段耗时最长）")
        elif phase == "verify":
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText("正在校验压缩包…")
        elif phase == "done":
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.progress_label.setText("完成")
        else:
            self.progress_bar.setRange(0, 100)
            pct = int(done * 100 / total) if total else 0
            self.progress_bar.setValue(min(100, pct))
            self.progress_label.setText(
                f"{name} {human_size(done)} / {human_size(total)} · {payload.get('done_items', 0)}/{payload.get('total_items', 0)} 项"
            )

    def _on_run_finished(self, result) -> None:  # noqa: ANN001
        tid = result.record.task_id
        self._progress.pop(tid, None)
        if tid == self._current_id:
            self.progress_box.hide()
            self._show_task(tid)
        self._update_buttons()
        self._maybe_offer_force_retry(result)

    @staticmethod
    def _forced_this_run(result) -> bool:
        """本轮记录是否已经用内置引擎（强制模式）跑过。"""
        rec = result.record
        if getattr(rec, "forced", False):
            return True
        backend = str(getattr(rec, "backend", "") or "")
        return backend.startswith(("py7zr", "zipfile", "python-", "builtin")) or "内置" in backend

    def _builtin_can_do_it(self, task: BackupTask) -> bool:
        """该任务改用内置/强制引擎后是否真能产出（且密码受支持）。

        与任务对话框同一来源（capabilities 表），因此 force=True 的
        ``available`` 恰好等于「勾上强制压缩后这条路走得通」：
        * 缺 7-Zip 的 7z：有 py7zr 才 True（无 py7zr 时弹了也没用）；
        * tar.xz 缺 xz 编解码器：False（内置引擎同样产不出）；
        * 带密码的 ZIP：内置 zipfile 对加密是 fail-closed 拒绝（archives.py
          ``PasswordRequired``），GPG 加密引擎尚未实现——两者重试必然再失败。
        探测异常时保守返回 False（宁可不弹，也不制造无效重试）。
        """
        opts = task.options
        if opts.password and (opts.fmt == "zip" or (opts.encrypt_mode or "") == "gpg"):
            return False
        try:
            from ..capabilities import format_capabilities

            entry = format_capabilities(force=True).get(opts.fmt)
            if not entry or not entry.get("available", False):
                return False
            if opts.password:
                pw = (entry.get("password") or {}).get(opts.encrypt_mode or "")
                # 未声明的加密方式（如 zip 的非 ASCII 密码限制）= 内置引擎同样无解
                return bool(pw and pw.get("available", False))
            return True
        except Exception:
            log.debug("内置引擎能力判定失败", exc_info=True)
            return False

    def _maybe_offer_force_retry(self, result) -> None:  # noqa: ANN001
        tid = result.record.task_id
        if tid in self._force_offered:
            # 一次性 guard：本会话已问过就不再问，彻底断掉弹窗循环
            return
        task = self.state.get(tid)
        if task is None or result.record.status != "failure" or task.options.force:
            return
        if self._forced_this_run(result):
            # 强制重试的结果本身又失败：不再第二次询问
            self._force_offered.add(tid)
            return
        errors = "\n".join(str(x) for x in (result.record.errors or [result.message]))
        low = errors.lower()
        # 密码/参数/格式类错误：内置引擎同样做不到（或错误与引擎无关），不弹
        if any(k in low for k in _FORCE_RETRY_EXCLUDES):
            return
        # 7-Zip 通用失败（退出码 2 + System ERROR）里没有权限字样时，
        # 多半是密码编码/参数问题——强制重试无益（审查 W-A7-1 的 M2 场景）
        if "system error" in low and "拒绝访问" not in errors and "access is denied" not in low:
            return
        if not any(k in low for k in _FORCE_RETRY_HINTS):
            return
        if not self._builtin_can_do_it(task):
            # 判定为「内置引擎也办不到」：记住结论，避免定时任务反复触发时重复探测
            self._force_offered.add(tid)
            return
        self._force_offered.add(tid)  # 弹窗前置位：模态期间的再次完成信号也不会重复问
        ret = QMessageBox.question(
            self,
            "压缩程序无法写入",
            "外部压缩程序没能写出压缩包，可能是目标目录被系统或安全软件限制。\n\n"
            "是否改用程序内置引擎（等效格式）立即重试一次？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.state.run_task(task.id, trigger="manual", force_retry=True)

    def _on_task_updated(self, task_id: str) -> None:
        task = self.state.get(task_id)
        widget = self._items.get(task_id)
        if task is not None and widget is not None:
            widget.update_task(task)
        if task_id == self._current_id and task is not None:
            text, kind = STATUS_MAP.get(task.last_status, ("未运行过", "muted"))
            self.detail_badge.set_state(text, kind)
            self._render_result(task)
        self._update_buttons()

    def has_running(self) -> bool:
        return any(self.state.is_running(t.id) for t in self.state.tasks())

    def _update_buttons(self) -> None:
        tid = self._current_id
        has = bool(tid) and self.state.get(tid) is not None
        running = has and self.state.is_running(tid)
        self.btn_run.setVisible(not running)
        self.btn_stop.setVisible(running)
        for btn in (self.btn_run, self.btn_edit, self.btn_dup, self.btn_del, self.btn_open, self.btn_reveal):
            btn.setEnabled(has)
        task = self.state.get(tid) if tid else None
        self.btn_open.setEnabled(bool(task and task.last_archive))
        self.btn_reveal.setEnabled(bool(task and task.last_archive and Path(task.last_archive).exists()))

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def new_task(self) -> None:
        from .task_dialog import TaskDialog

        dlg = TaskDialog(self.state, None, parent=self)
        if dlg.exec():
            task = dlg.result_task()
            if task is not None:
                self.state.save_task(task)
                self._current_id = task.id
                self.refresh()
                QTimer.singleShot(200, lambda: self._select_by_id(task.id))

    def _select_by_id(self, task_id: str) -> None:
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == task_id:
                self.list.setCurrentRow(i)
                return

    def edit_current(self) -> None:
        tid = self._current_id
        if not tid:
            return
        task = self.state.get(tid)
        if task is None:
            return
        from .task_dialog import TaskDialog

        dlg = TaskDialog(self.state, task, parent=self)
        if dlg.exec():
            updated = dlg.result_task()
            if updated is not None:
                self.state.save_task(updated)
                # 用户改了配置（格式/密码/强制等），旧的「内置引擎办不到」结论已失效
                self._force_offered.discard(updated.id)
                self._show_task(updated.id)

    def duplicate_current(self) -> None:
        tid = self._current_id
        if not tid:
            return
        new = self.state.store.duplicate(tid)
        if new is not None:
            self.state.store.save()
            self.state.tasks_changed.emit()
            self._current_id = new.id
            self.refresh()
            self._select_by_id(new.id)

    def delete_current(self) -> None:
        self._delete_task(self._current_id)

    # ------------------------------------------------------------------
    # 一键删除入口（1.1.2）：右键菜单 + Delete 键
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001, N802
        from PySide6.QtCore import QEvent

        if obj is getattr(self, "list", None) and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                tid = self._id_at_current_row()
                if tid:
                    self._delete_task(tid)
                    return True
        return super().eventFilter(obj, event)

    def _id_at_current_row(self) -> str:
        item = self.list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def _on_list_menu(self, pos) -> None:  # noqa: ANN001
        from PySide6.QtWidgets import QMenu

        item = self.list.itemAt(pos)
        if item is None:
            return
        tid = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not tid or self.state.get(tid) is None:
            return
        self.list.setCurrentItem(item)

        menu = QMenu(self)
        act_run = menu.addAction("立即备份")
        act_edit = menu.addAction("编辑…")
        act_dup = menu.addAction("复制")
        menu.addSeparator()
        act_del = menu.addAction("删除任务…")
        act_run.triggered.connect(lambda: self._with_id(tid, self.run_current))
        act_edit.triggered.connect(lambda: self._with_id(tid, self.edit_current))
        act_dup.triggered.connect(lambda: self._with_id(tid, self.duplicate_current))
        act_del.triggered.connect(lambda: self._delete_task(tid))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _with_id(self, tid: str, fn) -> None:  # noqa: ANN001
        """先切到该任务再执行操作（右键菜单项可能不是当前行）。"""
        self._current_id = tid
        self._select_by_id(tid)
        fn()

    def _delete_task(self, tid: str) -> None:
        task = self.state.get(tid) if tid else None
        if task is None:
            return
        ret = QMessageBox.question(
            self,
            "删除任务",
            f"确定删除任务「{task.name}」吗？\n\n已有的备份文件不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.state.delete_task(tid)
            self._force_offered.discard(tid)  # 任务消失，guard 不留残留
            self._current_id = ""
            self.refresh()

    def run_current(self) -> None:
        tid = self._current_id
        if not tid:
            return
        task = self.state.get(tid)
        if task is None:
            return
        if not task.destination or not any(s.path for s in task.sources):
            QMessageBox.warning(self, "任务未配置完整", "请先设置备份来源和备份位置。")
            return
        if not self.state.run_task(tid, trigger="manual"):
            QMessageBox.information(self, "提示", "该任务正在运行中。")
            return
        # 用户主动点的「立即备份」：允许重新询问一次内置引擎重试。
        # 注意只能在这条按钮路径上重置——force_retry 走的是 state.run_task，
        # 若在 _on_run_started 里重置，强制重试失败后会立刻再弹（W-A7-1 的循环）。
        self._force_offered.discard(tid)
        self._on_run_started(tid)

    def stop_current(self) -> None:
        tid = self._current_id
        if not tid:
            return
        worker = getattr(self.state, f"_worker_{tid}", None)
        if worker is not None:
            worker.cancel()
            self.progress_label.setText("正在停止…")

    def open_archive(self) -> None:
        task = self.state.get(self._current_id)
        if task is None or not task.last_archive:
            return
        from ..util import open_in_file_manager

        open_in_file_manager(str(Path(task.last_archive).parent))

    def reveal_archive(self) -> None:
        task = self.state.get(self._current_id)
        if task is None or not task.last_archive:
            return
        if not reveal_file(task.last_archive):
            QMessageBox.information(self, "提示", "无法打开文件管理器。")

    # ------------------------------------------------------------------
    def set_palette_mode(self, dark: bool) -> None:
        self._dark = dark
        self.detail_badge.set_dark(dark)
        for widget in self._items.values():
            widget.badge.set_dark(dark)

    def on_shown(self) -> None:
        self.refresh()
