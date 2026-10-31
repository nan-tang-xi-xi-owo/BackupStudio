"""实测：打开下拉弹层，检查其背景像素是否正常（不是黑块）。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="pp-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)
OUT = R / "docs" / "screenshots"

from PySide6.QtGui import QGuiApplication
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

for style in ("win", "glass", "98", "mag"):
    st.set_style(style)
    app.setStyleSheet(stylesheet(st.palette))
    w._select("settings")
    for _ in range(14):
        app.processEvents()

    sp = w._pages["settings"]
    cb = sp.style_combo
    cb.showPopup()
    for _ in range(14):
        app.processEvents()

    v = cb.view()
    popup = v.window()  # 弹层顶层窗口
    print(f"\n  === {style} ===")
    print(f"    弹层类名 = {type(popup).__name__}")
    print(f"    弹层可见 = {popup.isVisible()}  尺寸 {popup.width()}x{popup.height()}")

    # 抓弹层自身
    img = popup.grab().toImage()
    if not img.isNull():
        # 取列表项区域中点（避开顶部边距）
        cx, cy = img.width() // 2, max(20, img.height() // 2)
        c = img.pixelColor(cx, cy)
        corner = img.pixelColor(4, img.height() - 4)
        print(f"    弹层中心像素 = {c.name()}  底部角 = {corner.name()}")
        dark = sum(1 for y in range(0, img.height(), 6) for x in range(0, img.width(), 6)
                   if img.pixelColor(x, y).lightness() < 40)
        total = len(range(0, img.height(), 6)) * len(range(0, img.width(), 6))
        print(f"    暗像素占比 = {dark * 100.0 / max(total,1):.1f}%  "
              f"{'!! 黑块' if dark * 100.0 / max(total,1) > 30 else 'OK'}")
        p = OUT / f"popup-{style}.png"
        img.save(str(p))
    cb.hidePopup()
    for _ in range(6):
        app.processEvents()
w.close()
