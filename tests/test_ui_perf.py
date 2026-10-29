"""UI 性能回归：切页不得阻塞 UI 线程，重置缓存必须真的重置。

## 缺陷（实测）

**P0-1 切页卡顿**：``main_window._select()`` 同步调用 ``page.on_shown()``。
* 环境页：``on_shown`` → ``rescan()`` 第一行 ``reset_cache()``，随后串行启动
  7za/unrar/tar 子进程并查注册表，实测阻塞 **921–1237 ms**，窗口标题显示「未响应」。
* 运行记录页：每次 ``reload()`` 重建 300 行 × 10 列 = 3000 个 QTableWidgetItem，
  实测阻塞 **195–250 ms**。

**P0-2 重置无效**：``capabilities.reset_cache()`` 只清 ``environment_report``
与 ``_codec_support``，**没清** ``tools`` 模块的 ``probe_7z`` / ``find_7z_gui``
/ ``probe_rar`` / ``probe_unrar_trial``。于是用户装了 7-Zip 后点「重新检测」，
``probe_7z`` 仍命中旧缓存 —— 按钮从上线起就没真正生效过。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    inst = QApplication.instance() or QApplication([])
    yield inst


# ---------------------------------------------------------------------------
# P0-2：reset_cache 必须清 tools 的 lru_cache
# ---------------------------------------------------------------------------


class TestResetCacheClearsTools:
    """``reset_cache()`` 必须让「重新检测」真正重探。"""

    def test_probe_7z_cache_is_cleared(self):
        from backupstudio import tools
        from backupstudio.capabilities import reset_cache

        tools.probe_7z()
        assert tools.probe_7z.cache_info().currsize > 0, "前置条件：应已有缓存条目"

        reset_cache()

        assert tools.probe_7z.cache_info().currsize == 0, (
            "reset_cache 没有清 probe_7z：用户装了 7-Zip 后点「重新检测」仍会看到旧结果"
        )

    def test_all_tools_caches_cleared(self):
        """四个带 @lru_cache 的函数都要清，漏一个就有一条路径仍是旧的。"""
        from backupstudio import tools
        from backupstudio.capabilities import reset_cache

        for fn in (
            tools.find_7z_gui,
            tools.probe_7z,
            tools.probe_rar,
            tools.probe_unrar_trial,
        ):
            fn()  # 制造缓存条目（结果无所谓，重点是 cache_info）

        reset_cache()

        for fn in (
            tools.find_7z_gui,
            tools.probe_7z,
            tools.probe_rar,
            tools.probe_unrar_trial,
        ):
            assert fn.cache_info().currsize == 0, f"{fn.__name__} 的缓存未被清空"

    def test_reset_tools_cache_is_idempotent(self):
        from backupstudio.tools import reset_tools_cache

        reset_tools_cache()
        reset_tools_cache()  # 空缓存上再清一次不得抛异常


# ---------------------------------------------------------------------------
# P0-1：环境页切页不得阻塞
# ---------------------------------------------------------------------------


class TestEnvironmentPageNonBlocking:
    """环境页探测必须在后台线程，``on_shown`` 立即返回。"""

    def _page(self, app):  # noqa: ANN001
        from backupstudio.gui.appstate import AppState
        from backupstudio.gui.environment_page import EnvironmentPage
        from backupstudio.store import TaskStore

        import tempfile

        store = TaskStore(Path(tempfile.mkdtemp()) / "tasks.json")
        state = AppState(store)
        return EnvironmentPage(state, window=None)

    def test_on_shown_returns_immediately(self, app, tmp_path):
        """首次 ``on_shown`` 不得在 UI 线程里跑探测。

        断言方式：调用后 ``_probe_busy`` 立即为 True（说明丢给了后台线程），
        且耗时远小于实测的同步探测时间（~750 ms）。
        """
        page = self._page(app)
        t = time.perf_counter()
        page.on_shown()
        elapsed_ms = (time.perf_counter() - t) * 1000

        assert elapsed_ms < 100, (
            f"on_shown 阻塞了 {elapsed_ms:.0f} ms —— 探测很可能又回到 UI 线程了"
        )
        assert page._probe_busy is True, "应已把探测丢给后台线程"

    def test_second_on_shown_reuses_cached_result(self, app):
        """已有结果时再切回来不得重扫。

        注意：构造函数末尾会发起一次首次探测，所以要先把状态摆成"已完成"，
        再断言第二次 ``on_shown`` 不再发起新探测。
        """
        page = self._page(app)
        page._probe_busy = False  # 模拟首次探测已完成
        page._caps = {"zip": {"available": True}}

        t = time.perf_counter()
        page.on_shown()
        elapsed_ms = (time.perf_counter() - t) * 1000

        assert elapsed_ms < 20, f"已有结果却仍耗时 {elapsed_ms:.0f} ms"
        assert page._probe_busy is False, "有缓存时不该发起新探测"

    def test_probe_result_is_applied(self, app):
        """后台线程的结果必须真的渲染进控件（不能只跑不显示）。"""
        page = self._page(app)
        page.rescan(force=False)

        deadline = time.time() + 30
        while page._probe_busy and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert page._probe_busy is False, "探测未在 30s 内完成"
        assert page._caps, "探测完成后 _caps 仍是空的 —— 结果没回填"
        assert page.fmt_table.rowCount() > 0, "格式矩阵没有渲染出行"

    def test_concurrent_rescan_is_deduplicated(self, app):
        """探测进行中再次调用不得并发第二个线程。"""
        page = self._page(app)
        page.rescan(force=False)
        assert page._probe_busy is True
        page.rescan(force=False)  # 应被 _probe_busy 挡掉
        # 不崩溃、不重复启动即可；等它自然结束
        deadline = time.time() + 30
        while page._probe_busy and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert page._probe_busy is False


# ---------------------------------------------------------------------------
# P0-1：历史页数据不变时不重建表格
# ---------------------------------------------------------------------------


class TestHistoryPageSkipsRebuild:
    def test_identical_data_skips_table_rebuild(self, app, monkeypatch):
        """数据指纹相同时 ``reload()`` 不得重建表格。

        实测：重建 300 行要 60–250 ms；用户来回切页时数据通常没变。
        """
        import tempfile

        import backupstudio.engine as engine
        from backupstudio.gui.appstate import AppState
        from backupstudio.gui.history_page import HistoryPage
        from backupstudio.store import TaskStore

        tmp = Path(tempfile.mkdtemp())
        hist = tmp / "history.jsonl"
        hist.write_text(
            "\n".join(
                f'{{"task_name":"t{i}","status":"success","finished_at":"2026-10-29T01:00:{i:02d}"}}'
                for i in range(10)
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(engine, "history_path", lambda: hist)

        store = TaskStore(tmp / "tasks.json")
        state = AppState(store)
        page = HistoryPage(state, window=None)

        page.reload()
        first = page.table.rowCount()
        assert first == 10

        # 用类级计数包装：monkeypatch 在实例属性上对 C++ 侧不可靠，
        # 这里直接记录 rowCount 变化与耗时来判断"有没有重建"。
        before_items = [page.table.item(0, 0).text() for _ in range(1)]
        t = time.perf_counter()
        page.reload()
        elapsed_ms = (time.perf_counter() - t) * 1000

        assert elapsed_ms < 20, f"重复 reload 耗时 {elapsed_ms:.0f} ms，应近乎为 0（未走缓存重建）"
        assert page.table.rowCount() == 10
        assert [page.table.item(0, 0).text() for _ in range(1)] == before_items, "内容不应改变"

    def test_new_history_entry_triggers_rebuild(self, app, monkeypatch):
        """历史变化后必须重建，否则用户看不到新的备份记录。"""
        import tempfile

        import backupstudio.engine as engine
        from backupstudio.gui.appstate import AppState
        from backupstudio.gui.history_page import HistoryPage
        from backupstudio.store import TaskStore

        tmp = Path(tempfile.mkdtemp())
        hist = tmp / "history.jsonl"
        hist.write_text('{"task_name":"a","status":"success","finished_at":"2026-10-29T01:00:00"}\n', encoding="utf-8")
        monkeypatch.setattr(engine, "history_path", lambda: hist)

        page = HistoryPage(AppState(TaskStore(tmp / "tasks.json")), window=None)
        page.reload()
        assert page.table.rowCount() == 1

        hist.write_text(
            hist.read_text(encoding="utf-8")
            + '{"task_name":"b","status":"success","finished_at":"2026-10-29T02:00:00"}\n',
            encoding="utf-8",
        )
        page.reload()
        assert page.table.rowCount() == 2, "新增历史后表格没更新"


# ---------------------------------------------------------------------------
# P0-1：历史读取按文件指纹记忆化
# ---------------------------------------------------------------------------


class TestHistoryMemoization:
    def test_repeated_read_hits_cache(self, app, monkeypatch):
        import tempfile

        import backupstudio.engine as engine
        from backupstudio.gui.appstate import AppState
        from backupstudio.store import TaskStore

        tmp = Path(tempfile.mkdtemp())
        hist = tmp / "history.jsonl"
        hist.write_text('{"task_name":"a","status":"success"}\n', encoding="utf-8")
        monkeypatch.setattr(engine, "history_path", lambda: hist)

        calls = {"n": 0}
        real = engine.read_history

        def counting(limit=500):  # noqa: ANN001
            calls["n"] += 1
            return real(limit)

        monkeypatch.setattr("backupstudio.gui.appstate.read_history", counting)

        state = AppState(TaskStore(tmp / "tasks.json"))
        state.history()
        state.history()
        state.history()
        assert calls["n"] == 1, f"文件未变却重复解析了 {calls['n']} 次"

    def test_file_change_invalidates_cache(self, app, monkeypatch):
        import tempfile

        import backupstudio.engine as engine
        from backupstudio.gui.appstate import AppState
        from backupstudio.store import TaskStore

        tmp = Path(tempfile.mkdtemp())
        hist = tmp / "history.jsonl"
        hist.write_text('{"task_name":"a","status":"success"}\n', encoding="utf-8")
        monkeypatch.setattr(engine, "history_path", lambda: hist)

        state = AppState(TaskStore(tmp / "tasks.json"))
        assert len(state.history()) == 1

        hist.write_text(
            '{"task_name":"a","status":"success"}\n{"task_name":"b","status":"success"}\n',
            encoding="utf-8",
        )
        assert len(state.history()) == 2, "文件变化后仍返回旧结果"
