"""可复用的小部件：卡片、徽章、开关、路径选择、下拉增强等。"""

from __future__ import annotations


from . import DARK, LIGHT
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
        # 1.1.2：改为从 Palette 派生（原值与 Palette 不一致：
        # Badge 用 #16A34A、StatusPill 用 #22C55E、Palette 用 #127F3A，
        # 同一语义三个不同绿）
        "ok": (LIGHT.success, "#DCFCE7"),
        "warn": (LIGHT.warning, "#FEF3C7"),
        "bad": (LIGHT.danger, "#FEE2E2"),
        "info": (LIGHT.accent, "#DBEAFE"),
        "muted": (LIGHT.text_dim, "#E5E7EB"),
    }
    DARK_COLORS = {
        "ok": (DARK.success, "#14372B"),
        "warn": (DARK.warning, "#3A2E10"),
        "bad": (DARK.danger, "#3B1717"),
        "info": (DARK.accent, "#152B4D"),
        "muted": (DARK.text_dim, "#2A2F3D"),
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
        # 1.1.2：改用 Palette 语义色（原 #22C55E 与 Badge 的 #16A34A、
        # Palette 的 #127F3A 是三个不同的绿）
        _pal = DARK if getattr(self, "_dark", False) else LIGHT
        color = {
            "ok": _pal.success,
            "warn": _pal.warning,
            "bad": _pal.danger,
            "run": _pal.accent,
        }.get(kind, _pal.text_dim)
        self.dot.setStyleSheet(f"color: {color}; font-size: 14px;")
        self.text.setText(text)


class Kbd(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Badge")


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
        # 1.1.2：改用 Palette（原 track_off 浅色 #CBD5E1 不在 Palette 里）
        _pal = DARK if self._dark else LIGHT
        track_on = QColor(_pal.accent)
        track_off = QColor(_pal.border_strong)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track_on if on else track_off)
        p.drawRoundedRect(self.rect(), 12, 12)
        knob = QColor(_pal.accent_text if self._dark else _pal.surface)
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
        self.view().setMinimumWidth(280)

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
