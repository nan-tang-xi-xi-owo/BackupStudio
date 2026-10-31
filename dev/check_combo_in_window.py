"""从整窗截图里裁剪下拉框区域，看它实际显示成什么色（而不是 grab 单控件）。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="wc-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)
OUT = R / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui import stylesheet
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
    app.setStyleSheet(stylesheet(st.palette))
    w._select("settings")
    for _ in range(14):
        app.processEvents()
    sp = w._pages["settings"]
    cb = sp.theme_combo
    win_img = w.grab().toImage()
    # 控件在窗口内的坐标
    tl = cb.mapTo(w, QPoint(0, 0))
    x = tl.x() + cb.width() // 2
    y = tl.y() + cb.height() // 2
    c = win_img.pixelColor(x, y)
    left = win_img.pixelColor(tl.x() + 1, y)
    print(f"  {style:<6} 下拉框在窗口内 ({tl.x()},{tl.y()}) "
          f"中心={c.name()} 左边缘={left.name()}  "
          f"{'有边框' if left.name() != c.name() else '无边框'}")
    # 裁一块看
    crop = win_img.copy(tl.x() - 4, tl.y() - 4, cb.width() + 8, cb.height() + 8)
    p = OUT / f"combo-{style}.png"
    crop.save(str(p))
    print(f"         裁剪图 {p.name} {p.stat().st_size} B")
w.close()
