"""设置页：常规、通知、主题、守护进程、配置导入导出、关于。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QTime

from .. import APP_NAME, APP_NAME_ZH, APP_ID, __version__
from ..autostart import autostart_available, is_autostart_enabled, set_autostart
from ..daemon import daemon_status, ensure_daemon, stop_daemon_safe
from ..flatpak import daemon_supported as daemon_available
from ..notify import describe_backend
from ..paths import config_dir, config_dir_state, log_dir
from ..util import open_in_file_manager
from .appstate import AppState
from .widgets import Card, Combo, Switch, muted, title_label

log = logging.getLogger("backupstudio.gui.settings")


class SettingsPage(QWidget):
    def __init__(self, state: AppState, window) -> None:  # noqa: ANN001
        super().__init__()
        self.state = state
        self.window = window
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 22)
        outer.setSpacing(14)
        outer.addWidget(title_label("设置", 1))

        from PySide6.QtWidgets import QScrollArea

        holder = QWidget()
        holder.setObjectName("Page")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(14)

        # 常规
        general = Card()
        general.add(title_label("常规", 3))
        self.sw_autostart = Switch(dark=state.palette.dark)
        self.sw_autostart.toggled.connect(self._on_autostart)
        self.row_autostart = self._row("开机自动启动（启动后常驻托盘）", self.sw_autostart)
        general.add(self.row_autostart)
        # N3：Flatpak 沙箱内开关**置灰**并说明原因，而不是让用户点了以后
        # 得到一个写进沙箱、宿主永远看不到的"成功"。
        from .. import flatpak as _flatpak

        if _flatpak.is_flatpak() and not _flatpak.portal_declared():
            # N4：没有门户授权时 home 之外的目录根本选不到/读不到，必须先说清楚。
            general.add(muted("⚠ " + _flatpak.portal_hint().replace("\n", " ")))
        if not autostart_available():
            self.sw_autostart.setEnabled(False)
            self.sw_autostart.setToolTip(_flatpak.autostart_hint())
            general.add(muted("⚠ " + _flatpak.autostart_hint().replace("\n", " ")))
        self.sw_start_min = Switch(dark=state.palette.dark)
        self.sw_start_min.toggled.connect(self._save)
        general.add(self._row("启动时最小化到托盘", self.sw_start_min))
        self.sw_close_tray = Switch(dark=state.palette.dark)
        self.sw_close_tray.toggled.connect(self._save)
        general.add(self._row("关闭窗口时最小化到托盘（不退出程序）", self.sw_close_tray))
        self.sw_daemon = Switch(dark=state.palette.dark)
        self.sw_daemon.toggled.connect(self._save)
        self.row_daemon = self._row(
            "同时启动后台守护进程（关闭主窗口后也能按计划备份）", self.sw_daemon
        )
        general.add(self.row_daemon)
        # N4：Flatpak 沙箱内守护进程拿不到文件门户授权，home 之外的备份会静默失败。
        # 与其"假装支持"，不如把开关置灰 + 明确指向内置调度器。
        if not daemon_available():
            self.sw_daemon.setEnabled(False)
            self.sw_daemon.setChecked(False)
            self.sw_daemon.setToolTip(_flatpak.daemon_hint())
            general.add(muted("⚠ " + _flatpak.daemon_hint().replace("\n", " ")))
        appearance = QHBoxLayout()
        appearance.addWidget(QLabel("外观主题"), 1)
        self.theme_combo = Combo()
        self.theme_combo.addItem("跟随系统", "system")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.currentIndexChanged.connect(self._on_theme)
        appearance.addWidget(self.theme_combo)
        general.add_layout(appearance)
        lay.addWidget(general)

        # 通知
        notif = Card()
        notif.add(title_label("通知", 3))
        self.sw_notif = Switch(dark=state.palette.dark)
        self.sw_notif.toggled.connect(self._save)
        notif.add(self._row("启用备份通知", self.sw_notif))
        self.sw_tray_msg = Switch(dark=state.palette.dark)
        self.sw_tray_msg.toggled.connect(self._save)
        notif.add(self._row("显示托盘气泡提示", self.sw_tray_msg))
        self.sw_native = Switch(dark=state.palette.dark)
        self.sw_native.toggled.connect(self._save)
        notif.add(self._row("同时使用系统通知（macOS: 通知中心 / Linux: notify-send）", self.sw_native))
        ev = QHBoxLayout()
        self.cb_success = Switch(dark=state.palette.dark)
        self.cb_failure = Switch(dark=state.palette.dark)
        self.cb_success.toggled.connect(self._save)
        self.cb_failure.toggled.connect(self._save)
        ev.addWidget(QLabel("通知事件"), 1)
        ev.addWidget(QLabel("成功时"))
        ev.addWidget(self.cb_success)
        ev.addSpacing(12)
        ev.addWidget(QLabel("失败时"))
        ev.addWidget(self.cb_failure)
        notif.add_layout(ev)
        self.sw_quiet = Switch(dark=state.palette.dark)
        self.sw_quiet.toggled.connect(self._save)
        notif.add(self._row("免打扰时段内不弹出通知", self.sw_quiet))
        qh = QHBoxLayout()
        qh.addWidget(QLabel("免打扰"), 1)
        self.quiet_start = QTimeEdit(QTime(23, 0))
        self.quiet_start.setDisplayFormat("HH:mm")
        self.quiet_end = QTimeEdit(QTime(7, 0))
        self.quiet_end.setDisplayFormat("HH:mm")
        self.quiet_start.timeChanged.connect(self._save)
        self.quiet_end.timeChanged.connect(self._save)
        qh.addWidget(self.quiet_start)
        qh.addWidget(QLabel("至"))
        qh.addWidget(self.quiet_end)
        notif.add_layout(qh)
        notif.add(muted(f"当前系统通知后端：{describe_backend()}"))
        lay.addWidget(notif)

        # 守护进程
        daemon = Card()
        daemon.add(title_label("后台守护进程", 3))
        daemon.add(muted("守护进程即使主窗口关闭也能按计划备份，数据通过本地 IPC 与界面通信。"))
        self.daemon_status = QLabel("检测中…")
        self.daemon_status.setStyleSheet("font-size: 13px;")
        daemon.add(self.daemon_status)
        dh = QHBoxLayout()
        self.btn_daemon_start = QPushButton("启动守护进程")
        self.btn_daemon_start.clicked.connect(self._daemon_start)
        self.btn_daemon_stop = QPushButton("停止守护进程")
        self.btn_daemon_stop.clicked.connect(self._daemon_stop)
        self.btn_daemon_refresh = QPushButton("刷新状态")
        self.btn_daemon_refresh.clicked.connect(self._refresh_daemon)
        dh.addWidget(self.btn_daemon_start)
        dh.addWidget(self.btn_daemon_stop)
        dh.addWidget(self.btn_daemon_refresh)
        dh.addStretch(1)
        daemon.add_layout(dh)
        if not daemon_available():
            from .. import flatpak

            self.btn_daemon_start.setEnabled(False)
            self.btn_daemon_stop.setEnabled(False)
            self.btn_daemon_start.setToolTip(flatpak.daemon_hint())
        lay.addWidget(daemon)

        # 数据与日志
        data = Card()
        data.add(title_label("数据与日志", 3))
        self.lbl_config = QLabel("")
        self.lbl_config.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_logs = QLabel("")
        self.lbl_logs.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        data.add(self._row("配置目录", self.lbl_config))
        data.add(self._row("日志目录", self.lbl_logs))
        dh2 = QHBoxLayout()
        b1 = QPushButton("打开配置目录")
        b1.clicked.connect(lambda: open_in_file_manager(str(config_dir())))
        b2 = QPushButton("打开日志目录")
        b2.clicked.connect(lambda: open_in_file_manager(str(log_dir())))
        b3 = QPushButton("清理旧日志")
        b3.clicked.connect(self._clean_logs)
        dh2.addWidget(b1)
        dh2.addWidget(b2)
        dh2.addWidget(b3)
        dh2.addStretch(1)
        data.add_layout(dh2)
        lay.addWidget(data)

        # 导入导出
        io_card = Card()
        io_card.add(title_label("任务备份与迁移", 3))
        io_card.add(muted("导出的 JSON 包含全部任务配置；如需迁移到另一台电脑，可选择包含密码（注意保管）。"))
        ih = QHBoxLayout()
        b_exp = QPushButton("导出任务…")
        b_exp.clicked.connect(self._export)
        b_imp = QPushButton("导入任务…")
        b_imp.clicked.connect(self._import)
        ih.addWidget(b_exp)
        ih.addWidget(b_imp)
        ih.addStretch(1)
        io_card.add_layout(ih)
        lay.addWidget(io_card)

        # 关于
        about = Card()
        about.add(title_label("关于", 3))
        about.add(QLabel(f"{APP_NAME_ZH} · {APP_NAME} {__version__}"))
        about.add(muted(
            f"应用 ID：{APP_ID}\n"
            "跨平台文件夹备份与多格式压缩工具。GUI 基于 PySide6；7-Zip 由 Igor Pavlov 开发（LGPL，"
            "RAR 为 RARLAB 专有软件，需自行安装）。"
        ))
        bh = QHBoxLayout()
        b_lic = QPushButton("第三方许可")
        b_lic.clicked.connect(self._show_licenses)
        b_about = QPushButton("环境与压缩支持")
        b_about.clicked.connect(self._goto_env)
        bh.addWidget(b_lic)
        bh.addWidget(b_about)
        bh.addStretch(1)
        about.add_layout(bh)
        lay.addWidget(about)
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        self._load()

    # ------------------------------------------------------------------
    def _row(self, text: str, widget: QWidget) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lay.addWidget(lbl, 1)
        lay.addWidget(widget)
        return w

    def _load(self) -> None:
        self._loading = True
        s = self.state.store.settings
        self.sw_start_min.setChecked(s.start_minimized)
        self.sw_close_tray.setChecked(s.close_to_tray)
        self.sw_daemon.setChecked(s.auto_run_backup_daemon)
        self.theme_combo.select_data(s.theme_mode)
        n = s.notifications
        self.sw_notif.setChecked(n.enabled)
        self.sw_tray_msg.setChecked(n.tray)
        self.sw_native.setChecked(n.native)
        self.cb_success.setChecked("success" in n.events)
        self.cb_failure.setChecked("failure" in n.events)
        self.sw_quiet.setChecked(n.quiet_hours)
        self.quiet_start.setTime(QTime(*_parse(n.quiet_start, (23, 0))))
        self.quiet_end.setTime(QTime(*_parse(n.quiet_end, (7, 0))))
        try:
            self.sw_autostart.blockSignals(True)
            self.sw_autostart.setChecked(is_autostart_enabled())
        finally:
            self.sw_autostart.blockSignals(False)
        self.lbl_config.setText(str(config_dir()))
        self.lbl_logs.setText(str(log_dir()))
        # N2：配置目录发生回退时必须让用户看见（回退位置可能是 tmpfs，重启即丢）。
        st = config_dir_state()
        if st.get("fell_back"):
            self.lbl_config.setText(
                f"{st.get('actual')}  ⚠ 已回退（首选 {st.get('preferred')} 不可写）"
            )
            self.lbl_config.setStyleSheet("color:#B45309;")
            self.lbl_config.setToolTip(str(st.get("reason") or ""))
        self._loading = False
        self._refresh_daemon()

    def _save(self, *_args) -> None:
        if self._loading:
            return
        s = self.state.store.settings
        s.start_minimized = self.sw_start_min.isChecked()
        s.close_to_tray = self.sw_close_tray.isChecked()
        # N4：沙箱内不落盘守护进程开关（否则每次启动都会去拉起一个注定拿不到
        # 门户授权的子进程，用户看到的是"定时备份永不执行"且没有提示）。
        s.auto_run_backup_daemon = self.sw_daemon.isChecked() if daemon_available() else False
        s.theme_mode = self.theme_combo.currentData() or "system"
        n = s.notifications
        n.enabled = self.sw_notif.isChecked()
        n.tray = self.sw_tray_msg.isChecked()
        n.native = self.sw_native.isChecked()
        events = []
        if self.cb_success.isChecked():
            events.append("success")
        if self.cb_failure.isChecked():
            events.append("failure")
        n.events = events or ["failure"]
        n.quiet_hours = self.sw_quiet.isChecked()
        n.quiet_start = self.quiet_start.time().toString("HH:mm")
        n.quiet_end = self.quiet_end.time().toString("HH:mm")
        self.state.save_settings(s)

    def _on_theme(self, *_args) -> None:
        if self._loading:
            return
        self._save()
        self.state.set_theme(self.theme_combo.currentData() or "system")

    def _on_autostart(self, checked: bool) -> None:
        if self._loading:
            return
        ok, msg = set_autostart(checked)
        if ok:
            # 只有真的写成功才落盘，避免"设置显示已开启、实际没生效"。
            self.state.store.settings.start_with_system = checked
            self.state.store.save()
            QMessageBox.information(self, "开机自启", msg)
        else:
            QMessageBox.warning(self, "设置失败", msg)
            self.sw_autostart.blockSignals(True)
            self.sw_autostart.setChecked(is_autostart_enabled())
            self.sw_autostart.blockSignals(False)

    # ------------------------------------------------------------------
    def _refresh_daemon(self) -> None:
        """在后台线程查询，避免进程枚举拖慢界面。"""
        from PySide6.QtCore import QThread
        from PySide6.QtCore import Signal as _Signal

        outer = self

        class _Probe(QThread):
            done = _Signal(object)

            def run(self) -> None:  # noqa: D401
                try:
                    outer._probe_result = daemon_status(timeout_ms=600)
                except Exception as exc:  # noqa: BLE001
                    outer._probe_result = {"running": False, "error": str(exc)}
                self.done.emit(outer._probe_result)

        probe = _Probe(self)
        probe.done.connect(lambda st: self._render_daemon(st))
        probe.start()
        self._daemon_probe = probe
        self.daemon_status.setText("检测中…")

    def _render_daemon(self, st: dict) -> None:
        if not daemon_available():
            from .. import flatpak

            self.daemon_status.setText(
                "<b style='color:#B45309'>使用内置调度器</b><br>"
                + flatpak.daemon_hint().replace("\n", "<br>")
            )
            self.daemon_status.setTextFormat(Qt.TextFormat.RichText)
            return
        if "error" in st:
            self.daemon_status.setText(f"状态未知：{st['error']}")
            return
        if st.get("running"):
            lines = [f"<b style='color:#16A34A'>运行中</b>（PID {st.get('pid')}）"]
            for t in st.get("tasks", []):
                lines.append(
                    f"&nbsp;&nbsp;• {t.get('name')}：{t.get('status') or '等待'}，下次 {t.get('next') or '—'}"
                )
            self.daemon_status.setText("<br>".join(lines))
            self.daemon_status.setTextFormat(Qt.TextFormat.RichText)
        else:
            self.daemon_status.setText("<b style='color:#DC2626'>未运行</b>（关闭主窗口后计划不会执行）")
            self.daemon_status.setTextFormat(Qt.TextFormat.RichText)

    def _daemon_start(self) -> None:
        ok, msg = ensure_daemon()
        if ok:
            QMessageBox.information(self, "守护进程", msg)
        else:
            # N4：Flatpak 内会返回「请使用内置调度器」的指引，这里要按警告展示，
            # 不能再用 information 图标假装成功。
            QMessageBox.warning(self, "守护进程不可用", msg)
        self._refresh_daemon()

    def _daemon_stop(self) -> None:
        ok, msg = stop_daemon_safe()
        QMessageBox.information(self, "守护进程", msg)
        self._refresh_daemon()

    def _clean_logs(self) -> None:
        kept = 0
        removed = 0
        try:
            for f in log_dir().glob("*.log*"):
                try:
                    if f.stat().st_size == 0:
                        f.unlink()
                        removed += 1
                    else:
                        kept += 1
                except OSError:
                    continue
        except OSError as exc:
            QMessageBox.warning(self, "清理失败", str(exc))
            return
        QMessageBox.information(self, "完成", f"已清理 {removed} 个空日志文件。运行中的日志保留。")

    # ------------------------------------------------------------------
    def _export(self) -> None:
        dlg = QFileDialog(self, "导出任务", str(Path.home() / "backupstudio-tasks.json"), "JSON (*.json)")
        dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        if not dlg.exec():
            return
        path = dlg.selectedFiles()[0]
        include_pw = QMessageBox.question(
            self, "是否包含密码", "导出的文件是否包含备份密码？\n包含后任何拿到该文件的人都能解开备份。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes
        try:
            self.state.store.export_bundle(Path(path), include_passwords=include_pw)
            QMessageBox.information(self, "导出成功", f"已导出到：\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(exc))

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入任务", str(Path.home()), "JSON (*.json)")
        if not path:
            return
        try:
            count = self.state.store.import_bundle(Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", f"无法读取文件：\n{exc}")
            return
        self.state.tasks_changed.emit()
        QMessageBox.information(self, "导入完成", f"已导入 {count} 个任务。")

    def _show_licenses(self) -> None:
        text = _licenses_text()
        dlg = QDialog(self)
        dlg.setWindowTitle("第三方许可")
        dlg.resize(760, 560)
        lay = QVBoxLayout(dlg)
        box = QTextEdit()
        box.setReadOnly(True)
        box.setPlainText(text)
        box.setStyleSheet("font-family:'Cascadia Mono','Consolas',monospace; font-size: 12px;")
        lay.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        lay.addWidget(buttons)
        dlg.exec()

    def _goto_env(self) -> None:
        self.window._select("env")

    # ------------------------------------------------------------------
    def set_palette_mode(self, dark: bool) -> None:
        for sw in (self.sw_autostart, self.sw_start_min, self.sw_close_tray, self.sw_daemon,
                   self.sw_notif, self.sw_tray_msg, self.sw_native, self.sw_quiet,
                   self.cb_success, self.cb_failure):
            sw.set_dark(dark)

    def on_shown(self) -> None:
        self._refresh_daemon()


def _parse(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = str(text).split(":")
        return int(h) % 24, int(m) % 60
    except (ValueError, AttributeError):
        return default


def _license_search_dirs() -> list[Path]:
    """第三方许可文本的查找目录（源码运行 & 冻结产物都要能命中）。

    N8 背景：旧实现只看 ``Path(__file__).parent.parent.parent``，
    源码运行时是仓库根（``packaging/THIRD-PARTY-7ZIP.txt`` 能命中），
    但 PyInstaller 冻结后它是 ``_internal/``，而 build.py 当时只把许可复制到
    **产物根目录** → 打包版设置页的「第三方许可」对话框里看不到 7-Zip 许可全文，
    等于分发了 LGPL 二进制却没随附许可。现在两边都找，且 build.py 两处都复制。
    """
    dirs: list[Path] = []
    pkg_parent = Path(__file__).resolve().parent.parent.parent
    dirs.append(pkg_parent)
    # 冻结形态：sys._MEIPASS 指向 _internal；可执行文件旁是产物根目录
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    # 去重但保持顺序
    seen: set[Path] = set()
    unique: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def _licenses_text() -> str:
    parts = []
    candidates = [
        ("THIRD-PARTY-NOTICES.txt", ("THIRD-PARTY-NOTICES.txt", "packaging/THIRD-PARTY-NOTICES.txt")),
        ("THIRD-PARTY-7ZIP.txt", ("THIRD-PARTY-7ZIP.txt", "packaging/THIRD-PARTY-7ZIP.txt")),
    ]
    for label, relatives in candidates:
        for base in _license_search_dirs():
            hit = next((base / rel for rel in relatives if (base / rel).is_file()), None)
            if hit is None:
                continue
            try:
                parts.append(f"===== {label} =====\n" + hit.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            break
    parts.append(
        "===== 组件概览 =====\n"
        "* PySide6 / Qt 6 — LGPLv3（动态链接，二进制分发时随附本说明）\n"
        "* 7-Zip（7za）— GNU LGPL + unRAR 限制\n"
        "* py7zr / pyzstd / zstandard / pyzmq / psutil / rarfile — 各自的开源许可\n"
        "* RAR — RARLAB 专有软件，未随程序分发，需用户自行安装\n"
    )
    return "\n\n".join(parts)
