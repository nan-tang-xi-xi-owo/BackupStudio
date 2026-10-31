"""第三次测量阴影 —— 用 Win32 SetWindowPos 强制定位，并先验证采样点背景。

前两次失败原因：
① 采样点落在终端黑底（亮度 12-15），原生参照也"无阴影" → 方法无效
② w.move() 被 MainWindow.__init__ 里的尺寸/位置逻辑覆盖，窗口没到空白区

这次：用 SetWindowPos 强制移动，并打印采样点处的背景色以自证位置正确。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目")
sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="sh4-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication([])
import ctypes
import ctypes.wintypes as wt

dwm = ctypes.windll.dwmapi
user32 = ctypes.windll.user32
scr = QGuiApplication.primaryScreen()
dpr = scr.devicePixelRatio()
full = scr.grabWindow(0)
FW, FH = full.width(), full.height()
print(f"  屏幕(物理) {FW}x{FH}  dpr={dpr}")

# 选右下角一片区域放窗口（通常桌面空白）
PX, PY = int(FW * 0.55), int(FH * 0.55)
print(f"  目标物理位置 ({PX},{PY})")
c = full.toImage().pixelColor(PX - 40, PY + 100)
print(f"  该处背景色 = {c.name()}  （应为桌面浅色，若非浅色则本次仍不可靠）")


def place(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    SWP_NOZORDER, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0004, 0x0010, 0x0040
    user32.SetWindowPos(wt.HWND(hwnd), None, x, y, w, h,
                        SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW)


def measure(hwnd: int, tag: str) -> float:
    for _ in range(10):
        app.processEvents()
    r = wt.RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    img = scr.grabWindow(0).toImage()
    y = (r.top + r.bottom) // 2
    vals = []
    for dx in (24, 20, 16, 12, 8, 5, 3):
        x = r.left - dx
        if 0 <= x < img.width() and 0 <= y < img.height():
            cc = img.pixelColor(x, y)
            vals.append((dx, cc.name(),
                         0.2126 * cc.red() + 0.7152 * cc.green() + 0.0722 * cc.blue()))
    print(f"  {tag}  (窗口 left={r.left} top={r.top}):")
    for dx, name, lum in vals:
        print(f"    -{dx:>2}px  {name}  {lum:>6.1f}")
    if len(vals) < 2:
        return 0.0
    spread = vals[0][2] - vals[-1][2]
    ok = "有阴影" if spread > 2 else "无阴影"
    print(f"    落差 {spread:+6.1f} -> {ok}")
    return spread


native = QWidget()
native.resize(400, 240)
native.show()
place(int(native.winId()), PX, PY, 400, 240)
s_native = measure(int(native.winId()), "① 原生窗口（参照）")

from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

st = AppState(TaskStore(CFG / "tasks.json"))
w = MainWindow(st)
w.show()
hwnd = int(w.winId())
place(hwnd, PX, PY, 700, 460)
s_before = measure(hwnd, "② BackupStudio 无边框")


class M(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                ("t", ctypes.c_int), ("b", ctypes.c_int)]


hr = dwm.DwmExtendFrameIntoClientArea(wt.HWND(hwnd), ctypes.byref(M(1, 1, 1, 1)))
place(hwnd, PX, PY, 700, 460)
s_after = measure(hwnd, "③ DwmExtendFrameIntoClientArea 后")

print(f"\n  DwmExtend HRESULT = 0x{hr & 0xFFFFFFFF:08X}")
print(f"  原生={s_native:+.1f}  无边框={s_before:+.1f}  扩展后={s_after:+.1f}")
if s_native > 2 and s_before <= 2:
    print("  => 确认：原生有阴影，无边框丢了阴影")
elif s_native <= 2:
    print("  => 参照组仍测不出阴影，本次测量无效")
else:
    print("  => 两者都有阴影")
w.close()
native.close()
