"""验证：滚动后下拉框是否落到标题栏高度内，导致点击被当拖动截走。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="tb2-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QScrollArea

app = QApplication([])
from backupstudio.gui import stylesheet
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui.titlebar import TITLE_H
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
st = AppState(ts)
app.setStyleSheet(stylesheet(st.palette))
w = MainWindow(st)
w.resize(1000, 680)
w.show()
for _ in range(12):
    app.processEvents()
w._select("settings")
for _ in range(16):
    app.processEvents()

sp = w._pages["settings"]
sa = sp.findChild(QScrollArea)
bar = sa.verticalScrollBar() if sa else None
print(f"  标题栏高度 TITLE_H = {TITLE_H}")
print(f"  滚动范围 0..{bar.maximum() if bar else 0}")

for scroll in (0, 200, 400, 600, 810):
    if bar:
        bar.setValue(scroll)
        for _ in range(8):
            app.processEvents()
    hits = []
    for name in ("theme_combo", "style_combo", "tonal_combo", "seed_edit", "btn_pick_seed"):
        wdg = getattr(sp, name, None)
        if wdg is None:
            continue
        p = wdg.mapTo(w, QPoint(0, 0))
        in_title = p.y() <= TITLE_H
        hits.append((name, p.y(), in_title))
    danger = [h for h in hits if h[2]]
    print(f"\n  scroll={scroll}:")
    for name, y, bad in hits:
        print(f"    {name:<14} 窗口内 y={y:>4}  "
              f"{'⚠ 落在标题栏拖动区 → 点击会被截走' if bad else 'OK'}")
    if danger:
        print(f"    ** {len(danger)} 个控件会触发拖动误判 **")
w.close()
