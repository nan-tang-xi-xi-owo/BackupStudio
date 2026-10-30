"""环境与压缩支持页：系统/发行版检测、压缩组件矩阵、缺失软件与安装建议。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QScrollArea,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..capabilities import (
    ENCRYPTION_LABELS,
    FORMAT_LABELS,
    MULTIVOLUME_LABELS,
    capability_report_text,
    environment_report,
    format_capabilities,
    format_status_line,
    missing_requirements,
    package_hint,
    reset_cache,
)
from ..platform_info import describe_environment, get_platform_info
from .appstate import AppState
from .icons import line_icon
from .widgets import Card, muted, title_label

log = logging.getLogger("backupstudio.gui.env")


@dataclass
class _ProbeResult:
    """一次环境探测的完整产物（在后台线程里构造，回到 UI 线程再渲染）。"""

    caps: dict
    env: dict
    info: object
    rows: list


class EnvironmentPage(QWidget):
    #: 后台探测完成（参数是 _ProbeResult 或 Exception）。
    #: 用 object 而不是具体类型：Qt 信号跨线程传递时不做类型检查更稳。
    _probe_done = Signal(object)

    def __init__(self, state: AppState, window) -> None:  # noqa: ANN001
        super().__init__()
        self.state = state
        self.window = window
        self._caps = {}
        self._probe_busy = False
        #: 上次探测是否失败（失败后切页不再自动重试，避免每次白跑 1 秒）
        self._probe_failed = False
        self._probe_done.connect(self._apply_probe)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; } QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        self._content = QWidget()
        self._content.setObjectName("Page")
        outer_layout.addWidget(scroll)
        scroll.setWidget(self._content)

        lay = QVBoxLayout(self._content)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(14)

        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(title_label("环境与压缩支持", 1))
        self.os_label = muted("")
        col.addWidget(self.os_label)
        head.addLayout(col, 1)
        self.btn_rescan = QPushButton("重新检测")
        self.btn_rescan.setIcon(line_icon("settings", "#64748B", 16))
        self.btn_rescan.clicked.connect(lambda: self.rescan(force=True))
        head.addWidget(self.btn_rescan)
        lay.addLayout(head)

        # 系统信息
        sys_card = Card()
        sys_card.add(title_label("系统信息", 3))
        self.sys_holder = QWidget()
        self.sys_grid = QGridLayout(self.sys_holder)
        self.sys_grid.setHorizontalSpacing(20)
        self.sys_grid.setVerticalSpacing(8)
        sys_card.add(self.sys_holder)
        self.distro_note = QLabel("")
        self.distro_note.setWordWrap(True)
        self.distro_note.setStyleSheet("background: rgba(79,107,237,0.10); border-radius: 10px; padding: 10px 12px;")
        sys_card.add(self.distro_note)
        lay.addWidget(sys_card)

        # 压缩组件
        comp_card = Card()
        comp_card.add(title_label("压缩组件", 3))
        self.comp_table = QTableWidget(0, 3)
        self.comp_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.comp_table.setHorizontalHeaderLabels(["组件", "状态", "路径 / 版本"])
        self.comp_table.verticalHeader().setVisible(False)
        self.comp_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.comp_table.setShowGrid(False)
        self.comp_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.comp_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.comp_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.comp_table.horizontalHeader().setStretchLastSection(True)
        self.comp_table.setFixedHeight(240)
        comp_card.add(self.comp_table)
        lay.addWidget(comp_card)

        # 格式矩阵
        fmt_card = Card()
        fmt_card.add(title_label("压缩格式支持", 3))
        self.fmt_table = QTableWidget(0, 4)
        self.fmt_table.setHorizontalHeaderLabels(["格式", "状态", "分卷", "密码"])
        self.fmt_table.verticalHeader().setVisible(False)
        self.fmt_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.fmt_table.setShowGrid(False)
        self.fmt_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.fmt_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.fmt_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.fmt_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.fmt_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.fmt_table.setMinimumHeight(max(300, self.fmt_table.rowCount() * 30 + 60))
        fmt_card.add(self.fmt_table)
        lay.addWidget(fmt_card, 2)

        # 缺失软件
        miss_card = Card()
        miss_card.add(title_label("缺失的软件与安装建议", 3))
        self.miss_box = QWidget()
        self.miss_lay = QVBoxLayout(self.miss_box)
        self.miss_lay.setContentsMargins(0, 0, 0, 0)
        self.miss_lay.setSpacing(8)
        miss_card.add(self.miss_box)
        self.miss_ok = QLabel("")
        miss_card.add(self.miss_ok)
        lay.addWidget(miss_card)

        # 原始报告
        raw_card = Card()
        raw_head = QHBoxLayout()
        raw_head.addWidget(title_label("诊断报告（可复制反馈）", 3), 1)
        self.btn_copy = QPushButton("复制报告")
        self.btn_copy.clicked.connect(self._copy_report)
        raw_head.addWidget(self.btn_copy)
        raw_card.add_layout(raw_head)
        self.raw = QTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setFixedHeight(170)
        self.raw.setStyleSheet("font-family: 'Cascadia Mono','Consolas','Menlo',monospace; font-size: 12px;")
        raw_card.add(self.raw)
        lay.addWidget(raw_card)

        self.rescan(force=True)  # 首次进入：清缓存并后台探测（不阻塞启动）

    # ------------------------------------------------------------------
    def set_palette_mode(self, dark: bool) -> None:
        return

    def on_shown(self) -> None:
        """切页时**不清缓存**、**不阻塞 UI**。

        环境不会在两次点击之间变化：真正需要重扫的场景只有"用户刚装了 7-Zip"，
        而那由「重新检测」按钮负责（会传 force=True）。

        原来的实现每次切页都 ``reset_cache()`` 再把六项外部探测串行跑一遍，
        实测阻塞 UI 线程 921–1237 ms（标题栏显示「未响应」）。
        """
        if self._caps:  # 已有结果直接复用，切页零成本
            return
        if self._probe_failed:
            # 上一次探测失败时不要每次切页都重试：那是白烧 1 秒的后台 CPU，
            # 且用户看不到任何变化。要么点「重新检测」，要么看诊断报告里的原因。
            return
        self.rescan(force=False)

    def rescan(self, force: bool = False) -> None:
        """重新探测环境。

        ``force=True``（用户点「重新检测」）：清空所有缓存后重探；
        ``force=False``（首次进入页面）：用现有缓存，并在后台线程里跑。

        探测本身要启动 7za/unrar/tar 子进程并查注册表，实测串行约 1.3 s，
        **绝不能放在 UI 线程**——那会让窗口变成「未响应」。
        """
        if self._probe_busy:
            return  # 上一次还没回来，不重复发起
        self._probe_busy = True
        self._probe_failed = False  # 新一轮探测开始，清掉上次的失败标记
        if force:
            self.btn_rescan.setEnabled(False)
            self.btn_rescan.setText("检测中…")

        def _work() -> None:
            # 注意：reset_cache 会清 lru_cache，只在 worker 里、且只在 force 时做，
            # 且 worker 内不会再被别的探测并发调用（_probe_busy 已互斥）。
            if force:
                reset_cache()
            try:
                data = _ProbeResult(
                    caps=format_capabilities(force=False),
                    env=environment_report(),
                    info=get_platform_info(),
                    rows=describe_environment(),
                )
            except Exception as exc:  # noqa: BLE001
                data = exc
            try:
                self._probe_done.emit(data)
            except RuntimeError:  # 页面已销毁（C++ 对象没了）
                pass

        threading.Thread(target=_work, daemon=True, name="bs-env-probe").start()

    @Slot(object)
    def _apply_probe(self, data: object) -> None:
        """后台探测完成，回到 UI 线程渲染。"""
        self._probe_busy = False
        self.btn_rescan.setEnabled(True)
        self.btn_rescan.setText("重新检测")
        if isinstance(data, Exception):
            log.warning("环境探测失败：%s", data)
            self._probe_failed = True
            self.raw.setPlainText(f"环境探测失败：{data}")
            return
        # 显式判断而不是 assert：assert 在 python -O 下会被移除，
        # 那时 data.caps 会变成对任意对象的属性访问并抛出难懂的 AttributeError。
        if not isinstance(data, _ProbeResult):
            log.warning("环境探测返回了意外类型 %r，已忽略", type(data))
            return
        self._render(data.caps, data.env, data.info, data.rows)

    def _render(self, caps, env, info, rows) -> None:  # noqa: ANN001
        self._caps = caps
        self.os_label.setText(info.label)

        # 系统信息网格
        while self.sys_grid.count():
            item = self.sys_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        cols = 3
        for i, (k, v) in enumerate(rows):
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(0, 0, 8, 0)
            cl.setSpacing(1)
            key = QLabel(k)
            key.setObjectName("Muted")
            key.setStyleSheet("font-size: 11px;")
            val = QLabel(v or "-")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setStyleSheet("font-size: 13px; font-weight: 600;")
            cl.addWidget(key)
            cl.addWidget(val)
            self.sys_grid.addWidget(cell, i // cols, i % cols)
        for c in range(cols):
            self.sys_grid.setColumnStretch(c, 1)
        self.distro_note.setText(self._distro_note(info, env))

        # 组件表
        rar_path = env.get("rar")
        rar_label = rar_path
        if rar_path and not env.get("rar_can_write"):
            rar_label = f"{rar_path}（未注册试用版，只能解压）"
        comps = [
            ("7-Zip（7z/7za）", env.get("seven_zip"), env.get("seven_zip_version", "")),
            ("RAR（WinRAR）", rar_label, env.get("rar_version", "")),
            ("UnRAR", env.get("unrar"), ""),
            ("tar", env.get("tar"), "GNU tar" if env.get("gnu_tar") else "bsdtar"),
            ("GPG", env.get("gpg"), ""),
            ("zstandard 模块", "内置" if env.get("zstd_module") else None, "Python zstandard"),
        ]
        self.comp_table.setRowCount(len(comps))
        for i, (name, path, ver) in enumerate(comps):
            self.comp_table.setItem(i, 0, QTableWidgetItem(name))
            ok = bool(path)
            item = QTableWidgetItem("可用" if ok else "未找到")
            item.setForeground(Qt.GlobalColor.darkGreen if ok else Qt.GlobalColor.darkRed)
            self.comp_table.setItem(i, 1, item)
            detail = str(path) if path and path != "内置" else ("内置" if path else "")
            if ver:
                detail = f"{detail}  ·  {ver}".strip(" ·")
            self.comp_table.setItem(i, 2, QTableWidgetItem(detail))

        # 格式矩阵
        self.fmt_table.setRowCount(len(caps))
        for i, (fmt, entry) in enumerate(caps.items()):
            self.fmt_table.setItem(i, 0, QTableWidgetItem(FORMAT_LABELS.get(fmt, fmt)))
            status_item = QTableWidgetItem(format_status_line(fmt, entry))
            status_item.setForeground(Qt.GlobalColor.darkGreen if entry["available"] else Qt.GlobalColor.darkRed)
            tip = entry.get("reason", "")
            if entry.get("engine"):
                tip = f"{entry['engine']}\n{entry.get('engine_version', '')}\n{tip}"
            status_item.setToolTip(tip)
            self.fmt_table.setItem(i, 1, status_item)

            mv_text = []
            for mode, m in entry["multivolume"].items():
                label = MULTIVOLUME_LABELS.get(mode, mode).split("（")[0]
                mv_text.append(("✓ " if m["available"] else "✗ ") + label)
            self.fmt_table.setItem(i, 2, QTableWidgetItem("  ".join(mv_text) or "—"))

            pw_text = []
            for mode, p in entry["password"].items():
                label = ENCRYPTION_LABELS.get(mode, mode).split("（")[0]
                pw_text.append(("✓ " if p["available"] else "✗ ") + label)
            self.fmt_table.setItem(i, 3, QTableWidgetItem("  ".join(pw_text) or "—"))
        self.fmt_table.resizeRowsToContents()

        # 缺失软件
        while self.miss_lay.count():
            item = self.miss_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        missing = missing_requirements(caps)
        if missing:
            self.miss_ok.hide()
            seen = set()
            for fmt, hint, desc in missing:  # noqa: B007
                if hint in seen:
                    continue
                seen.add(hint)
                box = QLabel(f"<b>{hint}</b><br>{desc}")
                box.setWordWrap(True)
                box.setTextFormat(Qt.TextFormat.RichText)
                box.setStyleSheet(
                    "background: rgba(245,158,11,0.12); border-radius: 10px; padding: 10px 12px;"
                )
                box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self.miss_lay.addWidget(box)
        else:
            self.miss_ok.setText("✓ 所有格式所需的组件都已就绪。")
            self.miss_ok.setStyleSheet("color: #16A34A; font-weight: 600;")
            self.miss_ok.show()

        # 原始报告
        self.raw.setPlainText(self._full_report(caps, env))
        self.comp_table.setFixedHeight(max(150, self.comp_table.rowCount() * 30 + 50))
        # 内容过多时允许整页滚动
        total = (
            self.sys_holder.sizeHint().height() + self.distro_note.sizeHint().height()
            + self.comp_table.height() + self.fmt_table.minimumHeight()
            + self.miss_ok.height() + 260 + self.miss_lay.count() * 70
        )
        self._content.setMinimumHeight(total)

    def _distro_note(self, info, env) -> str:  # noqa: ANN001
        if info.is_linux:
            pm = info.package_manager or "未检测到包管理器"
            desktop = info.desktop or "未知"
            note = (
                f"检测到发行版：<b>{info.pretty_name or info.distro_name or 'Linux'}</b>"
                f"（id={info.distro_id or '?'}，同族={' '.join(info.distro_like) or '-'}），"
                f"包管理器：<b>{pm}</b>，桌面环境：{desktop}。"
            )
            need = []
            if not env.get("seven_zip"):
                need.append(package_hint("7zip"))
            return note + ("<br>建议安装：" + "；".join(need) if need else "<br>压缩所需组件已基本齐全。")
        if info.is_macos:
            return (
                f"检测到 macOS {info.release}（{info.machine}）。"
                f"包管理器：<b>{info.package_manager or '未安装 Homebrew'}</b>。"
                + ("" if info.package_manager else " 建议先安装 Homebrew：/bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
            )
        if info.is_windows:
            return (
                f"检测到 {info.branch}（Build {info.extra.get('build', '?')}）。"
                f"Windows 自带 tar.exe（bsdtar）可用于 tar 系格式；7-Zip 已内置在程序中（7za），"
                f"如需 ZIP AES-256 建议安装完整版 7-Zip。"
            )
        return ""

    def _full_report(self, caps, env) -> str:  # noqa: ANN001
        info = get_platform_info()
        lines = [f"BackupStudio 环境报告  {info.label}", f"Python {info.python} · {'frozen' if info.is_frozen else 'source'}"]
        lines.append("")
        lines.append(capability_report_text(caps))
        lines.append("")
        lines.append("组件路径：")
        for k in ("seven_zip", "rar", "unrar", "tar", "gpg"):
            lines.append(f"  {k:<10}: {env.get(k) or '未找到'}")
        return "\n".join(lines)

    def _copy_report(self) -> None:
        QApplication.clipboard().setText(self.raw.toPlainText())
        self.btn_copy.setText("已复制 ✓")
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1500, lambda: self.btn_copy.setText("复制报告"))
