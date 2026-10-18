"""主窗口：侧边导航 + 页面堆栈 + 托盘。"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QSize, Qt, Signal, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, APP_NAME_ZH, __version__
from . import app_icon, stylesheet, tray_state_icon
from .appstate import AppState

log = logging.getLogger("backupstudio.gui")

NAV_ITEMS = [
    ("tasks", "备份任务"),
    ("history", "运行记录"),
    ("env", "环境与压缩支持"),
    ("settings", "设置"),
]


class MainWindow(QMainWindow):
    about_to_quit = Signal()

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._really_quit = False
        self.setWindowTitle(f"{APP_NAME_ZH} · {APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1180, 760)
        self.setMinimumSize(980, 640)

        central = QWidget()
        central.setObjectName("Page")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        self.stack.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.stack, 1)

        from .environment_page import EnvironmentPage
        from .history_page import HistoryPage
        from .settings_page import SettingsPage
        from .tasks_page import TasksPage

        self.tasks_page = TasksPage(state, self)
        self.history_page = HistoryPage(state, self)
        self.env_page = EnvironmentPage(state, self)
        self.settings_page = SettingsPage(state, self)
        for page in (self.tasks_page, self.history_page, self.env_page, self.settings_page):
            self.stack.addWidget(page)

        self._select("tasks")
        self._build_tray()
        self._install_shortcuts()
        # 第一次运行时提示「程序在托盘里」，只提示一次
        if not state.store.settings.tray_hint_shown and not state.store.settings.start_minimized:
            QTimer.singleShot(1500, self._show_tray_hint_once)

        state.notified.connect(self._on_notify)
        state.theme_changed.connect(self._apply_theme)
        state.run_started.connect(self._on_run_started)
        state.run_finished.connect(self._on_run_finished)
        self._apply_theme(state.palette)

    # ------------------------------------------------------------------
    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Card")
        side.setFixedWidth(216)
        side.setStyleSheet("QFrame#Card { border-radius: 0; border: none; }")
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 18, 14, 14)
        lay.setSpacing(6)

        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(QSize(34, 34)))
        names = QVBoxLayout()
        names.setSpacing(0)
        t = QLabel(APP_NAME_ZH)
        t.setStyleSheet("font-size: 16px; font-weight: 700;")
        s = QLabel(APP_NAME)
        s.setObjectName("Muted")
        s.setStyleSheet("font-size: 11px;")
        names.addWidget(t)
        names.addWidget(s)
        brand.addWidget(logo)
        brand.addSpacing(8)
        brand.addLayout(names, 1)
        lay.addLayout(brand)
        lay.addSpacing(18)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QToolButton] = {}
        from .icons import line_icon

        for key, label in NAV_ITEMS:
            btn = QToolButton()
            btn.setObjectName("NavButton")
            btn.setText(f"   {label}")
            btn.setIcon(line_icon(key, "#94A3B8", 20))
            btn.setIconSize(QSize(20, 20))
            btn.setCheckable(True)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            btn.clicked.connect(lambda _=False, k=key: self._select(k))
            self.nav_group.addButton(btn)
            self.nav_buttons[key] = btn
            lay.addWidget(btn)
        lay.addStretch(1)

        self.side_status = QLabel("守护进程：检测中…")
        self.side_status.setObjectName("Muted")
        self.side_status.setWordWrap(True)
        self.side_status.setStyleSheet("font-size: 11px;")
        lay.addWidget(self.side_status)
        lay.addSpacing(6)
        ver = QLabel(f"v{__version__} · {'已打包' if getattr(sys, 'frozen', False) else '源码运行'}")
        ver.setObjectName("Muted")
        ver.setStyleSheet("font-size: 11px;")
        lay.addWidget(ver)
        return side

    def _select(self, key: str) -> None:
        order = [k for k, _ in NAV_ITEMS]
        idx = order.index(key) if key in order else 0
        self.stack.setCurrentIndex(idx)
        self.nav_buttons[key].setChecked(True)
        page = self.stack.currentWidget()
        if hasattr(page, "on_shown"):
            page.on_shown()

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+B"), self, activated=lambda: self.state.run_task(self.tasks_page.selected_task_id()) if self.tasks_page.selected_task_id() else None)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=lambda: (self._select("tasks"), self.tasks_page.new_task()))
        QShortcut(QKeySequence("F5"), self, activated=self.tasks_page.refresh)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self._select("settings"))

    # ------------------------------------------------------------------
    def _build_tray(self) -> None:
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon

        self.tray_available = True
        self.tray = QSystemTrayIcon(tray_state_icon("idle"), self)
        self.tray.setToolTip(f"{APP_NAME_ZH} · {APP_NAME}")
        self.tray.setVisible(True)
        menu = QMenu()
        self.tray_menu = menu
        act_show = QAction("打开主窗口", self)
        act_show.triggered.connect(self.show_main)
        menu.addAction(act_show)
        self.tray_tasks_menu = menu.addMenu("立即运行任务")
        menu.addSeparator()
        act_logs = QAction("查看运行记录", self)
        act_logs.triggered.connect(lambda: (self.show_main(), self._select("history")))
        menu.addAction(act_logs)
        act_folder = QAction("打开配置目录", self)
        act_folder.triggered.connect(self._open_config_dir)
        menu.addAction(act_folder)
        menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)
        self._quit_action = act_quit
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
            log.info("系统托盘已创建（visible=%s）", self.tray.isVisible())
        else:
            # 无托盘环境（部分 Linux 桌面 / 远程会话）：关闭窗口即退出，避免「关不掉」
            log.warning("系统托盘不可用，窗口关闭将直接退出")
            self.tray_available = False
        self._rebuild_tray_tasks()
        self.state.tasks_changed.connect(self._rebuild_tray_tasks)

        # 从托盘恢复窗口时若图标被系统偷走（explorer 重启、外壳刷新），自动补回
        self._tray_watch = QTimer(self)
        self._tray_watch.setInterval(30_000)
        self._tray_watch.timeout.connect(self._ensure_tray_visible)
        self._tray_watch.start()

    def _ensure_tray_visible(self) -> None:
        """确保托盘图标仍然存在（Windows 资源管理器重启会清空通知区域）。"""
        try:
            from PySide6.QtWidgets import QSystemTrayIcon

            if not self.tray_available or not QSystemTrayIcon.isSystemTrayAvailable():
                return
            if not self.tray.isVisible():
                log.info("托盘图标丢失，重新显示")
                self.tray.show()
        except Exception:  # noqa: BLE001
            log.debug("检查托盘状态失败", exc_info=True)

    def _show_tray_hint_once(self) -> None:
        cfg = self.state.store.settings
        cfg.tray_hint_shown = True
        try:
            self.state.store.save()
        except Exception:  # noqa: BLE001
            pass
        self._show_tray_hint()

    def _show_tray_hint(self) -> None:
        """首次运行时提示用户：程序在托盘里。"""
        if not self.tray_available:
            return
        try:
            self.tray.showMessage(
                APP_NAME_ZH,
                "程序已驻留系统托盘：关闭窗口后仍会按计划备份。找不到图标时点任务栏的 ▲ 展开隐藏图标。",
                self.tray.MessageIcon.Information,
                5000,
            )
        except Exception:  # noqa: BLE001
            pass

    def _rebuild_tray_tasks(self) -> None:
        from PySide6.QtGui import QAction

        m = self.tray_tasks_menu
        m.clear()
        tasks = self.state.tasks()
        if not tasks:
            a = QAction("（暂无任务）", self)
            a.setEnabled(False)
            m.addAction(a)
            return
        for t in tasks:
            a = QAction(t.name, self)
            a.triggered.connect(lambda _=False, tid=t.id: self.state.run_task(tid, trigger="manual"))
            m.addAction(a)

    def _on_tray_activated(self, reason) -> None:  # noqa: ANN001
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_main()

    def _on_notify(self, title: str, message: str, level: str) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        icon = {
            "success": QSystemTrayIcon.MessageIcon.Information,
            "error": QSystemTrayIcon.MessageIcon.Critical,
            "warning": QSystemTrayIcon.MessageIcon.Warning,
        }.get(level, QSystemTrayIcon.MessageIcon.Information)
        self.tray.showMessage(title, message, icon, 6000)
        self.tray.setIcon(tray_state_icon("success" if level == "success" else "failure"))
        self._reset_tray_icon_later()

    def _reset_tray_icon_later(self) -> None:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(8000, lambda: self.tray.setIcon(tray_state_icon("idle")))

    def _on_run_started(self, task_id: str) -> None:
        self.tray.setIcon(tray_state_icon("running"))
        t = self.state.get(task_id)
        self.tray.setToolTip(f"{APP_NAME_ZH} · 正在备份：{t.name if t else ''}")

    def _on_run_finished(self, result) -> None:  # noqa: ANN001
        self.tray.setIcon(tray_state_icon("success" if result.record.status == "success" else "failure"))
        self.tray.setToolTip(f"{APP_NAME_ZH} · {result.record.status}")

    def _open_config_dir(self) -> None:
        from ..util import open_in_file_manager
        from .appstate import config_directory

        open_in_file_manager(str(config_directory()))

    # ------------------------------------------------------------------
    def _apply_theme(self, palette) -> None:  # noqa: ANN001
        QApplication.instance().setStyleSheet(stylesheet(palette))
        self.tasks_page.set_palette_mode(palette.dark)
        self.history_page.set_palette_mode(palette.dark)
        self.env_page.set_palette_mode(palette.dark)
        self.settings_page.set_palette_mode(palette.dark)

    def show_main(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if (
            self._really_quit
            or not self.state.store.settings.close_to_tray
            or not getattr(self, "tray_available", True)
        ):
            event.accept()
            self.state.stop_scheduler()
            self.tray.hide()
            self.about_to_quit.emit()
            QApplication.instance().quit()
            return
        event.ignore()
        self.hide()
        if not getattr(self, "_tray_hint_shown", False):
            self._tray_hint_shown = True
            self.tray.showMessage(
                APP_NAME_ZH,
                "已最小化到系统托盘，备份计划仍在后台运行。右键托盘图标可退出。",
                self.tray.MessageIcon.Information,
                4000,
            )

    def quit_app(self) -> None:
        if self.tasks_page.has_running():
            ret = QMessageBox.question(
                self, "确认退出", "还有备份正在运行，现在退出会中断备份。确定退出吗？"
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        self._really_quit = True
        self.close()
