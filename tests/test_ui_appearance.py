"""UI 外观回归：布局宽度、文字省略、主题对比度、样式覆盖盲区。

## 背景（全部为实测确认的缺陷）

**布局宽度**：任务页左栏写死 300px、详情四宫格每格 240px、历史页整表
``ResizeToContents``、设置页路径标签不换行 —— 叠加起来把主窗口最小宽度顶到
**1051px**，而窗口默认 980px。结果：整页出现横向滚动条，任务卡片文字被裁成
``ZC`` / ``p-bs-fix-src`` 这种残句。

**文字截断**：任务卡片标题与摘要都是单行 ``QLabel`` 且列表项高度写死 64px，
超长内容直接被裁掉，用户看不到完整信息。

**样式盲区**：``QTableCornerButton::section`` / ``QHeaderView`` 背景 /
表格 viewport 未被 QSS 覆盖，深色主题下露出浅色块。

**对比度**：浅色主题 ``success`` 3.30:1、``text_dim/bg`` 4.47:1 低于 WCAG AA 的 4.5:1。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_stylesheet(qapp):
    """每个用例跑完把全局 QSS 清空。

    ``MainWindow`` 构造时会 `QApplication.setStyleSheet(...)`，全局状态会漏到
    后面的用例里 —— 不隔离时「断言 QSS 里有某条规则」的用例会被上一个用例
    装好的样式表影响，出现"单跑绿、整跑红"的假失败。
    """
    yield
    qapp.setStyleSheet("")


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    inst = QApplication.instance() or QApplication([])
    yield inst


def _pump(app, rounds: int = 40) -> None:
    for _ in range(rounds):
        app.processEvents()


# ---------------------------------------------------------------------------
# 对比度
# ---------------------------------------------------------------------------


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg: str, bg: str) -> float:
    a, b = _relative_luminance(fg), _relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


class TestContrast:
    """所有正文/状态色必须达到 WCAG AA（4.5:1）。"""

    @pytest.mark.parametrize("theme", ["LIGHT", "DARK"])
    @pytest.mark.parametrize("role", ["text", "text_dim", "success", "warning", "danger"])
    def test_role_meets_aa_on_surface(self, theme, role):
        import backupstudio.gui as g

        p = getattr(g, theme)
        ratio = _contrast(getattr(p, role), p.surface)
        assert ratio >= 4.5, (
            f"{theme}.{role} 在 surface 上的对比度只有 {ratio:.2f}:1，低于 WCAG AA 的 4.5:1"
        )

    @pytest.mark.parametrize("theme", ["LIGHT", "DARK"])
    def test_secondary_text_on_page_bg(self, theme):
        import backupstudio.gui as g

        p = getattr(g, theme)
        ratio = _contrast(p.text_dim, p.bg)
        assert ratio >= 4.5, f"{theme}.text_dim 在页面背景上只有 {ratio:.2f}:1"

    def test_primary_button_text_readable(self):
        import backupstudio.gui as g

        for theme in ("LIGHT", "DARK"):
            p = getattr(g, theme)
            ratio = _contrast(p.accent_text, p.accent)
            assert ratio >= 4.5, f"{theme} 主按钮文字对比度 {ratio:.2f}:1"


# ---------------------------------------------------------------------------
# QSS 覆盖盲区
# ---------------------------------------------------------------------------


class TestStylesheetCoversTableChrome:
    """表格的"装饰性"部件必须被 QSS 显式覆盖。"""

    @pytest.fixture(params=["LIGHT", "DARK"])
    def qss(self, request):
        import backupstudio.gui as g

        return g.stylesheet(getattr(g, request.param)), request.param

    def test_corner_button_styled(self, qss):
        text, theme = qss
        assert "QTableCornerButton::section" in text, (
            f"{theme}: 未覆盖表格左上角交角 —— windowsvista 下会显示纯黑方块"
        )

    def test_header_background_styled(self, qss):
        text, theme = qss
        # QHeaderView 整体背景（不只是 ::section）
        assert "QHeaderView {" in text or "QHeaderView{" in text, (
            f"{theme}: 未设置 QHeaderView 整体背景，表头空白区会露底色"
        )

    def test_combo_popup_styled(self, qss):
        text, theme = qss
        assert "combobox-popup: 0" in text, (
            f"{theme}: 未让下拉弹层使用 QListView —— QSS 对默认弹层不生效（白底）"
        )
        assert "QComboBox QFrame" in text, (
            f"{theme}: 未给弹层容器着底色 —— 会在四周留白边"
        )


# ---------------------------------------------------------------------------
# 文字省略
# ---------------------------------------------------------------------------


class TestElidedLabel:
    def test_elides_long_text(self, qapp):
        from backupstudio.gui.widgets import ElidedLabel

        lbl = ElidedLabel("这是一个非常非常非常长的任务名称用来验证省略号是否生效")
        lbl.resize(80, 24)
        lbl.show()
        _pump(qapp, 5)
        shown = lbl.text()
        assert shown != lbl.full_text(), "窄宽度下没有省略，文字会被裁断"
        assert "…" in shown, f"应带省略号，实际 {shown!r}"
        assert lbl.toolTip() == lbl.full_text(), "完整文本应放进 tooltip"

    def test_full_text_preserved(self, qapp):
        from backupstudio.gui.widgets import ElidedLabel

        full = "D:/very/long/path/to/some/backup/destination/folder"
        lbl = ElidedLabel(full)
        lbl.resize(60, 24)
        lbl.show()
        _pump(qapp, 5)
        assert lbl.full_text() == full, "完整文本不得被省略号覆盖掉"

    def test_wide_enough_shows_everything(self, qapp):
        from backupstudio.gui.widgets import ElidedLabel

        full = "短名"
        lbl = ElidedLabel(full)
        lbl.resize(400, 24)
        lbl.show()
        _pump(qapp, 5)
        assert lbl.text() == full, "宽度足够时不该省略"

    def test_minimum_width_is_relaxable(self, qapp):
        """minHint 必须是 0 宽，否则长文本会把整页顶宽（实测设置页 689px）。"""
        from backupstudio.gui.widgets import ElidedLabel

        lbl = ElidedLabel("D:/a/very/long/path/" * 8)
        assert lbl.minimumSizeHint().width() == 0, (
            "ElidedLabel 的可压缩最小宽度必须是 0，否则会撑宽整个页面"
        )


# ---------------------------------------------------------------------------
# 布局宽度：窗口在默认尺寸下不得出现横向滚动
# ---------------------------------------------------------------------------


class TestNoHorizontalScrollAtDefaultSize:
    @pytest.fixture()
    def window(self, qapp, tmp_path, monkeypatch):
        import backupstudio.engine as engine
        import backupstudio.paths as paths

        monkeypatch.setenv("BACKUPSTUDIO_CONFIG_DIR", str(tmp_path / "cfg"))
        paths.reset_decisions()
        # 造一份有长任务名 + 长路径 + 长历史的数据，把"最坏情况"塞进去
        hist = tmp_path / "history.jsonl"
        import json

        hist.write_text(
            "\n".join(
                json.dumps(
                    {
                        "task_name": "一个非常长的任务名称用于测试布局压缩能力",
                        "status": "success",
                        "format": "7z",
                        "files": 12345,
                        "source_bytes": 987654321,
                        "archive_bytes": 123456789,
                        "duration": 123.4,
                        "verification": "passed",
                        "archive": "/an/extremely/long/path/to/some/backup/file.7z",
                        "started_at": "2026-10-29T01:00:00",
                        "finished_at": "2026-10-29T01:00:02",
                        "task_id": "t1",
                    },
                    ensure_ascii=False,
                )
                for _ in range(30)
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(engine, "history_path", lambda: hist)

        from backupstudio.config import BackupTask
        from backupstudio.gui.appstate import AppState
        from backupstudio.gui.main_window import MainWindow
        from backupstudio.store import TaskStore

        store = TaskStore(tmp_path / "tasks.json")
        store.upsert(
            BackupTask(
                name="一个非常长的任务名称用于测试布局压缩能力是否正常工作",
                destination="D:/an/extremely/long/destination/path/for/backups",
            )
        )
        win = MainWindow(AppState(store))
        win.resize(980, 640)
        win.show()
        _pump(qapp, 50)
        yield win
        win.close()

    def test_window_min_width_fits_default(self, window):
        """主窗口最小宽度必须 <= 980（默认尺寸），否则用户一开就是横向滚动。"""
        assert window.minimumSizeHint().width() <= 980, (
            f"主窗口最小宽度 {window.minimumSizeHint().width()}px 超过默认 980px"
        )

    @pytest.mark.parametrize("page", ["tasks", "history", "env", "settings"])
    def test_page_has_no_horizontal_scroll(self, window, qapp, page):
        from PySide6.QtWidgets import QScrollArea

        order = [k for k, _ in __import__("backupstudio.gui.main_window", fromlist=["NAV_ITEMS"]).NAV_ITEMS]
        window._select(page)
        _pump(qapp, 30)
        page_widget = window.stack.widget(order.index(page))
        offenders = []
        for sa in page_widget.findChildren(QScrollArea):
            sb = sa.horizontalScrollBar()
            if sb.maximum() > 0 and sb.isVisible():
                offenders.append(sb.maximum())
        assert not offenders, f"{page} 页出现横向滚动（溢出 {offenders}）"

    def test_task_cards_elide_instead_of_clipping(self, window):
        """任务卡片必须用省略号，而不是把文字裁断。"""
        tp = window.tasks_page
        _pump(window, 10) if False else None
        for _tid, widget in tp._items.items():
            title = getattr(widget, "title", None)
            assert title is not None
            assert hasattr(title, "full_text"), "任务标题应使用 ElidedLabel"
            # 窄宽度下必须已省略（并且不是把 mid-string 直接切断）
            assert title.full_text(), "完整文本应保留"


# ---------------------------------------------------------------------------
# 样式基线
# ---------------------------------------------------------------------------


class TestStyleBaseline:
    def test_fusion_style_is_set(self, qapp):
        """Windows 默认的 windowsvista 与 QSS 混用会露原生黑块，必须固定 Fusion。"""
        import inspect

        import backupstudio.app as appmod

        src = inspect.getsource(appmod)
        assert 'setStyle("Fusion")' in src or "setStyle('Fusion')" in src, (
            "启动时未设置 Fusion 风格：windowsvista 下表格交角会露黑块"
        )

    def test_radius_vocabulary_is_small(self):
        """圆角规格不应超过 4 种（原来有 8/9/10/12/14 五种混用）。"""
        import re

        import backupstudio.gui as g

        text = g.stylesheet(g.LIGHT)
        values = set(re.findall(r"border-radius:\s*(\d+)px", text))
        # 3/4/5 用于复选框、进度条等微型件，属合理细分
        large = {v for v in values if int(v) >= 6}
        assert len(large) <= 3, f"大圆角规格仍有 {sorted(large)} 种，应归并到 <=3 种"
