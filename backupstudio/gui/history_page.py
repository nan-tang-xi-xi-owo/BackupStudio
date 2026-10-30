"""运行记录页：历史列表、过滤、导出 JSON / CSV。"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QSizePolicy,
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

STATUS = {"success": ("成功", "ok"), "failure": ("失败", "bad"), "skipped": ("跳过", "warn")}
COLUMNS = ["时间", "任务", "状态", "格式", "文件数", "原始大小", "压缩后", "耗时", "校验", "压缩包"]

#: 各列初始宽度。刻意给得比"内容自然宽度"窄，让整表能落在可用宽度内；
#: 过长的内容由单元格省略号 + tooltip 兜住，用户可以拖动列宽。
#: 合计约 700px，低于 980px 窗口下的可用宽度（~740），避免默认就出横向滚动条。
_COL_WIDTHS = (96, 96, 56, 50, 50, 68, 68, 50, 54, 100)


class HistoryPage(QWidget):
    def __init__(self, state: AppState, window) -> None:  # noqa: ANN001
        super().__init__()
        self.state = state
        self.window = window
        self._rows: list[dict] = []
        #: 上次渲染时的数据指纹；相同则跳过重建（切页零成本）
        self._rendered_fp: tuple | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(16)

        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        t = title_label("运行记录", 1)
        m = muted("最近 500 次备份结果（保存在配置目录的 history.jsonl）")
        # 标题/副标题在窄窗口下要能被压缩，否则标题行（标题+下拉+三个按钮）
        # 会把整页最小宽度顶到 835px，超过可用的 764px → 整页横向滚动。
        for lbl in (t, m):
            lbl.setMinimumWidth(0)
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(t)
        col.addWidget(m)
        head.addLayout(col, 1)
        self.filter_combo = Combo()
        self.filter_combo.setMinimumWidth(0)
        self.filter_combo.setMaximumWidth(220)
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
        # 原来整表 ResizeToContents：10 列内容相加把历史页最小宽度顶到 835px，
        # 超过窗口的 980-216 可用宽度 → 整页横向滚动（其它页也被它连累，
        # 主窗口最小宽度被抬到 1051px）。
        # 改为：默认按内容但允许压缩，最后一列吃掉剩余空间；超长文件名由
        # item 的省略号处理（QStyle 默认对文本做 ElideRight）。
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        hh.setMinimumSectionSize(48)
        for i in range(self.table.columnCount()):
            self.table.setColumnWidth(i, _COL_WIDTHS[i] if i < len(_COL_WIDTHS) else 90)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.itemDoubleClicked.connect(lambda _: self._open_selected())
        # 允许整页被压窄：QTableWidget 的 minimumSizeHint 会随列数累积，
        # 集成到主窗口后把页面最小宽度推到 835px（窗口只有 980-216=764 可用），
        # 于是整页出现横向滚动。这里显式声明"可以压缩"，横向滚动留给表格自己处理。
        self.table.setMinimumWidth(0)
        self.table.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding
        )
        lay.addWidget(self.table, 3)

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
        # 必须用切片反转：state.history() 在缓存命中时返回的是**同一个 list 对象**，
        # 原地 rows.reverse() 会把它翻回去，下次再反一次 —— 顺序来回抖。
        rows = rows[::-1]
        # 数据没变就不重建表格。实测每次重建 300 行 = 3000 个 QTableWidgetItem，
        # 光 processEvents 里重绘就要 300 ms —— 而用户来回切页时数据通常一字未动。
        # 用 (条数, 首条与末条的指纹) 做廉价比较，避免对 300 个 dict 做深比较。
        fingerprint = (
            len(rows),
            str(rows[0].get("finished_at")) if rows else "",
            str(rows[-1].get("finished_at")) if rows else "",
            task_id,
        )
        if fingerprint == self._rendered_fp and self.table.rowCount() == len(rows):
            self._rows = rows
            return
        self._rendered_fp = fingerprint
        self._rows = rows
        # 批量填充期间关掉重绘，只为避免布局抖动
        self.table.setUpdatesEnabled(False)
        try:
            self._fill_table(rows)
        finally:
            self.table.setUpdatesEnabled(True)
        if not rows:
            # 空状态：表格与详情都留大片空白会让用户以为"界面坏了"。
            # 这里给出可行动的说明，而不是一个空白框。
            empty = (
                "还没有备份记录。\n\n"
                "运行一次备份任务后，这里会列出每次的结果（时间、状态、体积、校验）。\n"
                "记录保存在配置目录的 history.jsonl 中，最近 500 条。"
            )
            self.detail.setPlainText(empty)

    def _fill_table(self, rows: list[dict]) -> None:
        self.table.setRowCount(len(rows))
        # 颜色取自当前主题 Palette（不要在这里写死色值 —— 会让状态色与
        # 主题脱节：改主题时表格里的绿/红不跟着变，且容易与调色板不一致）
        palette = self.state.palette
        colors = {
            "success": QColor(palette.success),
            "failure": QColor(palette.danger),
            "skipped": QColor(palette.warning),
        }
        default_color = QColor(palette.text_dim)
        for i, r in enumerate(rows):
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
                # 只给"归档文件名"列挂 tooltip（放全文路径，单元格里显示的是 basename）。
                # 实测：每列都 setToolTip 时 300 行要 80 ms，仅 1 列降到 63 ms；
                # 其余列的值本身就在单元格里完整可见，tooltip 是冗余的。
                if c == len(values) - 1:
                    item.setToolTip(str(r.get("archive", "")))
                if c == 2:
                    item.setForeground(colors.get(r.get("status", ""), default_color))
                self.table.setItem(i, c, item)

    def _selected(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        idx = rows[0].row()
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    def _show_detail(self) -> None:
        r = self._selected()
        if r is None:
            return
        lines = [
            f"任务      : {r.get('task_name')}  ({r.get('task_id')})",
            f"状态      : {r.get('status')}",
            f"开始      : {r.get('started_at')}",
            f"结束      : {r.get('finished_at')}",
            f"触发方式  : {r.get('trigger')}",
            f"格式      : {r.get('format')}  后端: {r.get('backend')}",
            f"分卷      : {r.get('multivolume') or '无'}",
            f"加密      : {'是' if r.get('encrypted') else '否'} {r.get('encrypt_mode') or ''}",
            f"文件数    : {r.get('files')}  目录数: {r.get('dirs')}  跳过: {r.get('skipped')}",
            f"原始大小  : {human_size(r.get('source_bytes') or 0)}",
            f"压缩后    : {human_size(r.get('archive_bytes') or 0)}",
            f"耗时      : {human_duration(r.get('duration') or 0)}",
            f"校验      : {r.get('verification')}  {r.get('verification_message') or ''}",
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
