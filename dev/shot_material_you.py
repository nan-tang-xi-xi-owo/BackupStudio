"""渲染 Material You 不同源色/风格的截图（1.1.3）。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="my-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)
OUT = R / "docs" / "screenshots" / "styles"
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication
app = QApplication([])
from backupstudio.store import TaskStore
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui.styles import set_material_you, styled_palette, set_current
from backupstudio.gui import stylesheet

ts = TaskStore(CFG / "tasks.json")
st = AppState(ts)
set_current("mag")

cases = [
    ("#6750A4", "TONAL_SPOT", "my-purple-tonal"),
    ("#B1611C", "TONAL_SPOT", "my-orange-tonal"),
    ("#006D3B", "VIBRANT", "my-green-vibrant"),
    ("#B3261E", "EXPRESSIVE", "my-red-expressive"),
]
for seed, style, tag in cases:
    set_material_you(seed, style)
    st.palette = st._resolve_palette()
    app.setStyleSheet(stylesheet(st.palette))
    w = MainWindow(st); w.resize(1000, 660); w.show()
    for _ in range(12):
        app.processEvents()
    p = OUT / f"{tag}.png"
    w.grab().save(str(p))
    pal = styled_palette(st.palette, "mag")
    print(f"  {tag:<22} seed={seed} primary={pal.accent} {p.stat().st_size} B")
    w.close()
