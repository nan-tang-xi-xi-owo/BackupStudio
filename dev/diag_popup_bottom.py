"""排查"下拉往下滑"：模拟 combo 靠近屏幕底部时的行为。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="fl-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import QPoint
from PySide6.QtGui import QGuiApplication
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
scr = QGuiApplication.primaryScreen().availableGeometry()
print(f"  屏幕可用区 {scr.width()}x{scr.height()}  逻辑")

# 把窗口放到靠近屏幕底部的位置，让 combo 靠底
w.move(100, max(0, scr.height() - 700))
w.show()
for _ in range(12):
    app.processEvents()
w._select("settings")
for _ in range(16):
    app.processEvents()

sp = w._pages["settings"]
from PySide6.QtWidgets import QScrollArea

sa = sp.findChild(QScrollArea)
if sa:
    sa.verticalScrollBar().setValue(0)
    for _ in range(6):
        app.processEvents()

for cb, name in ((sp.theme_combo, "主题"), (sp.style_combo, "风格"),
                 (sp.tonal_combo, "配色风格")):
    g = cb.mapToGlobal(QPoint(0, 0))
    cb.showPopup()
    for _ in range(10):
        app.processEvents()
    popup = cb.view().window()
    pg = popup.geometry()
    below = scr.height() - (g.y() + cb.height())
    above = g.y()
    expect_below = g.y() + cb.height()
    delta = pg.y() - expect_below
    print(f"\n  {name} (global y={g.y()}, 下方剩余={below}px, 弹层高={pg.height()}px)")
    print(f"    弹层 y={pg.y()}  期望(下方)={expect_below}  偏移={delta:+d}")
    if delta < -10:
        print(f"    → 向上翻转（下方空间不足，正常行为），翻转量 {abs(delta)}px")
    elif delta > 10:
        print(f"    → **异常往下偏 {delta}px**")
    else:
        print("    → 正常（紧贴下方）")
    cb.hidePopup()
    for _ in range(6):
        app.processEvents()
w.close()
