"""可复用的小部件：卡片、徽章、开关、路径选择、下拉增强等。"""

from __future__ import annotations


from . import DARK, LIGHT
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
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

    # ── 玻璃风格（1.1.3）──────────────────────────────────────
    # 不用覆盖层控件（会和孙级按钮抢 z-order），直接在本控件 paintEvent
    # 里画「采样背景 → 模糊 → 色散 → 高光」。
    _glass_on: bool = False
    _glass_radius: int = 14
    _glass_tint: str = "#F7FAFF"
    _glass_alpha: float = 0.62
    _glass_dark: bool = False

    def set_glass(self, on: bool, *, radius: int = 14, tint: str = "#F7FAFF",
                  alpha: float = 0.62, dark: bool = False) -> None:
        self._glass_on = on
        if on:
            self._glass_radius = radius
            self._glass_tint = tint
            self._glass_alpha = alpha
            self._glass_dark = dark
            # 交给自绘，QSS 背景必须让位
            self.setStyleSheet("background: transparent; border: none;")
        else:
            # **必须清掉内联样式**：内联优先级高于全局 QSS，
            # 残留会让卡片永远透明无边框 —— 表现就是"切风格没区别"
            # （这是 1.1.3 的真实 bug）。
            self.setStyleSheet("")
            self._glass_cache = None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._glass_on:
            return super().paintEvent(event)
        from PySide6.QtCore import QPoint, QRect, Qt as _Qt
        from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap

        from .glass import _box_blur, _chromatic, displace

        win = self.window()
        if win is None:
            return super().paintEvent(event)
        # 采样背后（临时隐藏自己避免递归重绘）
        self.setVisible(False)
        try:
            top_left = self.mapTo(win, QPoint(0, 0))
            pm = QPixmap(self.size())
            pm.fill(_Qt.GlobalColor.transparent)
            win.render(pm, QPoint(0, 0), QRect(top_left, self.size()))
        finally:
            self.setVisible(True)

        # **缓存**：模糊+色散是整个玻璃效果里最贵的两步（实测 19 张卡
        # 会让截图脚本跑不动）。按「尺寸 + 窗口内坐标 + 主题」缓存，
        # 尺寸不变时不重算 —— 拖动/缩放时会重算，滚动不受影响。
        key = (self.width(), self.height(), top_left.x(), top_left.y(),
               self._glass_tint, self._glass_dark)
        cached = getattr(self, "_glass_cache", None)
        if cached is not None and cached[0] == key:
            img = cached[1]
        else:
            # **降采样再模糊**（1.1.3 性能修复）：模糊结果本就看不出细节，
            # 先缩到最长边 ~96px 再算，计算量降到约 1/20。
            # 优化前实测：首屏 12.9s / 切页 12.8s。
            w0, h0 = pm.width(), pm.height()
            longest = max(w0, h0)
            small = pm.toImage()
            if longest > 96:
                scale = 96.0 / longest
                small = small.scaled(max(8, int(w0 * scale)), max(8, int(h0 * scale)),
                                     _Qt.AspectRatioMode.IgnoreAspectRatio,
                                     _Qt.TransformationMode.SmoothTransformation)
            for r in (3, 2, 1):
                small = _box_blur(small, r)
            # **折射**：在低分辨率上做位移重采样（便宜且边缘更柔）
            small = displace(small, radius=16, strength=0.6,
                             edge=max(6, min(small.width(), small.height()) // 4))
            small = _chromatic(small, 1)
            # 放回原尺寸（平滑放大；本来就是模糊图，看不出插值）
            img = small.scaled(w0, h0, _Qt.AspectRatioMode.IgnoreAspectRatio,
                               _Qt.TransformationMode.SmoothTransformation)
            self._glass_cache = (key, img)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1,
                            self._glass_radius, self._glass_radius)
        p.setClipPath(path)
        p.setOpacity(0.92)
        p.drawPixmap(0, 0, QPixmap.fromImage(img))
        p.setOpacity(1.0)
        tint = QColor(self._glass_tint)
        tint.setAlphaF(self._glass_alpha)
        p.fillPath(path, tint)
        # 顶部高光
        g = QLinearGradient(0, 0, self.width() * 0.6, self.height())
        g.setColorAt(0.0, QColor(255, 255, 255, 90 if not self._glass_dark else 46))
        g.setColorAt(0.35, QColor(255, 255, 255, 22 if not self._glass_dark else 10))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, g)
        # 内高光边
        p.setPen(QColor(255, 255, 255, int(200 * self._glass_highlight)
                        if hasattr(self, "_glass_highlight") else 180))
        p.setBrush(_Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2,
                          self._glass_radius, self._glass_radius)
        p.end()
        # **必须调用 super()**：QFrame 的默认 paintEvent 负责让子控件
        # 正确合成到父级之上。自绘后不调 super() 会导致子控件（如
        # 「立即备份」按钮）被父级绘制内容盖住 —— 实测只剩一团模糊影子。
        return super().paintEvent(event)

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
        # 1.1.2：42×24 → 46×26（原尺寸低于 WCAG 2.1 AA 的 24×24 底线，
        # 且 24 高时圆角弧度显得局促）
        self.setFixedSize(46, 26)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._dark = dark

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(46, 26)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        # 1.1.2：改用 Palette（原 track_off 浅色 #CBD5E1 不在 Palette 里）
        _pal = DARK if self._dark else LIGHT
        track_on = QColor(_pal.accent)
        # 关闭态轨道加深，保证与卡片底 ≥1.9:1，一眼能看出"关着"
        track_off = QColor(_pal.text_dim if self._dark else "#AEB8CC")
        disabled = not self.isEnabled()
        if disabled:
            track_on = QColor(_pal.border)
            track_off = QColor(_pal.border)

        h = self.height()
        r = h / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track_on if on else track_off)
        p.drawRoundedRect(self.rect(), r, r)

        knob = QColor(_pal.surface if disabled else (_pal.accent_text if self._dark else "#FFFFFF"))
        d = h - 6
        x = self.width() - d - 3 if on else 3
        p.setBrush(knob)
        p.drawEllipse(x, 3, d, d)

        # 焦点环（WCAG：焦点必须可见）
        if self.hasFocus():
            pen = QPen(QColor(_pal.focus_ring))
            pen.setWidth(2)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), r - 1, r - 1)


class ElideLabel(QLabel):
    """单行省略号标签：宽度不足时显示 `…`，完整内容放 tooltip。

    1.1.2 新增。改造前长路径/长任务名是**硬截断**（实测副标题需 372px
    实得 240px，右侧内容直接消失且无提示），用户看不出被截掉了什么。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        self.setToolTip(self._full)
        super().setText(self._full)

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        s = super().minimumSizeHint()
        return QSize(24, s.height())

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        from PySide6.QtGui import QPainter as _QP

        p = _QP(self)
        fm = self.fontMetrics()
        avail = self.contentsRect().width()
        txt = fm.elidedText(self._full, Qt.TextElideMode.ElideMiddle, avail) if avail > 0 else ""
        p.setPen(self.palette().color(self.foregroundRole()))
        p.drawText(self.contentsRect(), int(self.alignment()), txt)


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
