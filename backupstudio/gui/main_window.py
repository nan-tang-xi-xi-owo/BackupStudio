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

        # **1.1.2 键盘可达性**：在**所有控件创建完成之后**显式串联导航按钮的
        # Tab 顺序。
        #
        # 为什么必须放在这里：`QWidget.setTabOrder` 要求两个控件都已有父窗口，
        # 且顺序会被后续 addWidget 打乱。放在 `_build_sidebar` 里调用时，
        # `self.stack` 和四个页面还没建完，实测无效（Tab 只能走到
        # tasks → history 就被页内控件截走）。
        #
        # 为什么需要它：`QStackedWidget` 只让**当前页**的可聚焦控件进 Tab 链
        # （Qt 正常行为），所以「Tab 走遍所有页面内部控件」本身不成立；
        # 键盘用户切换页面的**唯一路径**就是这 4 个导航按钮。
        # 必须保证它们连续可达，再配合空格/回车激活。
        navs = [self.nav_buttons[k] for k, _ in NAV_ITEMS if k in self.nav_buttons]
        for i in range(len(navs) - 1):
            QWidget.setTabOrder(navs[i], navs[i + 1])

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
            # **1.1.2 键盘可达性**：互斥 QButtonGroup 在 show() 时会把
            # **未选中项**的 focusPolicy 改成 NoFocus（Qt 对单选组的既定行为）。
            # 实测：show 前 4/4 是 TabFocus，show 后只有 1/4 是 TabFocus
            # → 连续 Tab 60 次，焦点只在当前页的 4 个控件里死循环，
            # 永远切不到「运行记录 / 环境 / 设置」。
            # 这里显式设 StrongFocus，并在 showEvent 里再设一次（见 _enforce_nav_focus）。
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            btn.setAccessibleName(label)
            btn.setAccessibleDescription(f"切换到{label}页")
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            btn.clicked.connect(lambda _=False, k=key: self._select(k))
            # **1.1.2**：`QToolButton` 默认只响应**空格**，不响应**回车**。
            # Windows 用户习惯回车激活，所以各绑一个 WidgetShortcut。
            # 实测：空格 4/4 切页正常，回车 0/4（本轮补上）。
            for _key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                _sc = QShortcut(QKeySequence(_key), btn)
                _sc.setContext(Qt.ShortcutContext.WidgetShortcut)
                _sc.activated.connect(lambda _=False, k=key: self._select(k))
            self.nav_group.addButton(btn)
            self.nav_buttons[key] = btn
            lay.addWidget(btn)
        lay.addStretch(1)
        # 注：导航按钮的 setTabOrder 在 __init__ 末尾统一做 —— 那时 `self.stack`
        # 与四个页面才建好，这里调用会被后续 addWidget 打乱（实测无效）。

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
        # **1.1.2**：`setChecked(True)` 会再次触发 Qt 对互斥组「未选中项
        # 置 NoFocus」的逻辑，所以每次切页后都要把焦点策略补回来。
        self._enforce_nav_focus()
        if hasattr(page, "on_shown"):
            page.on_shown()

    def _enforce_nav_focus(self) -> None:
        """把导航按钮的 focusPolicy 强制回 StrongFocus。

        ## 为什么需要这个（1.1.2）

        Qt 对**互斥 QButtonGroup** 有个既定行为：`show()` 时会把所有
        **未选中**的可选中按钮的 `focusPolicy` 改为 `NoFocus`（这是为了让
        单选组在 Tab 链里只占一个位置，组内用方向键切换）。

        但对**页面导航**来说这是灾难：焦点只能停在当前页，其余页面
        Tab 永远到不了。实测：连续 60 次 Tab，焦点只在「任务」页的
        4 个控件（nav / 按钮 / 列表 / 滚动区）里死循环。

        隔离实验（纯 Qt，不含项目代码）确认了触发时机：
            建好未 checkable         → TabFocus ×N
            setCheckable(True)       → TabFocus ×N
            addButton（互斥组）       → TabFocus ×N   ← 不是这里
            setChecked(True)         → TabFocus ×N
            **show()**               → NoFocus ×(N-1)  ← 只在这里变

        所以必须在 show 之后重设。`showEvent` 里调用本方法是唯一可靠时机。
        """
        for btn in self.nav_buttons.values():
            if btn.focusPolicy() != Qt.FocusPolicy.StrongFocus:
                btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def showEvent(self, event) -> None:  # noqa: ANN001, N802
        super().showEvent(event)
        # 单次定时器：等 Qt 完成内部状态设置后再改，否则会被再次覆盖
        QTimer.singleShot(0, self._enforce_nav_focus)

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
            # **1.1.2 修复**：原来没传 defaultButton，Qt 默认把「是」设为默认
            # → 用户误按回车就中断正在运行的备份。危险操作必须默认「否」。
            # 对照：tasks_page 的删除确认传了 No，是对的。
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("确认退出")
            box.setText("还有备份正在运行，现在退出会中断备份。")
            box.setInformativeText("确定要退出吗？")
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.button(QMessageBox.StandardButton.Yes).setText("退出并中断")
            box.button(QMessageBox.StandardButton.No).setText("继续备份")
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
        self._really_quit = True
        self.close()
