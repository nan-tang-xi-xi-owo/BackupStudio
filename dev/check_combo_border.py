"""对照：设置页下拉框在 win 与 glass 风格下的边框像素。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="cb-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
st = AppState(ts)
w = MainWindow(st)
w.resize(1100, 700)
w.show()
for _ in range(10):
    app.processEvents()

for style in ("win", "glass"):
    st.set_style(style)
    app.setStyleSheet(__import__("backupstudio.gui", fromlist=["stylesheet"]).stylesheet(st.palette))
    w._select("settings")
    for _ in range(12):
        app.processEvents()
    sp = w._pages["settings"]
    cb = sp.theme_combo
    img = cb.grab().toImage()
    # 采样边框：左边缘竖线中点、上边缘横线中点
    left_px = img.pixelColor(0, img.height() // 2).name()
    top_px = img.pixelColor(img.width() // 2, 0).name()
    center = img.pixelColor(img.width() // 2, img.height() // 2).name()
    print(f"  {style:<6} 下拉框 {cb.width()}x{cb.height()}  "
          f"左边缘={left_px} 上边缘={top_px} 中心={center}")
    # 判断是否有可见边框（边缘色与中心色不同 = 有边框）
    has_border = left_px != center or top_px != center
    print(f"         有可见边框: {has_border}")
w.close()
