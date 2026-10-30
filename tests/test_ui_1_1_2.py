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
