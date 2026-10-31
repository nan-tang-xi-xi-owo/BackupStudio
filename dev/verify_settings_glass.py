"""验证设置页玻璃效果 + 布局是否还被截断。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="gs-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)
OUT = R / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
ts.settings.ui_style = "glass"
ts.save()
st = AppState(ts)
w = MainWindow(st)
w.resize(1100, 700)
w.show()
for _ in range(14):
    app.processEvents()

w._select("settings")
for _ in range(14):
    app.processEvents()

sp = w._pages.get("settings")
print("  设置页外观控件：")
for name in ("theme_combo", "style_combo", "seed_edit", "btn_pick_seed", "tonal_combo"):
    wdg = getattr(sp, name, None)
    if wdg is None:
        print(f"    {name:<16} 缺失")
        continue
    g = wdg.geometry()
    vis = wdg.isVisible()
    right = g.x() + g.width()
    print(f"    {name:<16} x={g.x():>4} w={g.width():>4} right={right:>4} 可见={vis}")

# 卡片是否被水平裁掉
from backupstudio.gui.widgets import Card

for c in sp.findChildren(Card)[:2]:
    print(f"    卡片 {c.objectName()}: w={c.width()} glass={c._glass_on}")

p = OUT / "settings-glass-fixed.png"
w.grab().save(str(p))
print(f"\n  截图: {p.name} {p.stat().st_size} B")
w.close()
