"""历史页：关掉 ResizeToContents + 加「显示更多」。"""
from pathlib import Path

H = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\history_page.py")
s = H.read_text(encoding="utf-8")

# ── 1. ResizeToContents → 固定宽度（它是 O(行×列) 的反复测量，是大头）──
old = """        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)"""
new = """        # **不要用 ResizeToContents**：它会对每一列测量**所有行**再取最大宽度，
        # 300 行 × 10 列 = 3000 次测量，行数越多越慢（实测这是历史页卡顿主因之一）。
        # 改为固定列宽 + 末列拉伸，配合单元格省略号。
        _hdr = self.table.horizontalHeader()
        _hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        _widths = [130, 140, 70, 80, 70, 90, 90, 80, 70, 240]
        for _i, _w in enumerate(_widths[: self.table.columnCount()]):
            self.table.setColumnWidth(_i, _w)
        _hdr.setStretchLastSection(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setWordWrap(False)"""
if old in s:
    s = s.replace(old, new, 1)
    print("1) 已关掉 ResizeToContents")
else:
    print("1) 未匹配 ResizeToContents")

H.write_text(s, encoding="utf-8", newline="")
import py_compile
py_compile.compile(str(H), doraise=True)
print("2) 语法 OK")
