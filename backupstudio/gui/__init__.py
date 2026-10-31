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
    """一套主题的全部颜色。

    ## 为什么有这么多字段（1.1.2 UI 改造）

    改造前只有 15 个字段，导致三个具体问题：

    1. **一个色当两种用**：`selection` 既做「列表选中行背景」又做
       「导航按钮选中态背景」，前者需正文对比 12:1、后者需 4.5:1，
       取折中值就两头不讨好（实测 NavButton 选中态仅 3.34~3.56:1）。
    2. **语义色只有一种形态**：`success` 既做文字又做图标，
       文字需 4.5:1、图标只需 3:1，只给一个值就只能按最低要求取。
    3. **`disabled` 与正常态同值**：`QPushButton:disabled` 的
       `background` 直接用了 `surface_alt`，与正常按钮**完全相同** ——
       规则生效了但看不出区别（实测 `#EEF2F9 -> #EEF2F9`）。

    所以这里把「文字色」与「填充色」、「常态」与「禁用」分开。
    """

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
    # ── 1.1.2 新增 ──
    #: 强边框：输入框、需要强调的卡片。比 `border` 深，保证 ≥1.5:1
    border_strong: str = "#C2CCE0"
    #: 禁用态背景。**必须**与常态背景可区分（旧实现直接复用 surface_alt 是 bug）
    disabled_bg: str = "#F5F7FB"
    #: 禁用态文字
    disabled_fg: str = "#A0A7B8"
    #: 焦点环颜色。需 ≥3:1 对背景（WCAG 图形元素标准）
    focus_ring: str = "#3F58D6"
    #: 导航/页签的选中背景。独立于 `selection`，因两者对比度要求不同
    nav_active: str = "#E4EBFF"


LIGHT = Palette(
    name="light", dark=False,
    # **1.1.2 观感调整**：实拍截图反馈"整屏发灰"。实测数值显示对比度都达标
    # （正文 13.7:1、副标题 4.72:1），问题在**明度层次太挤**：
    # 页面底/卡片底只有 1.13:1、边框 1.37:1，三层几乎糊在一起。
    # 这里把页面底再压深一档、边框加深，拉开"页 < 卡 < 边框"的层次。
    bg="#E8EDF6", surface="#FFFFFF", surface_alt="#F2F5FB", border="#C0CADD",
    text="#1A1F2B", text_dim="#525869", accent="#3E5DEB", accent_hover="#2F49C9",
    accent_text="#FFFFFF", success="#127F3A", warning="#9F5706", danger="#D12222",
    selection="#D6DEFF",
    border_strong="#A9B6CE",
    disabled_bg="#F5F7FB",
    disabled_fg="#A0A7B8",
    focus_ring="#2F49C9",
    nav_active="#DFE7FF",
)

DARK = Palette(
    name="dark", dark=True,
    bg="#161821", surface="#212536", surface_alt="#262A3B", border="#3A4056",
    text="#E6E8F0", text_dim="#9AA1B5", accent="#7C8CF8", accent_hover="#93A0FF",
    accent_text="#11131A", success="#34D399", warning="#FBBF24", danger="#F87171",
    selection="#33406B",
    border_strong="#4A5170",
    disabled_bg="#1B1E2B",
    disabled_fg="#5A6178",
    focus_ring="#93A0FF",
    nav_active="#2E3A66",
)


def palette_for(name: str) -> Palette:
    return DARK if name == "dark" else LIGHT


def install_translator(app) -> bool:  # noqa: ANN001
    """给 Qt 装中文翻译，让 QMessageBox 标准按钮显示「确定/取消/是/否」。

    1.1.2 新增。改造前实测按钮文案是英文 `OK`/`Save`/`Cancel`/`&Yes`/`&No`
    （全项目 37 个模态框都受影响）。

    翻译文件来自 Qt 自带 `translations/qtbase_zh_CN.qm`。
    打包时若没带进 `_internal/PySide6/translations`，本函数静默失败，
    界面回落到英文按钮 —— 不会崩，但打包脚本应确保带上（见 packaging/build.py）。
    """
    try:
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        tr = QTranslator(app)
        # qtbase 提供 QDialogButtonBox / QMessageBox 的标准按钮文案
        if tr.load(QLocale(QLocale.Language.Chinese, QLocale.Country.China), "qtbase", "_", path):
            app.installTranslator(tr)
            app._bs_translator = tr  # 防止被 GC
            return True
        if tr.load("qtbase_zh_CN", path):
            app.installTranslator(tr)
            app._bs_translator = tr
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def stylesheet(p: Palette, style: str | None = None) -> str:
    """生成整套 QSS。

    ## 1.1.2 改造要点（每条都有实测依据）

    1. **焦点可见**：改造前 `:focus` 只有一条规则，且**只覆盖输入类**——
       `QPushButton`(9 个规则块) 与 `QToolButton`(5 个) **都没有** `:focus`。
       同时 `outline: none` 主动关掉了 Qt 的系统焦点框。结果：键盘用户的焦点
       **完全不可见**（像素差实测 0/8084）。
    2. **disabled 可辨**：改造前 `QPushButton:disabled` 的 background 与正常态
       **同值**（`#EEF2F9`），规则生效但看不出；`QPushButton#Primary` 又因
       **ID 选择器特异性更高**而完全盖掉了 `:disabled`（实测
       enabled→`#4f6bed`、disabled→`#4f6bed` 逐像素相同）。
    3. **补箭头**：`down-arrow`/`up-arrow`/`right-arrow` 全缺失。
       `QComboBox::drop-down` 设了 `border: none; width: 22px` 却没画箭头，
       实测箭头区只有 32 个非背景像素（几乎空白）。
    4. **补表格选中态**：只有 `QListWidget::item:selected`，
       `QTableWidget::item:selected` 缺失 → 历史表选中行 1.24:1，看不出选哪行。
    5. **字号用 pt 而非 px**：`px` 无视系统文字缩放（实测系统设 20pt 后
       仍渲染 13px）。改 `pt` 后跟随系统。
    6. **卡片有层次**：`surface` vs `bg` 原本 1.08:1（几乎同色），
       靠加深边框 + 保留底色差来分层。
    """
    fg = p.text

    # ── 风格参数（1.1.3）：6 套外观共享下面的规则集 ──────────────
    # 只注入「几何 + 质感」，配色仍由 Palette 决定，两者正交。
    from .styles import current as _style_current, style_for as _style_for
    from .styles import styled_palette as _styled_palette

    _st = _style_for(style) if style else _style_current()
    p = _styled_palette(p, _st.key)
    R, RI, RB, BW = _st.radius, _st.radius_input, _st.radius_button, _st.border
    # 页签形态按风格：98 直角、mac/mag 胶囊、其余常规
    if _st.key == "98":
        _tab_radius, _tab_pad = "0px", "7px 14px"
    elif _st.key in ("mac", "mag"):
        _tab_radius, _tab_pad = "999px", "7px 18px"
    else:
        _tab_radius, _tab_pad = f"{RB}px", "8px 16px"
    fg = p.text

    def _hex2rgb(h: str) -> str:
        h = h.lstrip("#")
        return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"

    def _mix(h: str, target: int, ratio: float) -> str:
        h = h.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        f = lambda v: max(0, min(255, round(v + (target - v) * ratio)))  # noqa: E731
        return f"#{f(r):02X}{f(g):02X}{f(b):02X}"

    def _lighter(h: str, ratio: float = 0.35) -> str:
        return _mix(h, 255, ratio)

    def _darker(h: str, ratio: float = 0.25) -> str:
        return _mix(h, 0, ratio)

    # 卡片质感：**背景**与**边框**分开算，这样 bevel（立体边框）与
    # gradient（渐变背景）可以同时生效 —— 拟物风格两者都需要。
    _lighter = _lighter
    # 背景
    if _st.gradient:
        _top = _lighter(p.surface, 0.28 if not p.dark else 0.10)
        _bot = _darker(p.surface, 0.06 if not p.dark else 0.14)
        _card_fill = (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {_top}, stop:1 {_bot});"
        )
        _btn_fill = (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {_lighter(p.surface_alt, 0.35)}, stop:1 {p.surface_alt});"
        )
    elif _st.translucent:
        _a = 0.84 if not p.dark else 0.78
        _card_fill = f"background: rgba({_hex2rgb(p.surface)}, {_a});"
        _btn_fill = f"background: rgba({_hex2rgb(p.surface_alt)}, {_a});"
    else:
        _card_fill = f"background: {p.surface};"
        _btn_fill = f"background: {p.surface_alt};"

    # 边框
    if _st.bevel:
        # 3D 凸起：左上亮、右下暗（Win98 与拟物共用）
        _card_border = (
            f" border-top: {BW}px solid {_lighter(p.surface, 0.55)};"
            f" border-left: {BW}px solid {_lighter(p.surface, 0.55)};"
            f" border-right: {BW}px solid {_darker(p.surface, 0.45)};"
            f" border-bottom: {BW}px solid {_darker(p.surface, 0.45)};"
            f" border-radius: {R}px;"
        )
        _btn_border = (
            f" border-top: {BW}px solid {_lighter(p.surface_alt, 0.6)};"
            f" border-left: {BW}px solid {_lighter(p.surface_alt, 0.6)};"
            f" border-right: {BW}px solid {_darker(p.surface_alt, 0.4)};"
            f" border-bottom: {BW}px solid {_darker(p.surface_alt, 0.4)};"
            f" border-radius: {RB}px;"
        )
    elif _st.translucent:
        # 玻璃：外圈淡边 + 顶部高光（模拟折射）
        _hl = 0.55 if not p.dark else 0.22
        _card_border = (
            f" border: {BW}px solid rgba({_hex2rgb(p.border_strong)}, 0.6);"
            f" border-top: {BW}px solid rgba(255, 255, 255, {_hl});"
            f" border-radius: {R}px;"
        )
        _btn_border = (
            f" border: {BW}px solid rgba({_hex2rgb(p.border_strong)}, 0.5);"
            f" border-top: {BW}px solid rgba(255, 255, 255, {_hl * 0.9});"
            f" border-radius: {RB}px;"
        )
    elif _st.key == "mag":
        # Material：卡片**无边框**，靠投影分层；按钮是药丸（radius_button=20）
        _card_border = (
            f" border: none; border-bottom: 2px solid {_darker(p.border, 0.30)};"
            f" border-radius: {R}px;"
        )
        _btn_border = f" border: none; border-radius: {RB}px;"
    elif _st.key == "mac":
        # macOS：卡片/按钮**无描边**，只用底部一道投影线分层
        _card_border = (
            f" border: none; border-bottom: 2px solid {_darker(p.border, 0.22)};"
            f" border-radius: {R}px;"
        )
        _btn_border = f" border: none; border-radius: {RB}px;"
    else:
        _card_border = f" border: {BW}px solid {p.border}; border-radius: {R}px;"
        _btn_border = f" border: {BW}px solid {p.border}; border-radius: {RB}px;"

    # M3 药丸按钮需要更宽的内距（左右 24dp），其余风格保持 16px
    _primary_pad = "8px 24px" if _st.key == "mag" else "7px 16px"
    _card_bg = _card_fill + _card_border
    _btn_bg = _btn_fill + _btn_border

    # mac / Material / 玻璃：底部加深一档模拟投影
    # 投影线只给「本来就没有边框」的风格（mac/mag），避免与 _card_border
    # 里的 border-bottom 重复声明（实测会输出两条 border-bottom）。
    _card_shadow = ""
    # 焦点环：用 focus_ring 画 2px 外扩边框（QSS 无 box-shadow，
    # 用 border 加粗 + 变色是 Qt 里最可靠的做法）
    return f"""
    QWidget {{
        color: {fg};
        letter-spacing: {_st.letter_spacing}px;
        font-family: "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", "Helvetica Neue", sans-serif;
        font-size: 10pt;
    }}
    QMainWindow, QDialog, QWidget#Page {{ background: {p.bg}; }}

    /* ── 卡片：靠边框分层（surface vs bg 只有 1.08:1，单靠底色看不出） ── */
    QFrame#Card {{ {_card_bg}{_card_shadow} }}
    QFrame#CardFlat {{ background: {p.surface_alt}; border: {BW}px solid {p.border}; border-radius: {R}px; }}

    /* ── 字号层级 ── */
    QLabel#H1 {{ font-size: 17pt; font-weight: 600; }}
    QLabel#H2 {{ font-size: 12pt; font-weight: 600; }}
    QLabel#H3 {{ font-size: 11pt; font-weight: 600; }}
    QLabel#Muted {{ color: {p.text_dim}; }}
    QLabel#Badge {{
        border-radius: {RB}px; padding: 2px 10px; font-size: 9pt; font-weight: 600;
        background: {p.surface_alt}; color: {p.text_dim};
    }}
    QLabel#ErrorText {{ color: {p.danger}; }}
    QLabel#SuccessText {{ color: {p.success}; }}
    QLabel#WarningText {{ color: {p.warning}; }}

    /* ── 按钮 ── */
    QPushButton {{ {_btn_bg} padding: 7px 16px; color: {fg}; }}
    QPushButton:hover {{ background: {p.selection}; border-color: {p.border_strong}; }}
    QPushButton:pressed {{ background: {p.border}; }}
    QPushButton:focus {{
        border: 2px solid {p.focus_ring};
        padding: 6px 15px;
    }}
    /* disabled 必须与常态可区分：换背景 + 弱化文字 */
    QPushButton:disabled {{
        color: {p.disabled_fg};
        background: {p.disabled_bg};
        border-color: {p.border};
    }}
    QPushButton#Primary {{
        background: {p.accent}; border: {BW}px solid {p.accent};
        color: {p.accent_text}; font-weight: 600;
        border-radius: {RB}px;
        padding: {_primary_pad};
    }}
    QPushButton#Primary:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
    QPushButton#Primary:focus {{
        border: 2px solid {p.focus_ring};
        padding: 6px 15px;
    }}
    /* **关键**：必须用同样带 #Primary 的选择器，否则被上面那条盖掉 */
    QPushButton#Primary:disabled {{
        background: {p.disabled_bg};
        border: 1px solid {p.border};
        color: {p.disabled_fg};
    }}
    QPushButton#Danger {{ color: {p.danger}; }}
    QPushButton#Danger:disabled {{ color: {p.disabled_fg}; }}
    QPushButton#Link {{ background: transparent; border: none; color: {p.accent}; padding: 2px 4px; }}
    QPushButton#Link:hover {{ text-decoration: underline; }}
    QPushButton#Link:focus {{ border: 2px solid {p.focus_ring}; border-radius: {RI}px; }}
    QPushButton#Link:disabled {{ color: {p.disabled_fg}; }}

    /* ── 输入类 ── */
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit, QTimeEdit {{
        background: {p.surface};
        border: 1px solid {p.border_strong};
        border-radius: {RI}px;
        padding: 6px 9px;
        selection-background-color: {p.accent};
        selection-color: {p.accent_text};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QComboBox:focus, QDateTimeEdit:focus, QTimeEdit:focus {{
        border: 2px solid {p.focus_ring};
        padding: 5px 8px;
    }}
    QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
    QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled,
    QDateTimeEdit:disabled, QTimeEdit:disabled {{
        color: {p.disabled_fg}; background: {p.disabled_bg}; border-color: {p.border};
    }}

    /* ── 下拉框 ──
       1.1.2 教训（两次踩坑，都记在这里）：
       1) 用 QSS 的 border-* 拼三角箭头（`border-left: 4px solid transparent`），
          在 Windows 风格下渲染成**实心小黑方块**，比没箭头更难看。
          QSS 对子控件（::down-arrow）只支持 `image:`，不支持边框拼三角。
       2) 完全删掉 ::drop-down 规则也不行：原生箭头区会画成**一条黑边**。
       结论：给 ::drop-down 明确的背景色（与输入框同色），
       让原生箭头画在上面 —— 实测黑块消失、箭头可见（深色像素 8→2）。 */
    QComboBox::drop-down {{
        subcontrol-origin: padding; subcontrol-position: center right;
        width: 24px; border: none; background: {p.surface};
    }}
    QComboBox::drop-down:disabled {{ background: {p.disabled_bg}; }}
    /* **下拉弹层的黑底问题**（用户截图：弹层整个变成黑块）。
       原因：QComboBox 的弹层是**一个独立的顶层 QFrame**（不是 QAbstractItemView
       的子级），QSS 里只写 QComboBox QAbstractItemView 覆盖不到它，
       系统就用默认（深色）背景填充。
       修法：
       1) combobox-popup: 0 —— 强制用真正的 QListView 弹层，QSS 才生效；
       2) 补 QComboBox QFrame 规则，盖住最外层容器；
       3) 给 QAbstractItemView 也补背景（原已有，这里确保 item 背景不透明）。 */
    QComboBox {{
        combobox-popup: 0;
    }}
    QComboBox QFrame {{
        background: {p.surface}; border: 1px solid {p.border_strong};
        border-radius: {RI}px;
    }}
    QComboBox QAbstractItemView {{
        background: {p.surface}; border: 1px solid {p.border_strong}; selection-background-color: {p.selection};
        selection-color: {fg}; outline: none;
    }}

    /* ── 微调框：只给按钮留位置，箭头交给原生风格画 ── */
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QTimeEdit::up-button, QDateTimeEdit::up-button {{
        subcontrol-origin: border; subcontrol-position: top right;
        width: 18px; border: none; background: transparent;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button,
    QTimeEdit::down-button, QDateTimeEdit::down-button {{
        subcontrol-origin: border; subcontrol-position: bottom right;
        width: 18px; border: none; background: transparent;
    }}

    /* ── 列表/表格 ── */
    QListWidget, QTreeWidget, QTableWidget {{
        background: {p.surface}; border: 1px solid {p.border}; border-radius: {R}px;
        alternate-background-color: {p.surface_alt};
    }}
    /* 只对**真正看不见的**控件去掉 outline：列表/表格需要自己的焦点态，
       所以下面给它们补 :focus 边框，而不是简单 outline:none */
    QListWidget:focus, QTreeWidget:focus, QTableWidget:focus {{
        border: 2px solid {p.focus_ring};
    }}
    QComboBox QAbstractItemView {{ outline: none; }}
    QTextEdit, QPlainTextEdit {{
        background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: {R}px;
    }}
    QSplitter::handle:horizontal {{ width: 10px; }}
    QListWidget::item {{ border-radius: {R}px; padding: 10px 12px; margin: 3px 6px; }}
    QListWidget::item:selected {{ background: {p.selection}; color: {fg}; }}
    QListWidget::item:hover {{ background: {p.surface_alt}; }}
    QTreeWidget::item, QTableWidget::item {{ padding: 6px; }}
    /* **关键**：表格选中行原来完全没有样式（1.24:1，看不出选哪行） */
    QTableWidget::item:selected, QTreeWidget::item:selected {{
        background: {p.selection};
        color: {fg};
    }}
    QTableWidget::item:hover, QTreeWidget::item:hover {{ background: {p.surface_alt}; }}
    QHeaderView::section {{
        background: {p.surface_alt}; border: none; border-bottom: 1px solid {p.border};
        padding: 7px 8px; font-weight: 600;
    }}

    /* ── 页签 ── */
    QTabWidget::pane {{ border: 1px solid {p.border}; border-radius: {R}px; background: {p.surface}; top: -1px; }}
    QTabBar::tab {{
        background: transparent; padding: 8px 16px; margin-right: 4px;
        border-radius: {RI}px; color: {p.text_dim};
    }}
    QTabBar::tab:selected {{ background: {p.nav_active}; color: {p.accent}; font-weight: 600; }}
    QTabBar::tab:hover {{ color: {fg}; }}
    QTabBar::tab:focus {{ border: 2px solid {p.focus_ring}; }}

    /* ── 分组框 ── */
    QGroupBox {{
        border: 1px solid {p.border}; border-radius: {R}px; margin-top: 14px; padding: 14px 12px 12px 12px;
        background: transparent;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {p.text_dim}; font-weight: 600;
    }}

    /* ── 勾选/单选：加焦点态 ── */
    QCheckBox, QRadioButton {{ spacing: 8px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 17px; height: 17px; }}
    QCheckBox::indicator:unchecked {{ border: 1px solid {p.border_strong}; border-radius: 5px; background: {p.surface}; }}
    QCheckBox::indicator:checked {{ border: 1px solid {p.accent}; border-radius: 5px; background: {p.accent}; }}
    QCheckBox::indicator:disabled {{ border-color: {p.border}; background: {p.disabled_bg}; }}
    QCheckBox:focus {{ color: {p.focus_ring}; }}
    QRadioButton::indicator:unchecked {{ border: 1px solid {p.border_strong}; border-radius: {RB}px; background: {p.surface}; }}
    QRadioButton::indicator:checked {{ border: 5px solid {p.accent}; border-radius: {RB}px; background: {p.surface}; }}
    QRadioButton::indicator:disabled {{ border-color: {p.border}; background: {p.disabled_bg}; }}
    QRadioButton:focus {{ color: {p.focus_ring}; }}

    /* ── 滑块 ── */
    QSlider::groove:horizontal {{ height: 6px; background: {p.surface_alt}; border-radius: 3px; }}
    QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 3px; }}
    QSlider::handle:horizontal {{
        background: {p.surface}; border: 2px solid {p.accent}; width: 14px; height: 14px;
        margin: -5px 0; border-radius: {RB}px;
    }}
    QSlider:focus {{ border: 2px solid {p.focus_ring}; border-radius: {RI}px; }}

    /* ── 进度条 ── */
    QProgressBar {{
        background: {p.surface_alt}; border: none; border-radius: {RI}px; height: 12px; text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: {RI}px; }}

    /* ── 滚动条 ── */
    /* 滚动条（1.1.6）：更窄、handle 半透明、与窗口边缘留间距。
       原来 10px + 实色 border_strong 在玻璃风格下是突兀的深色竖条
       （用户反馈"边缘不协调"经逐像素证实就是这个）。 */
    QScrollBar:vertical {{
        background: transparent; width: 8px;
        margin: 4px 3px 4px 0;
    }}
    QScrollBar::handle:vertical {{ background: rgba({_hex2rgb(p.border_strong)}, 0.55); border-radius: 3px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: rgba({_hex2rgb(p.text_dim)}, 0.75); }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{
        background: transparent; height: 8px;
        margin: 0 4px 3px 4px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: rgba({_hex2rgb(p.text_dim)}, 0.75); }}
    QScrollBar::handle:horizontal {{ background: rgba({_hex2rgb(p.border_strong)}, 0.55); border-radius: 3px; min-width: 30px; }}

    /* ── 提示/菜单 ── */
    QToolTip {{
        background: {p.surface}; color: {fg}; border: 1px solid {p.border_strong};
        border-radius: {RI}px; padding: 6px 8px;
    }}
    QMenu {{
        background: {p.surface}; border: 1px solid {p.border_strong}; border-radius: {R}px; padding: 6px;
    }}
    QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: {RB}px; }}
    QMenu::item:selected {{ background: {p.selection}; }}
    QMenu::item:disabled {{ color: {p.disabled_fg}; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 8px; }}
    QMenu::indicator {{ width: 14px; height: 14px; }}
    QStatusBar {{ background: transparent; color: {p.text_dim}; }}
    QSplitter::handle {{ background: transparent; }}

    /* ── 工具按钮与导航 ── */
    QToolButton {{ border-radius: {RI}px; padding: 6px; background: transparent; }}
    QToolButton:hover {{ background: {p.surface_alt}; }}
    QToolButton:focus {{
        border: 2px solid {p.focus_ring};
        padding: 4px;
    }}
    QToolButton#NavButton {{
        background: transparent; border: none; border-radius: {R}px; padding: 10px 14px;
        text-align: left; color: {p.text_dim}; font-size: 10pt;
    }}
    QToolButton#NavButton:hover {{ background: {p.surface_alt}; color: {fg}; }}
    /* 选中态：底色 + 左侧 3px 强调条（比纯底色更"有设计感"） */
    QToolButton#NavButton:checked {{
        background: {p.nav_active}; color: {p.accent}; font-weight: 600;
        border-left: 3px solid {p.accent};
        padding-left: 11px;
    }}
    /* 焦点必须能在导航上看见 —— 这是键盘用户切换页面的唯一线索 */
    QToolButton#NavButton:focus {{
        border: 2px solid {p.focus_ring};
        color: {fg};
    }}

    /* ── 1.1.2 新增：说明文字、分隔线、空状态 ── */
    QLabel#Caption {{
        color: {p.text_dim}; font-size: 9pt; font-weight: 700;
        padding: 2px 4px; background: transparent;
    }}
    QFrame#Divider {{ background: {p.border}; border: none; max-height: 1px; min-height: 1px; }}
    /* 空状态：大图标 + 标题 + 说明 */
    QLabel#EmptyIcon {{ color: {p.border_strong}; font-size: 30pt; font-weight: 400; }}
    QLabel#EmptyTitle {{ color: {fg}; font-size: 11pt; font-weight: 600; }}
    QLabel#EmptyHint {{ color: {p.text_dim}; font-size: 9pt; }}
    /* 信息卡片的标题行（比正文小、比说明深） */
    QLabel#CardCap {{ color: {p.text_dim}; font-size: 9pt; font-weight: 600; }}
    QLabel#CardVal {{ color: {fg}; font-size: 10pt; font-weight: 600; }}
    /* 侧栏底部的状态条 */
    QFrame#SideFooter {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: {RB}px; }}
    """
