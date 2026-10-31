"""实测自绘标题栏：截图各风格下的实际像素颜色。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="tb-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)
OUT = R / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication
app = QApplication([])
from backupstudio.store import TaskStore
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui import stylesheet

st = AppState(TaskStore(CFG / "tasks.json"))
for key in ("win", "98", "mag", "glass"):
    st.set_style(key)
    app.setStyleSheet(stylesheet(st.palette))
    w = MainWindow(st); w.resize(1000, 660); w.show()
    for _ in range(12):
        app.processEvents()

    tb = w.titlebar
    print(f"\n  === 风格 {key} ===")
    print(f"    frameless={w._frameless}  titlebar={'有' if tb else '无'}")
    if tb is None:
        w.close(); continue
    # 取标题栏中点像素（应为 surface 色）
    img = tb.grab().toImage()
    px = img.pixelColor(200, img.height() // 2)
    sep = img.pixelColor(img.width() // 2, img.height() - 1)
    print(f"    标题栏底色 = {px.name()}  (期望 surface={st.palette.surface})")
    print(f"    底部分隔线 = {sep.name()}  (期望 border={st.palette.border})")
    print(f"    按钮文字色 = {tb.btn_close.palette().color(tb.btn_close.foregroundRole()).name()}")
    p = OUT / f"titlebar-{key}.png"
    w.grab().save(str(p))
    print(f"    整窗截图 {p.name} {p.stat().st_size} B")
    w.close()
