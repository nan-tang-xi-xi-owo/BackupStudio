"""自绘标题栏（1.1.4）—— 让标题栏跟随应用主题。

## 为什么必须自绘（实测依据）

在本机 Windows 10 10.0.19044 上逐条实测 `DwmSetWindowAttribute` 返回码：

    DWMWA_USE_IMMERSIVE_DARK_MODE (20)   S_OK          ✅ 可用
    DWMWA_WINDOW_CORNER_PREFERENCE (33)  E_INVALIDARG  ❌
    DWMWA_BORDER_COLOR (34)              E_INVALIDARG  ❌
    DWMWA_CAPTION_COLOR (35)             E_INVALIDARG  ❌
    DWMWA_TEXT_COLOR (36)                E_INVALIDARG  ❌

`DwmGetWindowAttribute(35)` 读回也是 `E_INVALIDARG`。
即 **Win10 只能用 DWM 切深浅色，完全没法给标题栏自定义颜色**。
Material You 那种任意源色驱动标题栏，在 Win10 上只有自绘一条路。

## 设计

* `FramelessWindowHint` + 自绘 32px 标题栏（含图标/标题/最小化/最大化/关闭）
* 拖动：`windowHandle().startSystemMove()`（实测返回 True），
  自己算拖动会把 Aero Snap 弄丢，系统 API 能保住贴边吸附
* 缩放：8 个边缘热区 + `startSystemResize(edges)`
* 双击标题栏 → 最大化/还原（自己实现，因为无边框后没有系统非客户区）
* 只在 Windows 启用；macOS/Linux 保持系统标题栏（它们本来就跟主题，
  且 mac 的无边框会丢失红绿灯按钮与全屏动画）
* 颜色全部来自 `Palette`，随 `theme_changed` 自动更新
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPalette
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

#: 标题栏高度（逻辑像素，随 DPR 自动缩放）
TITLE_H = 34
#: 边缘缩放热区宽度
RESIZE_MARGIN = 5


class TitleButton(QPushButton):
    """标题栏按钮（最小化/最大化/关闭）。

    样式用**内联 QSS**：它必须比全局 QSS 更具体（否则被 QPushButton 规则盖掉），
    而且颜色要随主题变，所以由 `set_colors()` 在主题切换时重设。
    """

    def __init__(self, glyph: str, kind: str = "normal", parent: QWidget | None = None) -> None:
        super().__init__(glyph, parent)
        self._kind = kind
        self.setFixedSize(46, TITLE_H)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.set_colors("#1A1F2B", "#525869", "#E81123")

    def set_colors(self, fg: str, hover_bg: str, close_bg: str) -> None:
        hover = close_bg if self._kind == "close" else hover_bg
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" color: {fg}; font-size: 11pt; padding: 0; }}"
            f"QPushButton:hover {{ background: {hover};"
            f" color: {'#FFFFFF' if self._kind == 'close' else fg}; }}"
            f"QPushButton:pressed {{ background: {hover}; }}"
        )


class TitleBar(QWidget):
    """自绘标题栏：图标 + 标题 + 三键。拖动由主窗口处理。"""

    def __init__(self, window: QWidget, title: str, icon=None) -> None:  # noqa: ANN001
        super().__init__(window)
        self._win = window
        self.setFixedHeight(TITLE_H)
        self.setObjectName("TitleBar")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 0, 0)
        lay.setSpacing(8)

        self.icon = QLabel()
        self.icon.setFixedSize(16, 16)
        self.icon.setScaledContents(True)
        if icon is not None:
            self.icon.setPixmap(icon.pixmap(16, 16))
        lay.addWidget(self.icon)

        self.title = QLabel(title)
        f = QFont()
        f.setPointSizeF(9.5)
        self.title.setFont(f)
        lay.addWidget(self.title, 1)

        lay.addStretch(0)
        self.btn_min = TitleButton("─")
        self.btn_max = TitleButton("□")
        self.btn_close = TitleButton("✕", "close")
        lay.addWidget(self.btn_min)
        lay.addWidget(self.btn_max)
        lay.addWidget(self.btn_close)

        self.btn_min.clicked.connect(window.showMinimized)
        self.btn_max.clicked.connect(self.toggle_max)
        self.btn_close.clicked.connect(window.close)

        self._bg = "#FFFFFF"
        self._fg = "#1A1F2B"
        self._sep = "#C0CADD"
        self._hover = "#E8EDF6"
        self._close = "#E81123"
        self.set_colors("#FFFFFF", "#1A1F2B", "#C0CADD", "#E8EDF6", "#E81123")

    # ── 外观 ──
    def set_colors(self, bg: str, fg: str, sep: str, hover: str, close: str) -> None:
        self._bg, self._fg, self._sep = bg, fg, sep
        self._hover, self._close = hover, close
        self.btn_min.set_colors(fg, hover, close)
        self.btn_max.set_colors(fg, hover, close)
        self.btn_close.set_colors(fg, hover, close)
        self.title.setStyleSheet(f"color: {fg}; background: transparent;")
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self._bg))
        # 底部 1px 分隔线，让标题栏与内容区分开
        p.setPen(QColor(self._sep))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()

    # ── 行为 ──
    def toggle_max(self) -> None:
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()
        self.sync_max_glyph()

    def sync_max_glyph(self) -> None:
        self.btn_max.setText("❐" if self._win.isMaximized() else "□")

    # 双击标题栏 → 最大化/还原
    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:  # noqa: ANN001, N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.toggle_max()
        super().mouseDoubleClickEvent(e)

def prefer_frameless() -> bool:
    """是否用无边框自绘标题栏。

    **只在 Windows 启用**：
    * Win10 实测 DWMWA_CAPTION_COLOR 返回 E_INVALIDARG（无法配色），
      自绘是唯一办法；
    * macOS 保留系统标题栏 —— 无边框会丢红绿灯按钮、全屏动画与
      traffic-light 悬停行为，代价大于收益；
    * Linux 各桌面环境标题栏规则不一，保持系统默认更稳。

    可用环境变量 BACKUPSTUDIO_FRAMELESS=0/1 强制开关（便于排查）。
    """
    import os
    import sys

    forced = os.environ.get("BACKUPSTUDIO_FRAMELESS", "").strip()
    if forced in ("0", "false", "False"):
        return False
    if forced in ("1", "true", "True"):
        return True
    return sys.platform == "win32"
