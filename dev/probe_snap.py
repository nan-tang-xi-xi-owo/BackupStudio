"""验证无边框窗口的 Aero Snap 与拖动（关键行为，必须实测）。

Agent 报告说路线 B（纯 frameless）会丢系统菜单 —— 但我的实测显示
GetSystemMenu 有 7 项、WS_SYSMENU 在位。说明 Qt 的 Frameless 与
裸 CreateWindowEx(WS_POPUP) 不同。这里把其余关键行为也测清楚：

1. WM_NCHITTEST 当前返回什么（决定边缘缩放是否走系统）
2. 拖到屏幕顶端是否触发 Snap（用 SetWindowPos 模拟最终位置，
   看窗口是否被系统吸附成最大化）
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目")
sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="snap-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtWidgets import QApplication

app = QApplication([])
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32

GWL_STYLE = -16
for name, bit in (("WS_CAPTION", 0x00C00000), ("WS_THICKFRAME", 0x00040000),
                  ("WS_SYSMENU", 0x00080000), ("WS_MINIMIZEBOX", 0x00020000),
                  ("WS_MAXIMIZEBOX", 0x00010000), ("WS_POPUP", 0x80000000)):
    pass

from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

st = AppState(TaskStore(CFG / "tasks.json"))
w = MainWindow(st)
w.resize(760, 520)
w.show()
for _ in range(10):
    app.processEvents()

hwnd = int(w.winId())
style = user32.GetWindowLongW(wt.HWND(hwnd), GWL_STYLE) & 0xFFFFFFFF
print(f"  窗口样式 = 0x{style:08X}")
for name, bit in (("WS_CAPTION", 0x00C00000), ("WS_THICKFRAME", 0x00040000),
                  ("WS_SYSMENU", 0x00080000), ("WS_MINIMIZEBOX", 0x00020000),
                  ("WS_MAXIMIZEBOX", 0x00010000), ("WS_POPUP", 0x80000000)):
    print(f"    {name:<16} = {bool(style & bit)}")

# WM_NCHITTEST 探测：对窗口各处 SendMessage(WM_NCHITTEST)
WM_NCHITTEST = 0x0084
HT = {0: "HTNOWHERE", 1: "HTCLIENT", 2: "HTCAPTION", 10: "HTLEFT",
      11: "HTRIGHT", 12: "HTTOP", 13: "HTTOPLEFT", 14: "HTTOPRIGHT",
      15: "HTBOTTOM", 16: "HTBOTTOMLEFT", 17: "HTBOTTOMRIGHT"}
r = wt.RECT()
user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
print(f"\n  窗口物理矩形: ({r.left},{r.top}) - ({r.right},{r.bottom})")
tests = {
    "标题栏中央": (r.left + (r.right - r.left) // 2, r.top + 15),
    "上边缘": (r.left + (r.right - r.left) // 2, r.top + 2),
    "左边缘": (r.left + 2, r.top + (r.bottom - r.top) // 2),
    "客户区中央": (r.left + 300, r.top + 300),
}
print("  WM_NCHITTEST 返回值：")
for label, (x, y) in tests.items():
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    res = user32.SendMessageW(wt.HWND(hwnd), WM_NCHITTEST, 0, lp)
    print(f"    {label:<12} -> {res} ({HT.get(res, '?')})")

# Aero Snap：把窗口拖到屏幕顶部附近，看是否被吸附
print("\n  Aero Snap 测试（把窗口移到 y=0 附近，看系统是否吸附）：")
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
user32.SetWindowPos(wt.HWND(hwnd), None, 100, 0, 760, 520,
                    SWP_NOZORDER | SWP_NOACTIVATE)
for _ in range(8):
    app.processEvents()
r2 = wt.RECT()
user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r2))
print(f"    移到 y=0 后窗口矩形: ({r2.left},{r2.top}) - ({r2.right},{r2.bottom})")
print(f"    isMaximized = {w.isMaximized()}")
print(f"    => SetWindowPos 不会触发 Snap（Snap 由用户拖动手势触发，"
      f"程序化移动测不出）。此项需真人手测。")
w.close()
