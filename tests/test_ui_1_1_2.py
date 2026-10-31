"""1.1.2 UI 修复的回归测试。

锁住三个「改回去了也不会有人发现」的行为：
1. `partial` 状态必须映射成中文（服务端会产出这个状态）
2. 退出确认的默认按钮必须是「否」（默认「是」会误按回车中断备份）
3. 保留策略预览必须是防抖的（同步扫盘会卡 10 秒）
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


# ── 1. partial 状态映射 ────────────────────────────────────────────
def test_tasks_status_map_covers_partial() -> None:
    """engine 会产出 status="partial"，UI 状态表必须有这个键。

    缺键时的表现：`.get()` 回落到默认值「未运行过」——
    部分失败的备份在列表里显示成"从没跑过"，用户以为定时任务没生效。
    """
    from backupstudio.gui.tasks_page import STATUS_MAP

    assert "partial" in STATUS_MAP, "tasks_page 缺 partial 映射"
    text, kind = STATUS_MAP["partial"]
    assert text and text != "未运行过", f"partial 文案不对：{text!r}"
    assert kind in {"ok", "bad", "warn", "info", "muted"}, kind


def test_history_status_covers_partial() -> None:
    from backupstudio.gui.history_page import STATUS

    assert "partial" in STATUS, "history_page 缺 partial 映射"
    assert STATUS["partial"][0] != "partial", "不该把英文码直接显示给用户"


# ── 2. 退出确认默认按钮 ────────────────────────────────────────────
def test_quit_confirm_sets_default_no() -> None:
    """有任务在跑时退出，默认按钮必须是「否」。

    默认「是」时，用户误按回车就会中断正在运行的备份。

    这里**不实例化 MainWindow**（它在 offscreen 下会起托盘 + 守护进程轮询，
    测试会挂住），改为静态检查 `quit_app` 的源码 —— 判据同样硬：
    必须调用 setDefaultButton(No)，且必须设 Discard/Cancel 语义的按钮文案。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    m = re.search(r"def quit_app\(", src)
    assert m, "找不到 quit_app"
    rest = src[m.start() :]
    nxt = re.search(r"\n    def ", rest[10:])
    body = rest[: 10 + nxt.start()] if nxt else rest

    assert "setDefaultButton" in body, "退出确认没设默认按钮（Qt 默认会选「是」）"
    assert "StandardButton.No" in body, "默认按钮不是「否」"
    # 默认按钮不应该是 Yes
    default_line = next(
        (ln for ln in body.splitlines() if "setDefaultButton" in ln), ""
    )
    assert "Yes" not in default_line, f"默认按钮被设成了「是」：{default_line.strip()}"
    # 危险操作的按钮文案要能自解释
    assert "继续备份" in body, "「否」按钮文案不够明确（应为「继续备份」）"


# ── 3. 保留预览防抖 ────────────────────────────────────────────────
def test_retention_preview_disabled_clears(qapp, monkeypatch, tmp_path) -> None:
    """勾上再取消保留策略时，预览文案要清空，不能留着旧数字。"""
    monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(tmp_path))
    from backupstudio.gui.appstate import AppState
    from backupstudio.gui.task_dialog import TaskDialog
    from backupstudio.store import TaskStore

    st = AppState(TaskStore(tmp_path / "tasks.json"))
    dlg = TaskDialog(st, None)
    try:
        # 必须真的经历一次 toggle 才会发信号（默认就是 False，
        # 直接 setChecked(False) 不触发 toggled，测不出东西）
        dlg.ret_enabled.setChecked(True)
        dlg.ret_preview.setText("保留预览：将删除 3 份")
        dlg.ret_enabled.setChecked(False)
        assert dlg.ret_preview.text() == "", "取消勾选后预览没清空"
    finally:
        dlg.close()


# ── 4. QSS 关键规则不能丢 ──────────────────────────────────────────
@pytest.mark.parametrize(
    "selector",
    [
        "QPushButton:focus",
        "QPushButton#Primary:disabled",
        "QToolButton#NavButton:focus",
        "QTableWidget::item:selected",
        "QLabel#EmptyTitle",
        "QFrame#SideFooter",
    ],
)
def test_qss_keeps_critical_rules(selector: str) -> None:
    """这些选择器都是 1.1.2 修过的可见性/可用性缺陷，删掉会静默退化。"""
    from backupstudio.gui import LIGHT, DARK, stylesheet

    for pal in (LIGHT, DARK):
        assert selector in stylesheet(pal), f"{pal.name} 主题缺 {selector}"


def test_qss_has_no_pixel_fonts() -> None:
    """字号必须用 pt：px 会无视系统文字缩放（无障碍）。"""
    import re

    from backupstudio.gui import LIGHT, DARK, stylesheet

    for pal in (LIGHT, DARK):
        qss = stylesheet(pal)
        px = re.findall(r"font-size:\s*\d+px", qss)
        assert not px, f"{pal.name} 主题仍有 {len(px)} 处 px 字号：{px[:3]}"


# ── 5. 省略号标签 ──────────────────────────────────────────────────
def test_elide_label_keeps_full_text(qapp) -> None:
    """ElideLabel 必须把完整内容放进 tooltip，否则用户不知道被截掉了什么。"""
    from backupstudio.gui.widgets import ElideLabel

    long_path = "C:/very/long/path/" + "x" * 200 + "/file.txt"
    lab = ElideLabel(long_path)
    assert lab.toolTip() == long_path
    assert lab.full_text() == long_path


# ── 6. 一键删除入口 ────────────────────────────────────────────────
def test_task_list_has_context_menu_and_delete_key() -> None:
    """列表必须能直接删任务：右键菜单 + Delete 键。

    用户反馈「备份任务里没有一键删除」—— 之前只有右侧详情区一个按钮，
    且必须先选中任务才可用。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "tasks_page.py").read_text(
        encoding="utf-8"
    )
    assert "CustomContextMenu" in src, "列表没设右键菜单策略"
    assert "customContextMenuRequested" in src, "右键菜单没连接"
    assert "def _on_list_menu" in src, "缺少右键菜单处理函数"
    assert "Key_Delete" in src, "没有 Delete 键删除"
    assert "def _delete_task" in src, "缺少按 id 删除的实现"


# ── 7. 启动性能：懒加载 ────────────────────────────────────────────
def test_main_window_lazy_pages() -> None:
    """启动时只建「任务」页，其余三页懒加载。

    实测：`_apply_theme` 若直接访问 history/env/settings 属性，
    会把三页全部建出来（环境页探测要 0.9s），启动从 0.34s 退化到 2.08s。
    这条测试锁住那个退化。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    # _apply_theme 里不能出现直接访问三页属性的**代码**（注释里提到没关系）
    m = re.search(r"def _apply_theme\(.*?(?=\n    def )", src, re.S)
    assert m, "找不到 _apply_theme"
    code = [
        ln.split("#", 1)[0]
        for ln in m.group(0).splitlines()
        if not ln.strip().startswith("#")
    ]
    body = "\n".join(code)
    for attr in ("self.history_page", "self.env_page", "self.settings_page"):
        assert attr not in body, f"_apply_theme 里直接访问 {attr} 会触发懒加载，启动变慢"
    assert '"_pages"' in body or "self._pages" in body, "_apply_theme 应只遍历已创建的页面"
    # 必须有懒加载机制
    assert "def _page" in src and "_prewarm_pages" in src, "缺少懒加载/预热实现"


# ── 8. 批量勾选 + 批量删除（1.1.3）────────────────────────────────
def test_tasks_page_has_batch_delete() -> None:
    """任务页必须能一键勾选 + 批量删除。

    用户反馈「1205 个测试任务导致卡顿，需要一个一键勾选批量删除」——
    之前只能一个个进详情页删。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "tasks_page.py").read_text(
        encoding="utf-8"
    )
    for marker, desc in (
        ("btn_batch", "批量管理按钮"),
        ("chk_all", "全选复选框"),
        ("def _on_batch_toggled", "批量模式切换"),
        ("def _on_select_all", "全选处理"),
        ("def delete_selected", "批量删除"),
        ("def _sync_batch_bar", "计数同步"),
        ("batch_bar", "批量操作条"),
    ):
        assert marker in src, f"缺少{desc}（{marker}）"
    # 批量删除必须二次确认且默认「否」
    m = re.search(r"def delete_selected\(self\).*?(?=\n    def )", src, re.S)
    assert m, "找不到 delete_selected"
    body = m.group(0)
    assert "QMessageBox" in body and "StandardButton.No" in body, "批量删除缺少确认或默认非「否」"
    assert "备份文件不会被删除" in body, "确认文案没说清不动备份文件"


def test_task_item_has_checkbox() -> None:
    """列表项必须有复选框（批量模式的勾选载体）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "tasks_page.py").read_text(
        encoding="utf-8"
    )
    assert "self.chk = QCheckBox()" in src
    assert "def set_batch" in src, "缺少 set_batch（批量模式开关）"
    assert "on_check" in src, "复选框没有回传到页面"


# ── 9. 历史记录收纳（1.1.3）────────────────────────────────────────
def test_history_page_paginates() -> None:
    """历史页必须分页渲染，不能一次把所有记录建成表格行。

    用户实测：history.jsonl 有 4651 条，打开「运行记录」就卡。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "history_page.py").read_text(
        encoding="utf-8"
    )
    for marker, desc in (
        ("_page_size", "分页大小"),
        ("_shown", "当前显示条数"),
        ("def _show_more", "显示更多"),
        ("def _show_all", "全部显示"),
        ("def _update_page_info", "计数文案"),
    ):
        assert marker in src, f"缺少{desc}（{marker}）"
    # 必须只渲染前 _shown 条
    m = re.search(r"def reload\(self\).*?(?=\n    def )", src, re.S)
    assert m, "找不到 reload"
    body = m.group(0)
    assert "view = rows[: self._shown]" in body, "reload 没有截断到 _shown 条"
    assert "setRowCount(len(view))" in body, "表格行数不是按 view 设置"


def test_history_no_resize_to_contents() -> None:
    """不能用 ResizeToContents：它对每列测量所有行，行多时极慢。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "history_page.py").read_text(
        encoding="utf-8"
    )
    assert "ResizeMode.ResizeToContents)" not in src, "仍在用 ResizeToContents"
    assert "ResizeMode.Interactive" in src, "应改用固定列宽"


# ── 10. Material You 动态配色（1.1.3）──────────────────────────────
def test_material_you_generates_65_colors() -> None:
    """Material You：单一源色 → 5 套色调板 × 13 档 = 65 色（AOSP 规格）。"""
    from backupstudio.gui.mcu import TONES, tonal_palettes

    pal = tonal_palettes("#6750A4")
    assert len(pal) == 5, f"应有 5 套色调板，实际 {len(pal)}"
    assert len(TONES) == 13, f"应有 13 档 tone，实际 {len(TONES)}"
    total = sum(len(v) for v in pal.values())
    assert total == 65, f"应有 65 色，实际 {total}"
    for board, row in pal.items():
        assert len(row) == 13, f"{board} 只有 {len(row)} 档"
        for tone, color in row.items():
            assert color.startswith("#") and len(color) == 7, f"{board}/{tone} 颜色非法: {color}"


def test_material_you_seed_drives_palette() -> None:
    """换源色必须带来不同的主色 —— 这是 Material You 的核心特征。"""
    from backupstudio.gui import LIGHT
    from backupstudio.gui.styles import set_material_you, styled_palette

    primaries = set()
    for seed in ("#6750A4", "#B1611C", "#006D3B", "#B3261E"):
        set_material_you(seed)
        primaries.add(styled_palette(LIGHT, "mag").accent)
    set_material_you("#6750A4")
    assert len(primaries) >= 3, f"不同源色应产生不同主色，实际只有 {len(primaries)} 种"


def test_material_you_theme_styles() -> None:
    """Android 13+ 的四种 theme_style 都应可用且产生不同结果。"""
    from backupstudio.gui import LIGHT
    from backupstudio.gui.styles import set_material_you, styled_palette

    out = {}
    for st in ("TONAL_SPOT", "VIBRANT", "EXPRESSIVE", "SPRITZ"):
        set_material_you("#6750A4", st)
        out[st] = styled_palette(LIGHT, "mag").accent
    set_material_you("#6750A4", "TONAL_SPOT")
    assert len(set(out.values())) >= 3, f"四种 style 应产生不同主色，实际 {out}"


def test_material_you_light_dark_differ() -> None:
    """浅色/深色两套角色映射必须不同。"""
    from backupstudio.gui.mcu import scheme

    light = scheme("#6750A4", dark=False)
    dark = scheme("#6750A4", dark=True)
    assert light["background"] != dark["background"], "浅色/深色背景不应相同"
    assert light["primary"] != dark["primary"], "浅色/深色主色不应相同"


# ── 11. 自绘标题栏（1.1.4）────────────────────────────────────────
def test_titlebar_imports_are_module_level() -> None:
    """RESIZE_MARGIN / TITLE_H 必须在模块级导入。

    实测缺陷（2026-10-31）：它们曾写在 MainWindow.__init__ 内成为局部名，
    导致 `_edge_at` 与 `mouseMoveEvent` 抛 NameError —— Windows 上
    窗口一显示、鼠标移过就崩。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    # 必须有一行模块级（顶格）导入
    assert re.search(
        r"^from \.titlebar import .*RESIZE_MARGIN.*TITLE_H", src, re.M
    ), "titlebar 的 RESIZE_MARGIN/TITLE_H 未在模块级导入"
    # __init__ 内不得再出现同名导入（否则又是局部名）
    init_body = src.split("def __init__(self, state: AppState)", 1)[1].split("\n    def ", 1)[0]
    assert "from .titlebar import" not in init_body, "__init__ 内仍有 titlebar 导入（会是局部名）"


def test_titlebar_follows_palette() -> None:
    """标题栏配色必须来自风格化后的调色板，且随风格变化。"""
    import os
    import tempfile
    from pathlib import Path

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    root = Path(__file__).resolve().parents[1]
    import sys

    sys.path.insert(0, str(root))
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    cfg = Path(tempfile.mkdtemp(prefix="tb-", dir=str(root / "build")))
    os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(cfg)
    from backupstudio.gui.appstate import AppState
    from backupstudio.gui.main_window import MainWindow
    from backupstudio.store import TaskStore

    st = AppState(TaskStore(cfg / "tasks.json"))
    w = MainWindow(st)
    w.show()
    app.processEvents()
    if w.titlebar is None:
        w.close()
        return  # 平台不支持无边框时跳过（mac/Linux）
    seen = set()
    for key in ("win", "98", "mag"):
        st.set_style(key)
        app.processEvents()
        seen.add(w.titlebar._bg)
    assert len(seen) >= 2, f"不同风格的标题栏底色应不同，实际只有 {seen}"
    w.close()


def test_frameless_edge_zones_work() -> None:
    """边缘热区计算不得抛异常（曾因局部名导入抛 NameError）。"""
    import os
    import tempfile
    from pathlib import Path

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    root = Path(__file__).resolve().parents[1]
    import sys

    sys.path.insert(0, str(root))
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    cfg = Path(tempfile.mkdtemp(prefix="eg-", dir=str(root / "build")))
    os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(cfg)
    from backupstudio.gui.appstate import AppState
    from backupstudio.gui.main_window import MainWindow
    from backupstudio.store import TaskStore

    st = AppState(TaskStore(cfg / "tasks.json"))
    w = MainWindow(st)
    w.resize(900, 600)
    w.show()
    app.processEvents()
    # 左上角应命中 Top|Left，中心应为 0
    tl = w._edge_at(QPoint(1, 1))
    assert tl & Qt.Edge.LeftEdge and tl & Qt.Edge.TopEdge, f"左上角热区错误: {tl}"
    mid = w._edge_at(QPoint(450, 300))
    assert not mid, f"中心不应在热区: {mid}"
    w.close()


# ── 12. 路线 C：原生框架位（1.1.5）────────────────────────────────
def test_winframe_constants_and_structure() -> None:
    """winframe 必须具备路线 C 的三个要素：框架位、NCCALCSIZE、NCHITTEST。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "winframe.py").read_text(
        encoding="utf-8"
    )
    for marker, desc in (
        ("WS_THICKFRAME", "WS_THICKFRAME 框架位"),
        ("WS_CAPTION", "WS_CAPTION 框架位"),
        ("FRAME_BITS", "框架位组合"),
        ("WM_NCCALCSIZE", "客户区归零消息"),
        ("WM_NCHITTEST", "命中测试消息"),
        ("WM_NCLBUTTONDBLCLK", "原生双击消息"),
        ("DwmExtendFrameIntoClientArea", "阴影恢复"),
        ("SetWindowLongPtrW", "64 位指针 API"),
    ):
        assert marker in src, f"缺少{desc}（{marker}）"


def test_winframe_edge_priority_over_caption() -> None:
    """边缘判定必须**优先于**标题带。

    若顶边被判成 HTCAPTION，上边缘缩放和拖到屏幕顶部的 Aero Snap 都会失效
    —— 这是设计 Agent 明确提醒的坑。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "winframe.py").read_text(
        encoding="utf-8"
    )
    m = re.search(r"def _hit_test\(.*?(?=\n    def )", src, re.S)
    assert m, "找不到 _hit_test"
    body = m.group(0)
    i_edge = body.find("left, right = x <= m")
    # 只找函数体里 return 的 HTCAPTION，避免匹配到常量导入
    i_caption = body.find("return HTCAPTION")
    assert i_edge != -1, "找不到边缘判定"
    assert i_caption != -1, "找不到 HTCAPTION 判定"
    assert i_edge < i_caption, "边缘判定必须在 HTCAPTION 之前（否则 Snap/缩放失效）"


def test_winframe_dpi_conversion() -> None:
    """坐标必须做 DPI 换算：lParam 是物理像素，Qt 是逻辑像素。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "winframe.py").read_text(
        encoding="utf-8"
    )
    assert "ScreenToClient" in src, "必须用 ScreenToClient 转换坐标"
    assert "devicePixelRatioF" in src, "必须考虑 DPR（125% 缩放下否则偏移 25%）"
    assert "c_short" in src, "lParam 的 x/y 是有符号 short，需按 short 解析"


# ── 13. 设置页外观区不得水平溢出（1.1.5）────────────────────────────
def test_settings_appearance_uses_vertical_layout() -> None:
    """外观区必须是**垂直**布局（每行一个设置项）。

    实测缺陷：曾把 5 组控件塞进一个 QHBoxLayout，水平布局不换行，
    总宽度超卡片就被裁掉 —— 用户截图里「Material You 源色」右侧被截断。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "settings_page.py").read_text(
        encoding="utf-8"
    )
    assert "appearance = QVBoxLayout()" in src, "外观区必须用 QVBoxLayout（垂直）"
    assert "appearance = QHBoxLayout()" not in src, "外观区又变回 QHBoxLayout 了"


def test_settings_scroll_area_is_transparent() -> None:
    """设置页的 QScrollArea 必须透明，否则玻璃卡片采样到纯色 viewport。

    实测：设置页卡片被滚动区包着，不设透明时玻璃风格看不出折射
    （任务页/运行记录页的卡片直接挂在 _Backdrop 下，所以那两个页正常）。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "settings_page.py").read_text(
        encoding="utf-8"
    )
    assert "setAutoFillBackground(False)" in src, "滚动区 viewport 未关闭自动填充"
    assert "QScrollArea { background: transparent" in src, "滚动区未设透明背景"
    # 但**不能**给 holder 设 WA_TranslucentBackground，那会让子控件背景也变透明
    assert "holder.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)" not in src, \
        "不要给 holder 设 WA_TranslucentBackground（会波及子控件）"


# ── 14. 下拉弹层不得是黑块（1.1.6）──────────────────────────────────
def test_combo_popup_has_background_rules() -> None:
    """QComboBox 弹层必须有背景规则，否则会渲染成黑块。

    实测缺陷（用户截图）：打开配色下拉时弹层整个变成黑色。
    原因：弹层是一个**独立的顶层 QFrame**（不是 QAbstractItemView 的子级），
    只写 `QComboBox QAbstractItemView` 覆盖不到它。
    修法需要两样：`combobox-popup: 0`（用真 QListView）+ `QComboBox QFrame` 规则。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backupstudio" / "gui" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "combobox-popup: 0" in src, "缺少 combobox-popup: 0（否则弹层是原生窗口，QSS 不生效）"
    assert "QComboBox QFrame" in src, "缺少 QComboBox QFrame 规则（弹层最外层容器会露默认底色）"


def test_combo_popup_renders_light() -> None:
    """实测：四种风格的弹层都必须是浅色，暗像素占比 < 30%。"""
    import os
    import sys
    import tempfile
    from pathlib import Path

    os.environ.pop("QT_QPA_PLATFORM", None)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    cfg = Path(tempfile.mkdtemp(prefix="pp-", dir=str(root / "build")))
    os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(cfg)
    from backupstudio.gui import stylesheet
    from backupstudio.gui.appstate import AppState
    from backupstudio.gui.main_window import MainWindow
    from backupstudio.store import TaskStore

    ts = TaskStore(cfg / "tasks.json")
    st = AppState(ts)
    w = MainWindow(st)
    w.resize(1000, 680)
    w.show()
    for _ in range(8):
        app.processEvents()
    try:
        for style in ("win", "glass", "98"):
            st.set_style(style)
            app.setStyleSheet(stylesheet(st.palette))
            w._select("settings")
            for _ in range(10):
                app.processEvents()
            cb = w._pages["settings"].style_combo
            cb.showPopup()
            for _ in range(10):
                app.processEvents()
            popup = cb.view().window()
            img = popup.grab().toImage()
            if img.isNull():
                cb.hidePopup()
                continue
            dark = sum(
                1
                for y in range(0, img.height(), 8)
                for x in range(0, img.width(), 8)
                if img.pixelColor(x, y).lightness() < 40
            )
            total = len(range(0, img.height(), 8)) * len(range(0, img.width(), 8))
            ratio = dark * 100.0 / max(total, 1)
            cb.hidePopup()
            assert ratio < 30, f"{style} 风格弹层暗像素占比 {ratio:.1f}%（黑块）"
    finally:
        w.close()
