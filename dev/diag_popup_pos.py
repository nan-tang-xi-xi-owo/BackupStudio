"""诊断：下拉弹层位置为什么往下滑。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="pos-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import QPoint
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

app = QApplication([])
import ctypes
import ctypes.wintypes as wt

from backupstudio.gui import stylesheet
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
st = AppState(ts)
app.setStyleSheet(stylesheet(st.palette))
w = MainWindow(st)
w.resize(1000, 680)
w.move(200, 120)
w.show()
for _ in range(14):
    app.processEvents()
w._select("settings")
for _ in range(16):
    app.processEvents()

sp = w._pages["settings"]
cb = sp.style_combo
hwnd = int(w.winId())

user32 = ctypes.windll.user32

print(f"  frameless = {w._frameless}   _chrome = {getattr(w, '_chrome', None) is not None}")

r = wt.RECT()
user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
print(f"\n  窗口物理矩形   : ({r.left},{r.top}) - ({r.right},{r.bottom})")
print(f"  窗口逻辑几何   : {w.geometry().x()},{w.geometry().y()} {w.width()}x{w.height()}")
print(f"  frameGeometry  : {w.frameGeometry().x()},{w.frameGeometry().y()} "
      f"{w.frameGeometry().width()}x{w.frameGeometry().height()}")
dpr = w.devicePixelRatio()
print(f"  dpr = {dpr}")

# combo 的全局位置
g = cb.mapToGlobal(QPoint(0, 0))
print(f"\n  combo mapToGlobal = ({g.x()},{g.y()})  尺寸 {cb.width()}x{cb.height()}")
print(f"  combo 应显示在窗口内 y = {g.y() - w.geometry().y()}")

# 打开弹层，看它被放到了哪里
cb.showPopup()
for _ in range(12):
    app.processEvents()
popup = cb.view().window()
pg = popup.geometry()
print(f"\n  弹层几何       : {pg.x()},{pg.y()} {pg.width()}x{pg.height()}")
print(f"  弹层 mapToGlobal: ({popup.mapToGlobal(QPoint(0,0)).x()},"
      f"{popup.mapToGlobal(QPoint(0,0)).y()})")
print(f"  期望紧贴 combo 下方 y ≈ {g.y() + cb.height()}")
print(f"  实际弹层 top y   = {pg.y()}")
delta = pg.y() - (g.y() + cb.height())
print(f"  **偏移量 = {delta:+d} px**  {'⚠ 往下偏' if delta > 4 else ('OK' if abs(delta) <= 4 else '往上偏')}")
cb.hidePopup()
w.close()
