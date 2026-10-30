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
        # **1.1.2 自适应尺寸**：固定 1180×760 在 1366×768 / 1280×800 这类屏上
        # 加上任务栏就装不下（用户实测窗口被压到 991×518，比最小尺寸还矮）。
        # 改为按当前屏幕可用区域取「理想尺寸与可用区域的较小值」，
        # 并保证窗口不会超出屏幕。
        self.setWindowTitle(f"{APP_NAME_ZH} · {APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        avail = QApplication.primaryScreen().availableGeometry()
        # 兜底：离屏/最小平台会报极小的可用区域（实测 240×320），
        # 直接用会把窗口算成 900×600 甚至更小。低于阈值时按默认值。
        avail_w = avail.width() if avail.width() >= 800 else 1440
        avail_h = avail.height() if avail.height() >= 600 else 900
        w = min(1180, max(900, int(avail_w * 0.86)))
        h = min(760, max(600, int(avail_h * 0.88)))
        self.resize(w, h)
        self.setMinimumSize(min(900, avail_w), min(600, avail_h))

        # 1.1.3：玻璃风格需要「背后有内容」才有折射感。
        # 非玻璃风格仍是普通 QWidget（零开销）。
        from .styles import current as _style_current

        if _style_current().translucent:
            central = _Backdrop(state)
        else:
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

        # **1.1.2 启动性能**：只立刻建「任务」页，其余三页**懒加载**。
        #
        # 实测 MainWindow 构造 2.08 秒，其中 0.89 秒花在
        # `format_capabilities()` + `environment_report()`（探测 7z/rar/zip 能力），
        # 而环境页和任务对话框各调一次。首屏只需要任务页，没理由等它们。
        #
        # 懒加载后：启动只付任务页的成本；用户点开对应页才付出探测成本。
        # `_page()` 保证四页在第一次被访问时创建，且顺序与 NAV_ITEMS 一致
        # （`_select` 依赖 index，见下方 `_page_index`）。
        from .tasks_page import TasksPage

        self.tasks_page = TasksPage(state, self)
        self.stack.addWidget(self.tasks_page)
        self._pages: dict[str, object] = {"tasks": self.tasks_page}
        # 占位页：让 stack 的索引与 NAV_ITEMS 对齐（懒加载时逐个替换）
        self._placeholders: dict[str, QWidget] = {}
        for key, _label in NAV_ITEMS:
            if key == "tasks":
                continue
            ph = QWidget()
            ph.setObjectName("Page")
            self.stack.addWidget(ph)
            self._placeholders[key] = ph

        self._select("tasks")
        self._build_tray()
        self._install_shortcuts()
        # 空闲时预热其它页面：用户点过去时不再等探测（不阻塞首屏）
        QTimer.singleShot(1200, self._prewarm_pages)
        # 第一次运行时提示「程序在托盘里」，只提示一次
        if not state.store.settings.tray_hint_shown and not state.store.settings.start_minimized:
            QTimer.singleShot(1500, self._show_tray_hint_once)

        state.notified.connect(self._on_notify)
        state.theme_changed.connect(self._apply_theme)
        state.daemon_state_changed.connect(self._on_daemon_state)
        state.run_started.connect(self._on_run_started)
        state.run_finished.connect(self._on_run_finished)
        self._apply_theme(state.palette)

        # 1.1.2：守护进程状态轮询（原来 UI 从不主动探测，底栏永远是"检测中…"）
        self._daemon_kind = "muted"
        self._daemon_timer = QTimer(self)
        self._daemon_timer.setInterval(8000)
        self._daemon_timer.timeout.connect(lambda: self.state.poll_daemon_status())
        self._daemon_timer.start()
        QTimer.singleShot(400, lambda: self.state.poll_daemon_status())

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
        side.setFixedWidth(224)
        side.setStyleSheet("QFrame#Card { border-radius: 0; border: none; }")
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 18, 14, 14)
        lay.setSpacing(6)

        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(QSize(34, 34)))
        names = QVBoxLayout()
        names.setSpacing(1)
        t = QLabel(APP_NAME_ZH)
        t.setStyleSheet("font-size: 12pt; font-weight: 700;")
        s = QLabel(APP_NAME)
        s.setObjectName("Muted")
        s.setStyleSheet("font-size: 8pt;")
        names.addWidget(t)
        names.addWidget(s)
        brand.addWidget(logo)
        brand.addSpacing(9)
        brand.addLayout(names, 1)
        lay.addLayout(brand)
        lay.addSpacing(16)

        # 1.1.2：加分组标题，导航区与品牌区有视觉分隔
        cap = QLabel("导航")
        cap.setObjectName("Caption")
        lay.addWidget(cap)
        lay.addSpacing(2)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QToolButton] = {}
        from .icons import line_icon

        for key, label in NAV_ITEMS:
            btn = QToolButton()
            btn.setObjectName("NavButton")
            btn.setText(f"   {label}")
            btn.setIcon(line_icon(key, "#94A3B8", 20))
            btn.setIconSize(QSize(18, 18))
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

        # 1.1.2：底部状态收进一张小卡片（原来两行裸文字，显得松散）
        foot = QFrame()
        foot.setObjectName("SideFooter")
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(10, 8, 10, 8)
        fl.setSpacing(3)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.side_dot = QLabel("●")
        self.side_dot.setFixedWidth(9)
        self.side_status = QLabel("守护进程：检测中…")
        self.side_status.setObjectName("Muted")
        self.side_status.setWordWrap(True)
        self.side_status.setStyleSheet("font-size: 8pt;")
        row.addWidget(self.side_dot, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.side_status, 1)
        fl.addLayout(row)

        ver = QLabel(f"v{__version__} · {'已打包' if getattr(sys, 'frozen', False) else '源码运行'}")
        ver.setObjectName("Muted")
        ver.setStyleSheet("font-size: 8pt;")
        fl.addWidget(ver)
        lay.addWidget(foot)
        self._set_side_state("muted")
        return side

    def _set_side_state(self, kind: str) -> None:
        """底栏状态点颜色（1.1.2：原来永远是"检测中…"，因为信号没接）。"""
        from . import DARK as _D
        from . import LIGHT as _L

        pal = _D if getattr(self.state, "palette", _L).dark else _L
        color = {
            "ok": pal.success,
            "bad": pal.danger,
            "warn": pal.warning,
            "info": pal.accent,
        }.get(kind, pal.text_dim)
        if hasattr(self, "side_dot"):
            self.side_dot.setStyleSheet(f"color: {color}; font-size: 8pt;")

    # ------------------------------------------------------------------
    def _make_page(self, key: str):  # noqa: ANN201
        """按需创建页面（懒加载，1.1.2）。"""
        from .environment_page import EnvironmentPage
        from .history_page import HistoryPage
        from .settings_page import SettingsPage

        maker = {
            "history": HistoryPage,
            "env": EnvironmentPage,
            "settings": SettingsPage,
        }[key]
        page = maker(self.state, self)
        self._pages[key] = page
        # 新建的页面要立刻跟上当前主题（懒加载不会走 _apply_theme）
        if hasattr(self, "_pal") and hasattr(page, "set_palette_mode"):
            try:
                page.set_palette_mode(self._pal.dark)
            except Exception:  # noqa: BLE001
                log.debug("页面应用主题失败：%s", key, exc_info=True)
        # 1.1.3：懒加载出来的新页面也要装玻璃层
        if getattr(self, "_pal", None) is not None:
            QTimer.singleShot(0, lambda: self._install_glass(self._pal))
        return page

    def _page(self, key: str) -> QWidget:
        """取页面，未创建则创建并替换掉占位页。"""
        page = self._pages.get(key)
        if page is not None:
            return page  # type: ignore[return-value]
        page = self._make_page(key)
        idx = [k for k, _ in NAV_ITEMS].index(key)
        old = self.stack.widget(idx)
        self.stack.insertWidget(idx, page)  # type: ignore[arg-type]
        self.stack.removeWidget(old)
        old.deleteLater()
        self._placeholders.pop(key, None)
        return page  # type: ignore[return-value]

    def _prewarm_pages(self) -> None:
        """空闲预热：把还没建的页面建出来，用户点过去时不再等。

        放在 1200ms 后，避开首屏渲染；失败也不影响使用。
        """
        for key, _label in NAV_ITEMS:
            if key in self._pages:
                continue
            try:
                self._page(key)
            except Exception:  # noqa: BLE001
                log.debug("预热页面失败：%s", key, exc_info=True)

    # 属性代理：其它地方仍可用 `window.history_page` 这种写法
    @property
    def history_page(self):  # noqa: ANN201
        return self._page("history")

    @property
    def env_page(self):  # noqa: ANN201
        return self._page("env")

    @property
    def settings_page(self):  # noqa: ANN201
        return self._page("settings")

    def _select(self, key: str) -> None:
        order = [k for k, _ in NAV_ITEMS]
        idx = order.index(key) if key in order else 0
        # 懒加载：先确保目标页已创建（否则会切到空白占位页）
        self._page(key)
        self.stack.setCurrentIndex(idx)
        self.nav_buttons[key].setChecked(True)
        # 选中项图标换成强调色（视觉上"当前在哪"更明确）
        try:
            from .icons import line_icon

            pal = self.state.palette
            for k2, b2 in self.nav_buttons.items():
                b2.setIcon(line_icon(k2, pal.accent if k2 == key else pal.text_dim, 18))
        except Exception:  # noqa: BLE001
            pass
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
        # 1.1.3：玻璃层必须在页面/卡片都创建后再装
        # （`_apply_theme` 在 __init__ 早期调用，那时还只有占位页）
        QTimer.singleShot(0, lambda: self._install_glass(self.state.palette))

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
        # **1.1.2 启动性能**：只对**已创建**的页面调 set_palette_mode。
        # 原来这里直接访问 `self.history_page` / `self.env_page` /
        # `self.settings_page`，在懒加载下会立刻把它们建出来（环境页的
        # 能力探测要 0.9 秒），等于把懒加载省下的时间又花回去。
        # 未创建的页面在 `_page()` 里创建后会自行应用当前主题。
        QApplication.instance().setStyleSheet(stylesheet(palette))
        self._pal = palette
        # 导航图标跟随主题（原先写死 #94A3B8）
        from .icons import line_icon

        for key, btn in getattr(self, "nav_buttons", {}).items():
            color = palette.accent if btn.isChecked() else palette.text_dim
            btn.setIcon(line_icon(key, color, 18))
        if hasattr(self, "side_dot"):
            self._set_side_state(getattr(self, "_daemon_kind", "muted"))
        for page in getattr(self, "_pages", {}).values():
            if hasattr(page, "set_palette_mode"):
                page.set_palette_mode(palette.dark)
        # 1.1.3：玻璃风格下给卡片装折射层
        try:
            self._install_glass(palette)
        except Exception:  # noqa: BLE001
            log.debug("玻璃层安装失败", exc_info=True)

    def _install_glass(self, palette) -> None:  # noqa: ANN001
        """玻璃风格：让每张 Card 自绘折射背景（1.1.3）。

        **为什么不用覆盖层控件**：按钮通常是 Card 的**孙控件**
        （Card → layout → QPushButton），`lower()` 只能降到直接子级之下，
        实测把「立即备份」压成模糊影子 —— 试过 `stackUnder(子控件)` 与
        `lower()+raise_()` 都不行。自绘没有额外控件，也就没有 z-order 问题。
        """
        from .styles import current as _st
        from .widgets import Card

        on = _st().translucent
        alpha = 0.62 if not palette.dark else 0.55
        for card in self.findChildren(Card):
            card.set_glass(on, radius=_st().radius, tint=palette.surface,
                           alpha=alpha, dark=palette.dark)

    def _on_daemon_state(self, info) -> None:  # noqa: ANN001
        """底栏守护进程状态（1.1.2：此前信号没人接，永远显示"检测中…"）。"""
        if not isinstance(info, dict):
            return
        running = bool(info.get("running"))
        err = info.get("error") or ""
        if running:
            self._daemon_kind = "ok"
            pid = info.get("pid")
            self.side_status.setText(f"守护进程运行中{f'（PID {pid}）' if pid else ''}")
        else:
            self._daemon_kind = "warn" if err else "muted"
            self.side_status.setText("守护进程未运行" if not err else "无法探测守护进程")
            self.side_status.setToolTip(err)
        self._set_side_state(self._daemon_kind)

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

class _Backdrop(QWidget):
    """玻璃风格的背景：深/浅底色 + 2~3 个柔光斑。

    光斑用径向渐变画大圆，模糊后成为玻璃的折射源。
    只在 `glass` 风格下创建。
    """

    def __init__(self, state) -> None:  # noqa: ANN001
        super().__init__()
        self.setObjectName("Page")
        self.state = state
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._pal = state.palette
        state.theme_changed.connect(self._on_theme)

    def _on_theme(self, palette) -> None:  # noqa: ANN001
        self._pal = palette
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        from PySide6.QtGui import QColor, QPainter, QRadialGradient

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self._pal
        p.fillRect(self.rect(), QColor(pal.bg))

        w, h = self.width(), self.height()
        # 光斑配色：用主题强调色/成功色/警告色的低饱和版本
        spots = (
            (int(w * 0.16), int(h * 0.10), int(max(w, h) * 0.55), pal.accent, 0.30),
            (int(w * 0.88), int(h * 0.26), int(max(w, h) * 0.42), pal.success, 0.18),
            (int(w * 0.62), int(h * 0.95), int(max(w, h) * 0.50), pal.warning, 0.14),
            (int(w * 0.34), int(h * 0.72), int(max(w, h) * 0.38), pal.accent_hover, 0.16),
        )
        for cx, cy, r, color, alpha in spots:
            g = QRadialGradient(cx, cy, r)
            c = QColor(color)
            c.setAlphaF(alpha)
            g.setColorAt(0.0, c)
            g.setColorAt(0.55, QColor(color).darker(100))
            c2 = QColor(color)
            c2.setAlphaF(0.0)
            g.setColorAt(0.55, c2)
            g.setColorAt(1.0, c2)
            p.setBrush(g)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        p.end()
