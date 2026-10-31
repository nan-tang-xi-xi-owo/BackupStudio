"""精确测量：无边框窗口是否丢了 DWM 阴影（采样窗口外侧像素）。"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目")
sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="sh-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication([])
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
dwm = ctypes.windll.dwmapi

scr = QGuiApplication.primaryScreen()


def sample_outside(w: QWidget, tag: str) -> None:
    """采样窗口左外侧 30..2 px 的亮度，有阴影应看到渐变。"""
    from PySide6.QtCore import QPoint

    g = w.frameGeometry()
    dpr = scr.devicePixelRatio()
    img = scr.grabWindow(0).toImage()
    y = int((g.y() + g.height() // 2) * dpr)
    vals = []
    for dx in (30, 26, 22, 18, 14, 10, 6, 3, 2):
        x = int((g.x() - dx) * dpr)
        if 0 <= x < img.width() and 0 <= y < img.height():
            c = img.pixelColor(x, y)
            lum = 0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()
            vals.append((dx, c.name(), round(lum, 1)))
    print(f"  {tag}:")
    for dx, name, lum in vals:
        print(f"    -{dx:>2}px  {name}  亮度 {lum:>6.1f}")
    if len(vals) >= 2:
        spread = vals[0][2] - vals[-1][2]
        print(f"    亮度落差（外侧-近窗）= {spread:+.1f}  "
              f"{'有阴影渐变' if abs(spread) > 2 else '无阴影（平坦）'}")


# ① 参照：原生窗口
native = QWidget()
native.setWindowTitle("native ref")
native.resize(420, 260)
native.move(120, 120)
native.show()
for _ in range(8):
    app.processEvents()
sample_outside(native, "① 原生窗口（参照）")

# ② 当前 main_window 的无边框实现
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

st = AppState(TaskStore(CFG / "tasks.json"))
w = MainWindow(st)
w.resize(900, 600)
w.move(120, 420)
w.show()
for _ in range(10):
    app.processEvents()
sample_outside(w, "② BackupStudio（无边框）")

# ③ 试 DwmExtendFrameIntoClientArea 能否恢复阴影
hwnd = int(w.winId())


class MARGINS(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                ("t", ctypes.c_int), ("b", ctypes.c_int)]


hr = dwm.DwmExtendFrameIntoClientArea(wt.HWND(hwnd), ctypes.byref(MARGINS(1, 1, 1, 1)))
print(f"\n  DwmExtendFrameIntoClientArea(1,1,1,1) -> 0x{hr & 0xFFFFFFFF:08X}")
for _ in range(10):
    app.processEvents()
sample_outside(w, "③ 调用 DwmExtendFrameIntoClientArea 之后")

w.close()
native.close()
