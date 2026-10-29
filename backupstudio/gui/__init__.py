"""GUI 资源：图标（运行时渲染 SVG，无需外部文件）、主题 QSS 与配色。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# 随包的运行时图标（PNG），冻结程序读取它而不是现场渲染 SVG
ASSETS = Path(__file__).resolve().parent.parent / "assets" / "tray"
RUNTIME_SIZES = (16, 24, 32, 48, 64, 128, 256)

APP_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#6366F1"/>
      <stop offset="1" stop-color="#22D3EE"/>
    </linearGradient>
  </defs>
  <rect x="4" y="4" width="56" height="56" rx="16" fill="url(#g)"/>
  <g fill="#ffffff" fill-opacity="0.95">
    <rect x="15" y="16" width="34" height="9" rx="3"/>
    <rect x="15" y="28" width="34" height="9" rx="3" fill-opacity="0.78"/>
    <rect x="15" y="40" width="34" height="9" rx="3" fill-opacity="0.6"/>
  </g>
  <path d="M40 44 l4 5 7-9" stroke="#ffffff" stroke-width="4" fill="none"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

_TRAY_DOT = {
    "idle": "#22D3EE",
    "running": "#FBBF24",
    "success": "#34D399",
    "failure": "#F87171",
}


def svg_icon(svg: str, sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)) -> QIcon:
    """把 SVG 渲染成包含多尺寸 pixmap 的 QIcon。"""
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    icon = QIcon()
    for size in sizes:
        pm = QPixmap(QSize(size, size))
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        icon.addPixmap(pm)
    return icon


def app_icon() -> QIcon:
    """应用图标：优先读取随包 PNG（冻结程序不依赖 QtSvg/外部文件）。"""
    icon = QIcon()
    found = False
    for size in RUNTIME_SIZES:
        png = ASSETS / f"app-{size}.png"
        if png.is_file():
            pm = QPixmap(str(png))
            if not pm.isNull():
                icon.addPixmap(pm)
                found = True
    if found:
        return icon
    import logging

    logging.getLogger("backupstudio.gui").warning("未找到随包图标资源（%s），回退到内置 SVG 渲染", ASSETS)
    return svg_icon(APP_ICON_SVG)


def tray_state_icon(state: str) -> QIcon:
    """托盘图标：应用图标 + 右下角状态色圆点（空闲/运行中/成功/失败）。"""
    color = _TRAY_DOT.get(state, _TRAY_DOT["idle"])
    base = app_icon()
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128):
        src_size = min([s for s in RUNTIME_SIZES if s >= size] or [RUNTIME_SIZES[-1]])
        pm = base.pixmap(QSize(src_size, src_size))
        if pm.isNull():
            pm = QPixmap(QSize(size, size))
            pm.fill(Qt.GlobalColor.transparent)
        else:
            pm = pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = size * 0.34
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(size - r - 0.5, size - r - 0.5, r, r))
        painter.end()
        icon.addPixmap(pm)
    return icon


def format_icon() -> QIcon:  # 兼容旧调用
    return app_icon()


# --------------------------------------------------------------------------
# 主题
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Palette:
    name: str
    dark: bool
    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_dim: str
    accent: str
    accent_hover: str
    accent_text: str
    success: str
    warning: str
    danger: str
    selection: str


LIGHT = Palette(
    name="light", dark=False,
    bg="#F4F6FB", surface="#FFFFFF", surface_alt="#EEF2F9", border="#D8DEEA",
    text="#1F2430", text_dim="#5C6478", accent="#4F6BED", accent_hover="#3F58D6",
    accent_text="#FFFFFF", success="#15803D", warning="#B45309", danger="#B91C1C",
    selection="#DCE4FF",
)

DARK = Palette(
    name="dark", dark=True,
    bg="#161821", surface="#1E2130", surface_alt="#262A3B", border="#33384D",
    text="#E6E8F0", text_dim="#9AA1B5", accent="#7C8CF8", accent_hover="#93A0FF",
    accent_text="#11131A", success="#34D399", warning="#FBBF24", danger="#F87171",
    selection="#33406B",
)


def palette_for(name: str) -> Palette:
    return DARK if name == "dark" else LIGHT


def stylesheet(p: Palette) -> str:
    fg = p.text
    return f"""
    QWidget {{
        color: {fg};
        font-family: "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", "Helvetica Neue", sans-serif;
        font-size: 13px;
    }}
    QMainWindow, QDialog, QWidget#Page {{ background: {p.bg}; }}
    QFrame#Card {{
        background: {p.surface};
        border: 1px solid {p.border};
        border-radius: 12px;
    }}
    QFrame#CardFlat {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: 12px;
    }}
    QLabel#H1 {{ font-size: 22px; font-weight: 600; }}
    QLabel#H2 {{ font-size: 16px; font-weight: 600; }}
    QLabel#H3 {{ font-size: 14px; font-weight: 600; }}
    QLabel#Muted {{ color: {p.text_dim}; }}
    QLabel#Badge {{
        border-radius: 8px; padding: 2px 10px; font-size: 12px; font-weight: 600;
        background: {p.surface_alt}; color: {p.text_dim};
    }}
    QLabel#ErrorText {{ color: {p.danger}; }}
    QLabel#SuccessText {{ color: {p.success}; }}
    QLabel#WarningText {{ color: {p.warning}; }}
    QPushButton {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 7px 16px;
        color: {fg};
    }}
    QPushButton:hover {{ background: {p.selection}; }}
    QPushButton:pressed {{ background: {p.border}; }}
    QPushButton:disabled {{ color: {p.text_dim}; background: {p.surface_alt}; }}
    QPushButton#Primary {{
        background: {p.accent}; border: 1px solid {p.accent}; color: {p.accent_text}; font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
    QPushButton#Danger {{ color: {p.danger}; }}
    QPushButton#Link {{ background: transparent; border: none; color: {p.accent}; padding: 2px 4px; }}
    QPushButton#Link:hover {{ text-decoration: underline; }}
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit, QTimeEdit {{
        background: {p.surface};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 6px 9px;
        selection-background-color: {p.accent};
        selection-color: {p.accent_text};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QTimeEdit:focus {{
        border: 1px solid {p.accent};
    }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {p.text_dim}; background: {p.surface_alt}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox {{
        combobox-popup: 0;   /* 用真 QListView 弹层，QSS 才会生效（见 widgets.Combo） */
    }}
    QComboBox QAbstractItemView {{
        background: {p.surface}; border: 1px solid {p.border}; selection-background-color: {p.selection};
        selection-color: {fg}; outline: none;
        padding: 4px;
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 26px; padding: 2px 8px; border-radius: 8px;
    }}
    QComboBox QAbstractItemView::item:hover {{ background: {p.surface_alt}; }}
    /* 弹层最外层容器是独立顶层 QFrame，不补这条就会在四周留白边 */
    QComboBox QFrame {{ background: {p.surface}; border: 1px solid {p.border}; }}
    QComboBox::down-arrow {{
        image: none; width: 0; height: 0;
        border-left: 4px solid transparent; border-right: 4px solid transparent;
        border-top: 5px solid {p.text_dim};
        margin-right: 8px;
    }}
    QListWidget, QTreeWidget, QTableWidget {{
        background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; outline: none;
        alternate-background-color: {p.surface_alt};
    }}
    /* 表格的三处「样式盲区」——实测不补会露黑/白块：
       * 左上角交角按钮：默认由风格自绘，windowsvista 下是纯黑方块；
       * 表头整体背景（::section 只覆盖有表头段的地方，表头空白区不归它管）；
       * 表格右侧/底部空白（列宽不足控件宽度时露出的 viewport 区域）。 */
    QTableCornerButton::section {{
        background: {p.surface_alt};
        border: none;
        border-bottom: 1px solid {p.border};
    }}
    QHeaderView {{ background: {p.surface_alt}; border: none; }}
    QTableView QTableCornerButton::section {{ background: {p.surface_alt}; border: none; }}
    QTableWidget QWidget, QTableView QWidget {{ background: {p.surface}; }}
    QTextEdit, QPlainTextEdit {{
        background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 8px;
    }}
    QSplitter::handle:horizontal {{ width: 10px; }}
    QListWidget::item {{ border-radius: 8px; padding: 10px 12px; margin: 3px 6px; }}
    QListWidget::item:selected {{ background: {p.selection}; color: {fg}; }}
    QListWidget::item:hover {{ background: {p.surface_alt}; }}
    QTreeWidget::item, QTableWidget::item {{ padding: 6px; }}
    QHeaderView::section {{
        background: {p.surface_alt}; border: none; border-bottom: 1px solid {p.border};
        padding: 7px 8px; font-weight: 600;
    }}
    QTabWidget::pane {{ border: 1px solid {p.border}; border-radius: 12px; background: {p.surface}; top: -1px; }}
    QTabBar::tab {{
        background: transparent; padding: 8px 16px; margin-right: 4px;
        border-radius: 8px; color: {p.text_dim};
    }}
    QTabBar::tab:selected {{ background: {p.surface}; color: {p.accent}; font-weight: 600; }}
    QTabBar::tab:hover {{ color: {fg}; }}
    QGroupBox {{
        border: 1px solid {p.border}; border-radius: 12px; margin-top: 14px; padding: 14px 12px 12px 12px;
        background: transparent;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {p.text_dim}; font-weight: 600;
    }}
    QCheckBox, QRadioButton {{ spacing: 8px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 17px; height: 17px; }}
    QCheckBox::indicator:unchecked {{ border: 1px solid {p.border}; border-radius: 5px; background: {p.surface}; }}
    QCheckBox::indicator:checked {{ border: 1px solid {p.accent}; border-radius: 5px; background: {p.accent}; }}
    QRadioButton::indicator:unchecked {{ border: 1px solid {p.border}; border-radius: 8px; background: {p.surface}; }}
    QRadioButton::indicator:checked {{ border: 5px solid {p.accent}; border-radius: 8px; background: {p.surface}; }}
    QSlider::groove:horizontal {{ height: 6px; background: {p.surface_alt}; border-radius: 3px; }}
    QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 3px; }}
    QSlider::handle:horizontal {{
        background: {p.surface}; border: 2px solid {p.accent}; width: 14px; height: 14px;
        margin: -5px 0; border-radius: 8px;
    }}
    QProgressBar {{
        background: {p.surface_alt}; border: none; border-radius: 8px; height: 12px; text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: 8px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {p.text_dim}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {p.border}; border-radius: 4px; min-width: 30px; }}
    QToolTip {{
        background: {p.surface}; color: {fg}; border: 1px solid {p.border};
        border-radius: 8px; padding: 6px 8px;
    }}
    QMenu {{
        background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 6px;
    }}
    QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 8px; }}
    QMenu::item:selected {{ background: {p.selection}; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 8px; }}
    QStatusBar {{ background: transparent; color: {p.text_dim}; }}
    QSplitter::handle {{ background: transparent; }}
    QToolButton {{ border-radius: 8px; padding: 6px; background: transparent; }}
    QToolButton:hover {{ background: {p.surface_alt}; }}
    QToolButton#NavButton {{
        background: transparent; border: none; border-radius: 8px; padding: 10px 14px;
        text-align: left; color: {p.text_dim}; font-size: 13px;
    }}
    QToolButton#NavButton:hover {{ background: {p.surface_alt}; color: {fg}; }}
    QToolButton#NavButton:checked {{ background: {p.selection}; color: {p.accent}; font-weight: 600; }}
    """
