"""用鲜艳色标记法确认 combo 在整窗里的真实位置与是否被覆盖。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="mk-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)
OUT = R / "docs" / "screenshots"

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui import stylesheet
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
ts.settings.ui_style = "glass"
ts.save()
st = AppState(ts)
app.setStyleSheet(stylesheet(st.palette))
w = MainWindow(st)
w.resize(1100, 700)
w.show()
for _ in range(12):
    app.processEvents()
w._select("settings")
for _ in range(16):
    app.processEvents()

sp = w._pages["settings"]
cb = sp.theme_combo

# 用鲜艳洋红标记 combo，看整窗里该位置是否变洋红
cb.setStyleSheet("QComboBox { background: #FF00FF; border: 3px solid #00FF00; }")
for _ in range(10):
    app.processEvents()

win = w.grab().toImage()
tl = cb.mapTo(w, QPoint(0, 0))
# 扫描整窗找洋红像素，定位真实位置
hits = []
for y in range(0, win.height(), 4):
    for x in range(0, win.width(), 4):
        c = win.pixelColor(x, y)
        if c.red() > 200 and c.blue() > 200 and c.green() < 80:
            hits.append((x, y))
print(f"  洋红像素命中 {len(hits)} 个（抽样）")
if hits:
    xs = [h[0] for h in hits]
    ys = [h[1] for h in hits]
    print(f"    范围 x={min(xs)}..{max(xs)}  y={min(ys)}..{max(ys)}")
print(f"  cb.mapTo(w, 0,0) = ({tl.x()},{tl.y()})  尺寸 {cb.width()}x{cb.height()}")
print(f"    预期范围 x={tl.x()}..{tl.x()+cb.width()}  y={tl.y()}..{tl.y()+cb.height()}")

# 裁一块保存
crop = win.copy(max(0, tl.x() - 10), max(0, tl.y() - 10),
                min(cb.width() + 20, win.width() - tl.x()),
                min(cb.height() + 20, win.height() - tl.y()))
crop.scaled(crop.width() * 2, crop.height() * 2).save(str(OUT / "combo-marker.png"))
print(f"  已存 combo-marker.png")
w.close()
