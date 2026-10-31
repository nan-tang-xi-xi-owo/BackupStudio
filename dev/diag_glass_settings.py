"""诊断：设置页玻璃为何几乎看不出（1.1.5）。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="gd-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui.styles import set_current
from backupstudio.gui.widgets import Card
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
ts.settings.ui_style = "glass"
ts.save()
st = AppState(ts)
print(f"  当前风格: {st.store.settings.ui_style}")
w = MainWindow(st)
w.resize(1100, 700)
w.show()
for _ in range(14):
    app.processEvents()

for key in ("tasks", "history", "environment", "settings"):
    w._select(key)
    for _ in range(10):
        app.processEvents()
    page = w._pages.get(key)
    cards = page.findChildren(Card) if page else []
    tops = [c for c in cards if c.objectName() == "Card"]
    on = [c for c in cards if c._glass_on]
    print(f"\n  === {key} ===")
    print(f"    卡片总数 {len(cards)}  顶层 Card {len(tops)}  已开玻璃 {len(on)}")
    # 看顶层卡片的父级链（决定采样到的是窗口光斑还是滚动区底色）
    for c in tops[:2]:
        chain = []
        p = c.parent()
        depth = 0
        while p is not None and depth < 6:
            chain.append(f"{type(p).__name__}({p.objectName() or '-'})")
            p = p.parent()
            depth += 1
        print(f"      父级链: {' ← '.join(chain)}")
w.close()
