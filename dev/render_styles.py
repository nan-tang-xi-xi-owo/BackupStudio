"""渲染六种风格并量化差异（放 dev/ 下，避免被 build 清理误删）。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="vs-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)
OUT = R / "docs" / "screenshots" / "styles"
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication
app = QApplication([])
from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
for nm, fmt in (("照片备份", "7z"), ("文档备份", "zip")):
    ts.upsert(BackupTask(name=nm, sources=[SourceConfig(path=str(CFG))],
                         destination=str(CFG / "bak"),
                         options=ArchiveOptions(fmt=fmt, force=True)))

from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui.styles import style_names
from backupstudio.gui import stylesheet

st = AppState(ts)
imgs = {}
for key, name in style_names():
    st.set_style(key)
    app.setStyleSheet(stylesheet(st.palette))
    w = MainWindow(st); w.resize(1000, 660); w.show()
    for _ in range(14):
        app.processEvents()
    pm = w.grab()
    imgs[key] = pm.toImage()
    p = OUT / f"real-{key}.png"
    pm.save(str(p))
    print(f"  {key:<6} {name:<14} {p.stat().st_size:>7} B")
    w.close()

print("\n  差异（相对 win）:")
keys = [k for k, _ in style_names()]
a = imgs[keys[0]]
total = a.width() * a.height()
for k in keys[1:]:
    b = imgs[k]
    d = sum(1 for y in range(0, a.height(), 3) for x in range(0, a.width(), 3)
            if a.pixel(x, y) != b.pixel(x, y))
    print(f"    win vs {k:<6}: {d * 9 / total * 100:5.1f}%")
