"""可靠地测量 DWM 阴影：窗口移到桌面空白处，对照原生窗口。

上一次测量失败的原因：采样点落在终端/编辑器窗口的黑底上（亮度 12-15），
原生参照组也读出"无阴影"，说明测试位置无效 —— 不能据此下结论。
这次：
1. 先截全屏，找一块亮度均匀的浅色区域当背景
2. 把被测窗口放到该区域
3. 同时测原生 QWidget 参照
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目")
sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="sh2-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication([])
import ctypes
import ctypes.wintypes as wt

dwm = ctypes.windll.dwmapi
scr = QGuiApplication.primaryScreen()


def find_empty_area() -> tuple[int, int]:
    """找桌面上一块亮度均匀的浅色区域（逻辑坐标）。"""
    img = scr.grabWindow(0).toImage()
    dpr = scr.devicePixelRatio()
    w, h = img.width(), img.height()
    best, best_var = (0, 0), 1e9
    for ly in range(60, int(h / dpr) - 200, 40):
        for lx in range(60, int(w / dpr) - 400, 40):
            vals = []
            for dx in range(0, 300, 30):
                for dy in range(0, 150, 30):
                    c = img.pixelColor(int((lx + dx) * dpr), int((ly + dy) * dpr))
                    vals.append(0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue())
            avg = sum(vals) / len(vals)
            var = max(abs(v - avg) for v in vals)
            # 要亮（浅色桌面）且均匀
            if avg > 120 and var < best_var:
                best_var, best = var, (lx, ly)
    print(f"  找到均匀浅色区域: ({best[0]},{best[1]})  最大偏差 {best_var:.1f}")
    return best


def measure(w: QWidget, tag: str) -> float:
    """测量窗口左外侧的亮度落差。返回 落差值（>2 表示有阴影）。"""
    g = w.frameGeometry()
    dpr = scr.devicePixelRatio()
    img = scr.grabWindow(0).toImage()
    y = int((g.y() + g.height() // 2) * dpr)
    vals = []
    for dx in (24, 20, 16, 12, 8, 5, 3):
        x = int((g.x() - dx) * dpr)
        if 0 <= x < img.width() and 0 <= y < img.height():
            c = img.pixelColor(x, y)
            vals.append((dx, c.name(), 0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()))
    print(f"  {tag}:")
    for dx, name, lum in vals:
        print(f"    -{dx:>2}px  {name}  {lum:>6.1f}")
    if len(vals) < 2:
        return 0.0
    # 阴影：近窗应比远处暗
    spread = vals[0][2] - vals[-1][2]
    print(f"    落差 = {spread:+6.1f}  -> {'有阴影' if spread > 2 else '无阴影'}")
    return spread


area = find_empty_area()

native = QWidget()
native.resize(400, 240)
native.move(area[0], area[1])
native.show()
for _ in range(10):
    app.processEvents()
s_native = measure(native, "① 原生窗口（参照，必有阴影）")

from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

st = AppState(TaskStore(CFG / "tasks.json"))
w = MainWindow(st)
w.resize(700, 460)
w.move(area[0], area[1])
w.show()
for _ in range(12):
    app.processEvents()
s_before = measure(w, "② BackupStudio 无边框")

hwnd = int(w.winId())


class M(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                ("t", ctypes.c_int), ("b", ctypes.c_int)]


hr = dwm.DwmExtendFrameIntoClientArea(wt.HWND(hwnd), ctypes.byref(M(1, 1, 1, 1)))
for _ in range(12):
    app.processEvents()
s_after = measure(w, "③ DwmExtendFrameIntoClientArea(1,1,1,1) 后")
print(f"\n  DwmExtend 返回 0x{hr & 0xFFFFFFFF:08X}")
print(f"\n  结论: 原生={s_native:+.1f}  无边框={s_before:+.1f}  扩展后={s_after:+.1f}")
if s_native > 2 and s_before <= 2:
    print("  => 原生有阴影、无边框无阴影：确认丢了")
elif s_native <= 2:
    print("  => 参照组也测不出阴影，本次测量无效（不能下结论）")
w.close()
native.close()
