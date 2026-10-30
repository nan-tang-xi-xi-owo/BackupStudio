"""可复用的小部件：卡片、徽章、开关、路径选择、下拉增强等。"""

from __future__ import annotations


from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)



# --------------------------------------------------------------------------
# 基础容器
# --------------------------------------------------------------------------

class Card(QFrame):
    """圆角卡片容器。"""

    def __init__(self, parent: QWidget | None = None, *, flat: bool = False, margins: tuple[int, int, int, int] = (18, 16, 18, 16)) -> None:
        super().__init__(parent)
        self.setObjectName("CardFlat" if flat else "Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(*margins)
        self._layout.setSpacing(10)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:  # noqa: ANN001
        self._layout.addLayout(layout)


def title_label(text: str, level: int = 2) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName({1: "H1", 2: "H2", 3: "H3"}.get(level, "H3"))
    return lbl


def muted(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Muted")
    lbl.setWordWrap(True)
    return lbl


class Badge(QLabel):
    """小徽章：可用/不可用、状态等。"""

    COLORS = {
        "ok": ("#16A34A", "#DCFCE7"),
        "warn": ("#B45309", "#FEF3C7"),
        "bad": ("#B91C1C", "#FEE2E2"),
        "info": ("#1D4ED8", "#DBEAFE"),
        "muted": ("#4B5563", "#E5E7EB"),
    }
    DARK_COLORS = {
        "ok": ("#6EE7B7", "#14372B"),
        "warn": ("#FCD34D", "#3A2E10"),
        "bad": ("#FCA5A5", "#3B1717"),
        "info": ("#93C5FD", "#152B4D"),
        "muted": ("#CBD5E1", "#2A2F3D"),
    }

    def __init__(self, text: str = "", kind: str = "muted", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._kind = kind
        self._dark = False
        self.setObjectName("Badge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply()

    def _apply(self) -> None:
        fg, bg = (self.DARK_COLORS if self._dark else self.COLORS).get(self._kind, self.COLORS["muted"])
        self.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 9px; padding: 2px 10px; font-size: 12px; font-weight: 600;"
        )

    def set_state(self, text: str, kind: str) -> None:
        self._kind = kind
        self.setText(text)
        self._apply()

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self._apply()


class StatusPill(QWidget):
    """带圆点的状态条，例如「● 上次成功 2 分钟前」。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.dot = QLabel("●")
        self.text = QLabel("")
        self.text.setObjectName("Muted")
        lay.addWidget(self.dot)
        lay.addWidget(self.text, 1)
        self.set_state("muted", "")

    def set_state(self, kind: str, text: str) -> None:
        color = {"ok": "#22C55E", "warn": "#F59E0B", "bad": "#EF4444", "run": "#3B82F6"}.get(kind, "#9CA3AF")
        self.dot.setStyleSheet(f"color: {color}; font-size: 14px;")
        self.text.setText(text)


class Kbd(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Badge")


class ElidedLabel(QLabel):
    """按可用宽度省略的标签（单行，超出部分显示 ``…``）。

    为什么不用 ``setWordWrap(True)``：这里显示的是「计划 · 下次 · 目标目录」这类
    信息密度高、但不该换行的摘要。换行会让列表项高度参差，截断又会出现
    ``p-bs-fix-src`` 这种半截词。

    也没有用 ``QLabel.setElideMode``——Qt 的 QLabel 没有这个 API（那是
    ``QAbstractItemView`` 与 ``QFontMetrics.elidedText`` 的东西）。

    完整文本放进 tooltip，用户悬停即可看到。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setText(text)  # 走统一的 setText，保证 tooltip / 省略都初始化到

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 命名
        self._full = text
        self.setToolTip(text)
        self._relayout()

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self):  # noqa: ANN201, N802 - Qt 命名
        """返回"可以压到很窄"的最小尺寸。

        QLabel 默认的最小宽度是**整段文本**的宽度，一个长路径就能把整页顶宽
        （实测设置页因此出现 156px 横向溢出）。省略号本来就是为了在有限宽度下
        表达文本，所以这里允许压到 0 —— 布局给多宽就显示多宽。
        """
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        width = max(0, self.width() - 2)
        if width <= 0:
            return
        metrics = self.fontMetrics()
        # Qt 自带省略号算法：按像素宽度裁剪并补 "…"，不会切断多字节字符
        super().setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, width))


# --------------------------------------------------------------------------
# 输入类
# --------------------------------------------------------------------------

class PathEdit(QWidget):
    """带「浏览」按钮的路径输入。"""

    changed = Signal(str)

    def __init__(
        self,
        mode: str = "dir",
        placeholder: str = "",
        parent: QWidget | None = None,
        caption: str = "选择目录",
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.caption = caption
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.button = QPushButton("浏览…")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.button)
        self.button.clicked.connect(self._browse)
        self.edit.textChanged.connect(self.changed.emit)

    def _browse(self) -> None:
        start = self.edit.text().strip() or str(__import__("pathlib").Path.home())
        if self.mode == "dir":
            path = QFileDialog.getExistingDirectory(self, self.caption, start)
        elif self.mode == "exe":
            path, _ = QFileDialog.getOpenFileName(self, self.caption, start, "可执行文件 (*.exe *.bin);;所有文件 (*)")
        else:
            path, _ = QFileDialog.getOpenFileName(self, self.caption, start, "所有文件 (*)")
        if path:
            self.edit.setText(path)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, text: str) -> None:
        self.edit.setText(text or "")


class Switch(QAbstractButton):
    """iOS 风格的开关。"""

    def __init__(self, parent: QWidget | None = None, *, dark: bool = False) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._dark = dark

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        track_on = QColor("#4F6BED") if not self._dark else QColor("#7C8CF8")
        track_off = QColor("#CBD5E1") if not self._dark else QColor("#3A4056")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track_on if on else track_off)
        p.drawRoundedRect(self.rect(), 12, 12)
        knob = QColor("#FFFFFF") if not self._dark else QColor("#E6E8F0")
        p.setBrush(knob)
        x = self.width() - 21 if on else 3
        p.drawEllipse(x, 3, 18, 18)


class Combo(QComboBox):
    """下拉框：支持用 userData 取值，并强制统一字体。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 弹层是**独立的顶层窗口**（QComboBoxPrivateContainer），不是 combo 的子控件，
        # 因此 QSS 里的 `QComboBox QAbstractItemView` 选择器匹配不到它 —— 用户看到的是
        # 白底黑字的系统样式弹层。改用真正的 QListView 后 QSS 才能生效。
        #
        # 注意：setView() 会**重置 view 的尺寸策略**（实测行高 21→13、最小宽度 280→0），
        # 所以必须在其后重新声明这两项，否则长条目被压扁、弹层窄得看不清。
        self.setView(QListView())
        self.view().setMinimumWidth(280)
        self.view().setSpacing(2)
        # setView 会把行高重置成字体原始高度（实测 21→12px），条目挤在一起很难点。
        # 显式给一个与正文行高匹配的下限（QSS 的 min-height 是给 item 的，
        # 这里补的是 view 自身的 sizeHintForRow 基线）。
        self.view().setStyleSheet("QListView::item { min-height: 26px; }")

    def add_item(self, text: str, data=None) -> None:  # noqa: ANN001
        self.addItem(text, data)

    def current_data(self):  # noqa: ANN201
        return self.currentData()

    def select_data(self, data) -> None:  # noqa: ANN001
        idx = self.findData(data)
        if idx >= 0:
            self.setCurrentIndex(idx)


class CollapsibleSection(QWidget):
    """可折叠的高级选项区域。"""

    def __init__(self, title: str, parent: QWidget | None = None, expanded: bool = False) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.toggle = QPushButton(("▾ " if expanded else "▸ ") + title)
        self.toggle.setObjectName("Link")
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setStyleSheet("text-align: left; font-weight: 600;")
        self.content = QWidget()
        self.content.setVisible(expanded)
        lay.addWidget(self.toggle)
        lay.addWidget(self.content)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 4, 0, 0)
        self.content_layout.setSpacing(10)
        self.toggle.toggled.connect(self._on_toggle)
        self._title = title

    def _on_toggle(self, checked: bool) -> None:
        self.content.setVisible(checked)
        self.toggle.setText(("▾ " if checked else "▸ ") + self._title)

    def add(self, widget: QWidget) -> QWidget:
        self.content_layout.addWidget(widget)
        return widget

    def add_layout(self, layout) -> None:  # noqa: ANN001
        self.content_layout.addLayout(layout)


def icon_from_style(style: QStyle, kind: str) -> QIcon:
    mapping = {
        "settings": QStyle.StandardPixmap.SP_FileDialogDetailedView,
        "refresh": QStyle.StandardPixmap.SP_BrowserReload,
        "folder": QStyle.StandardPixmap.SP_DirIcon,
        "file": QStyle.StandardPixmap.SP_FileIcon,
        "warn": QStyle.StandardPixmap.SP_MessageBoxWarning,
        "info": QStyle.StandardPixmap.SP_MessageBoxInformation,
    }
    return style.standardIcon(mapping.get(kind, QStyle.StandardPixmap.SP_FileIcon))


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setObjectName("CardFlat")
    line.setFixedHeight(1)
    return line


def row(*widgets: QWidget, spacing: int = 8, stretch_last: bool = False) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for i, widget in enumerate(widgets):
        lay.addWidget(widget, 1 if (stretch_last and i == len(widgets) - 1) else 0)
    return w
