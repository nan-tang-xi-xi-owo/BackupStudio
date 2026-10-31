"""复现"有时候下拉弹层往下滑"：滚动到不同位置再开弹层。

重点怀疑：
1. 设置页在 QScrollArea 里，滚动后 combo 的全局坐标变了，但弹层用的是
   缓存的旧位置
2. NCCALCSIZE 归零后客户区 = 整窗，Qt 计算弹层位置时可能用了不含标题栏的
   坐标系
3. 自动化测试里坐标是对的，但真实点击（鼠标事件）路径不同
"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="sc-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui import stylesheet
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
st = AppState(ts)
app.setStyleSheet(stylesheet(st.palette))
w = MainWindow(st)
w.resize(1000, 680)
w.move(150, 100)
w.show()
for _ in range(14):
    app.processEvents()
w._select("settings")
for _ in range(16):
    app.processEvents()

sp = w._pages["settings"]
# 找设置页的 QScrollArea
from PySide6.QtWidgets import QScrollArea

sa = sp.findChild(QScrollArea)
print(f"  找到 QScrollArea: {sa is not None}")
if sa:
    bar = sa.verticalScrollBar()
    print(f"  滚动范围 0..{bar.maximum()}  当前 {bar.value()}")
    print(f"  viewport 高度 {sa.viewport().height()}")

for target in (0, 80, 160, 300, bar.maximum() if sa else 0):
    if sa:
        sa.verticalScrollBar().setValue(target)
        for _ in range(8):
            app.processEvents()
    cb = sp.style_combo if target == 0 else sp.tonal_combo
    g = cb.mapToGlobal(QPoint(0, 0))
    cb.showPopup()
    for _ in range(10):
        app.processEvents()
    popup = cb.view().window()
    pg = popup.geometry()
    expect = g.y() + cb.height()
    delta = pg.y() - expect
    flag = "⚠ 往下偏" if delta > 4 else ("⚠ 往上偏" if delta < -4 else "OK")
    print(f"  scroll={target:>4}  combo global y={g.y():>4}  "
          f"弹层 y={pg.y():>4}  期望={expect:>4}  偏移={delta:+d}  {flag}")
    cb.hidePopup()
    for _ in range(6):
        app.processEvents()

w.close()
