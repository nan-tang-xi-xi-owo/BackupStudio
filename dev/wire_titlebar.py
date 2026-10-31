"""把自绘标题栏接入 MainWindow（1.1.4）。

要点：
1. `FramelessWindowHint`（仅 Windows）
2. 用 QVBoxLayout 包住标题栏 + 原 central widget
3. 拖动走 `startSystemMove()`（保住 Aero Snap）
4. 8 向边缘缩放走 `startSystemResize()`
5. 主题变化时同步标题栏配色
"""
from pathlib import Path

M = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\main_window.py")
s = M.read_text(encoding="utf-8")

# ── 1. 导入 ──
if "from .titlebar import" not in s:
    s = s.replace(
        "from .styles import", "from .titlebar import TITLE_H, TitleBar, prefer_frameless\nfrom .styles import", 1
    )
    print("1) 已加 titlebar 导入")
else:
    print("1) 导入已存在")

# ── 2. __init__ 里创建标题栏（在 setCentralWidget 之后）──
old = """        self.setCentralWidget(central)"""
new = """        # ── 自绘标题栏（1.1.4）────────────────────────────────
        # Win10 上 DWM 的 CAPTION_COLOR 返回 E_INVALIDARG（实测），
        # 只能靠无边框自绘让标题栏跟随主题。mac/Linux 保持系统标题栏。
        self._frameless = prefer_frameless()
        if self._frameless:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            outer = QWidget()
            ol = QVBoxLayout(outer)
            ol.setContentsMargins(0, 0, 0, 0)
            ol.setSpacing(0)
            self.titlebar = TitleBar(self, f"{APP_NAME_ZH} · {APP_NAME} {__version__}",
                                     app_icon())
            ol.addWidget(self.titlebar)
            ol.addWidget(central, 1)
            self.setCentralWidget(outer)
            self.setMouseTracking(True)
            outer.setMouseTracking(True)
        else:
            self.titlebar = None
            self.setCentralWidget(central)"""
if old in s and "self.titlebar" not in s:
    s = s.replace(old, new, 1)
    print("2) 已创建标题栏")
else:
    print("2) 跳过（已存在或未匹配）")

# ── 3. 拖动 + 边缘缩放 + 双击（追加方法到 _apply_theme 之前）──
anchor = "    def _apply_theme(self, palette) -> None:  # noqa: ANN001"
methods = '''    # ------------------------------------------------------------------
    # 无边框窗口：拖动 / 边缘缩放（1.1.4）
    # ------------------------------------------------------------------
    def _edge_at(self, pos: QPoint):  # noqa: ANN201
        """判断鼠标是否落在缩放热区，返回 Qt.Edge 组合。"""
        from PySide6.QtCore import Qt as _Qt

        w, h, m = self.width(), self.height(), RESIZE_MARGIN
        x, y = pos.x(), pos.y()
        edges = _Qt.Edge(0)
        if x <= m:
            edges |= _Qt.Edge.LeftEdge
        elif x >= w - m:
            edges |= _Qt.Edge.RightEdge
        if y <= m:
            edges |= _Qt.Edge.TopEdge
        elif y >= h - m:
            edges |= _Qt.Edge.BottomEdge
        return edges

    def mousePressEvent(self, e) -> None:  # noqa: ANN001, N802
        """标题栏区域拖动窗口；边缘热区启动系统缩放。"""
        from PySide6.QtCore import Qt as _Qt

        if self._frameless and e.button() == _Qt.MouseButton.LeftButton:
            edges = self._edge_at(e.position().toPoint())
            if edges:
                self.windowHandle().startSystemResize(edges)
                return
            # 标题栏高度内的按下 = 拖动窗口（用系统 API 保住 Aero Snap）
            if e.position().y() <= TITLE_H and self.titlebar is not None:
                if self.windowHandle().startSystemMove():
                    return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:  # noqa: ANN001, N802
        """根据位置切换缩放光标。"""
        from PySide6.QtCore import Qt as _Qt

        if self._frameless:
            edges = self._edge_at(e.position().toPoint())
            cur = _Qt.CursorShape.ArrowCursor
            if edges in (_Qt.Edge.LeftEdge, _Qt.Edge.RightEdge):
                cur = _Qt.CursorShape.SizeHorCursor
            elif edges in (_Qt.Edge.TopEdge, _Qt.Edge.BottomEdge):
                cur = _Qt.CursorShape.SizeVerCursor
            elif edges:
                cur = (_Qt.CursorShape.SizeFDiagCursor
                       if edges & _Qt.Edge.LeftEdge else _Qt.CursorShape.SizeBDiagCursor)
            self.setCursor(cur)
        super().mouseMoveEvent(e)

    def changeEvent(self, e) -> None:  # noqa: ANN001, N802
        """最大化/还原时更新按钮字形。"""
        from PySide6.QtCore import QEvent

        if e.type() == QEvent.Type.WindowStateChange and self.titlebar is not None:
            self.titlebar.sync_max_glyph()
        super().changeEvent(e)

    def _apply_titlebar_theme(self, palette) -> None:  # noqa: ANN001
        """标题栏配色跟随主题（1.1.4）。"""
        if self.titlebar is None:
            return
        self.titlebar.set_colors(
            palette.surface, palette.text, palette.border,
            palette.surface_alt, palette.danger,
        )

'''
if "def _apply_titlebar_theme" not in s:
    s = s.replace(anchor, methods + anchor, 1)
    print("3) 已加拖动/缩放/主题方法")

# ── 4. _apply_theme 末尾同步标题栏 ──
old4 = """        # 1.1.3：玻璃风格下给卡片装折射层"""
new4 = """        # 1.1.4：标题栏跟随主题
        try:
            self._apply_titlebar_theme(palette)
        except Exception:  # noqa: BLE001
            log.debug("标题栏配色失败", exc_info=True)
        # 1.1.3：玻璃风格下给卡片装折射层"""
if old4 in s and "_apply_titlebar_theme(palette)" not in s.split("def _apply_theme")[1][:2000]:
    s = s.replace(old4, new4, 1)
    print("4) _apply_theme 已同步标题栏")

M.write_text(s, encoding="utf-8", newline="")
print("done")
