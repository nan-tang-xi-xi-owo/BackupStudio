"""运行记录页：历史列表、过滤、导出 JSON / CSV。"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QLabel,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..util import human_duration, human_size
from .appstate import AppState
from .icons import line_icon
from .widgets import Card, Combo, muted, title_label

log = logging.getLogger("backupstudio.gui.history")

STATUS = {
    "success": ("成功", "ok"),
    "failure": ("失败", "bad"),
    # **1.1.2 修复**：同 tasks_page —— `engine.py` 会产出 `partial`，
    # 这里缺这个键时历史记录会显示成原始英文 `partial` 或回落成「未运行过」。
    "partial": ("部分完成", "warn"),
    "skipped": ("跳过", "warn"),
}
COLUMNS = ["时间", "任务", "状态", "格式", "文件数", "原始大小", "压缩后", "耗时", "校验", "压缩包"]


class HistoryPage(QWidget):
    def __init__(self, state: AppState, window) -> None:  # noqa: ANN001
        super().__init__()
        self.state = state
        self.window = window
        self._rows: list[dict] = []
        # 1.1.3 收纳：分页显示，避免一次建几千个单元格
        self._page_size = 100
        self._shown = 100

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(16)

        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(title_label("运行记录", 1))
        col.addWidget(muted("最近 500 次备份结果（保存在配置目录的 history.jsonl）"))
        head.addLayout(col, 1)
        self.filter_combo = Combo()
        self.filter_combo.addItem("全部任务", "")
        self.filter_combo.currentIndexChanged.connect(self.reload)
        head.addWidget(self.filter_combo)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setIcon(line_icon("history", "#64748B", 16))
        self.btn_refresh.clicked.connect(self.reload)
        head.addWidget(self.btn_refresh)
        btn_export = QPushButton("导出 CSV")
        btn_export.clicked.connect(self.export_csv)
        head.addWidget(btn_export)
        btn_export_json = QPushButton("导出 JSON")
        btn_export_json.clicked.connect(self.export_json)
        head.addWidget(btn_export_json)
        lay.addLayout(head)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        # **不要 ResizeToContents**：它对每列测量**所有行**取最大宽度，
        # 300 行 x 10 列 = 3000 次测量（实测历史页卡顿主因之一）。
        # 改固定列宽 + 末列拉伸 + 单元格中间省略。
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for _i, _w in enumerate([130, 140, 70, 80, 70, 90, 90, 80, 70, 240]):
            if _i < self.table.columnCount():
                self.table.setColumnWidth(_i, _w)
        hh.setStretchLastSection(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setWordWrap(False)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.itemDoubleClicked.connect(lambda _: self._open_selected())
        lay.addWidget(self.table, 3)

        # **收纳**（1.1.3）：默认只渲染最近 100 条，底部提供「显示更多」，
        # 避免 4651 条历史一次全建（那是打开就卡的主因）。
        more = QWidget()
        ml = QHBoxLayout(more)
        ml.setContentsMargins(0, 2, 0, 2)
        self.page_info = QLabel("")
        self.page_info.setObjectName("Muted")
        ml.addWidget(self.page_info)
        ml.addStretch(1)
        self.btn_more = QPushButton("显示更多")
        self.btn_more.clicked.connect(self._show_more)
        ml.addWidget(self.btn_more)
        self.btn_all = QPushButton("全部显示")
        self.btn_all.setToolTip("记录很多时可能较慢")
        self.btn_all.clicked.connect(self._show_all)
        ml.addWidget(self.btn_all)
        lay.addWidget(more)

        detail_card = Card()
        detail_card.add(title_label("详情", 3))
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选择一条记录查看详情")
        self.detail.setStyleSheet("font-family: 'Cascadia Mono','Consolas','Menlo',monospace; font-size: 12px;")
        detail_card.add(self.detail)
        lay.addWidget(detail_card, 2)

        state.tasks_changed.connect(self._rebuild_filter)
        state.task_updated.connect(lambda _: None)

    def _rebuild_filter(self) -> None:
        current = self.filter_combo.currentData()
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItem("全部任务", "")
        for t in self.state.tasks():
            self.filter_combo.addItem(t.name, t.id)
        self.filter_combo.select_data(current)
        self.filter_combo.blockSignals(False)

    def _dark(self) -> bool:
        return self.state.palette.dark

    def reload(self) -> None:
        self._rebuild_filter()
        rows = self.state.history()
        task_id = self.filter_combo.currentData()
        if task_id:
            rows = [r for r in rows if r.get("task_id") == task_id]
        rows.reverse()
        self._rows = rows
        self._update_page_info(len(rows))
        # **收纳**：只渲染前 `_shown` 条（默认 100）。4651 条全渲染会卡死。
        view = rows[: self._shown]
        self.table.setRowCount(len(view))
        # 1.1.2：状态色改用主题色（原来写死 #16A34A/#DC2626/#D97706，
        # 主题切换不跟随；且缺 partial）
        from . import DARK as _D
        from . import LIGHT as _L

        _pal = _D if getattr(self, "_dark", False) else _L
        colors = {
            "success": QColor(_pal.success),
            "failure": QColor(_pal.danger),
            "partial": QColor(_pal.warning),
            "skipped": QColor(_pal.warning),
        }
        for i, r in enumerate(view):
            ts = str(r.get("finished_at") or r.get("started_at") or "")
            try:
                pretty = datetime.fromisoformat(ts).strftime("%m-%d %H:%M:%S")
            except ValueError:
                pretty = ts
            status, _ = STATUS.get(r.get("status", ""), (r.get("status", ""), "muted"))
            verify = {"passed": "通过", "failed": "失败", "skipped": "未校验"}.get(r.get("verification", ""), "")
            values = [
                pretty,
                r.get("task_name", ""),
                status,
                r.get("format", ""),
                str(r.get("files", 0)),
                human_size(r.get("source_bytes") or 0),
                human_size(r.get("archive_bytes") or 0),
                f"{float(r.get('duration') or 0):.1f}s",
                verify,
                Path(r.get("archive", "")).name,
            ]
            for c, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setToolTip(str(r.get("archive", "")) if c == len(values) - 1 else val)
                if c == 2:
                    item.setForeground(colors.get(r.get("status", ""), QColor("#6B7280")))
                self.table.setItem(i, c, item)
        if rows:
            self.table.selectRow(0)
        else:
            self.detail.setPlainText("没有记录。")

    def _selected(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        idx = rows[0].row()
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    # ------------------------------------------------------------------
    # 收纳：分页显示（1.1.3）
    # ------------------------------------------------------------------
    def _show_more(self) -> None:
        self._shown += 200
        self.reload()

    def _show_all(self) -> None:
        self._shown = max(len(self._rows), 1)
        self.reload()

    def _update_page_info(self, total: int) -> None:
        if not hasattr(self, "page_info"):
            return
        self.page_info.setText(f"共 {total} 条，已显示 {min(self._shown, total)} 条")
        self.btn_more.setVisible(total > self._shown)
        self.btn_all.setVisible(total > self._shown)

    def _show_detail(self) -> None:
        r = self._selected()
        if r is None:
            return
        # 1.1.2：枚举值翻成中文 —— 原来直接把 `success` / `manual` / `passed`
        # 这些内部英文码显示给用户（审计实测 15 处技术术语泄漏）
        st_txt = STATUS.get(str(r.get("status") or ""), (str(r.get("status") or "—"), ""))[0]
        trig = {
            "manual": "手动", "schedule": "定时", "daemon": "守护进程",
            "cli": "命令行", "tray": "托盘菜单", "": "—",
        }.get(str(r.get("trigger") or ""), str(r.get("trigger") or "—"))
        ver = {"passed": "通过", "failed": "未通过", "skipped": "未校验", "": "—"}.get(
            str(r.get("verification") or ""), str(r.get("verification") or "—")
        )
        lines = [
            f"任务      : {r.get('task_name')}",
            f"状态      : {st_txt}",
            f"开始      : {r.get('started_at')}",
            f"结束      : {r.get('finished_at')}",
            f"触发方式  : {trig}",
            f"格式      : {r.get('format')}",
            f"分卷      : {r.get('multivolume') or '无'}",
            f"加密      : {'是' if r.get('encrypted') else '否'} {r.get('encrypt_mode') or ''}",
            f"文件数    : {r.get('files')}  目录数: {r.get('dirs')}  跳过: {r.get('skipped')}",
            f"原始大小  : {human_size(r.get('source_bytes') or 0)}",
            f"压缩后    : {human_size(r.get('archive_bytes') or 0)}",
            f"耗时      : {human_duration(r.get('duration') or 0)}",
            f"校验      : {ver}  {r.get('verification_message') or ''}",
            f"压缩包    : {r.get('archive')}",
        ]
        if r.get("volumes") and isinstance(r.get("volumes"), int):
            lines.append(f"分卷数量  : {r['volumes']}")
        warnings = r.get("warnings") or []
        if warnings:
            lines.append("\n提示：")
            lines += [f"  • {w}" for w in warnings]
        errors = r.get("errors") or []
        if errors:
            lines.append("\n错误：")
            lines += [f"  • {e}" for e in errors]
        if r.get("cleaned"):
            lines.append("\n已清理：")
            lines += [f"  • {c}" for c in r["cleaned"]]
        sample = r.get("extracted_text") or ""
        if sample:
            lines.append("\n校验时读取到的内容（证明可还原）：")
            lines.append(sample)
        self.detail.setPlainText("\n".join(lines))

    def _open_selected(self) -> None:
        r = self._selected()
        if not r or not r.get("archive"):
            return
        from ..util import reveal_file

        if not reveal_file(r["archive"]):
            QMessageBox.information(self, "提示", "文件可能已被移动或删除。")

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", str(Path.home() / "backup-history.csv"), "CSV (*.csv)")
        if not path:
            return
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(COLUMNS)
        for r in self._rows:
            status, _ = STATUS.get(r.get("status", ""), (r.get("status", ""), ""))
            verify = {"passed": "通过", "failed": "失败", "skipped": "未校验"}.get(r.get("verification", ""), "")
            writer.writerow([
                r.get("finished_at") or r.get("started_at"), r.get("task_name"), status, r.get("format"),
                r.get("files"), human_size(r.get("source_bytes") or 0), human_size(r.get("archive_bytes") or 0),
                f"{float(r.get('duration') or 0):.1f}s", verify, r.get("archive"),
            ])
        try:
            Path(path).write_text(buf.getvalue(), encoding="utf-8-sig")
            QMessageBox.information(self, "导出成功", f"已导出到：\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出 JSON", str(Path.home() / "backup-history.json"), "JSON (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._rows, ensure_ascii=False, indent=2), encoding="utf-8")
            QMessageBox.information(self, "导出成功", f"已导出到：\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def set_palette_mode(self, dark: bool) -> None:
        return

    def on_shown(self) -> None:
        self.reload()
