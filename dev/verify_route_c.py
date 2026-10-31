"""验证 FramelessChrome（路线 C）核心行为。"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目")
sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="c-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

app = QApplication([])
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32

# 最小复现：裸 QMainWindow + Frameless + FramelessChrome
win = QMainWindow()
win.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
win.setCentralWidget(QWidget())
win.setWindowTitle("route C probe")
win.resize(760, 520)
win.show()
for _ in range(10):
    app.processEvents()

hwnd = int(win.winId())


def style_of(h: int) -> int:
    return user32.GetWindowLongW(wt.HWND(h), -16) & 0xFFFFFFFF


def report(tag: str) -> None:
    s = style_of(hwnd)
    print(f"  {tag}: style=0x{s:08X}")
    for name, bit in (("WS_CAPTION", 0x00C00000), ("WS_THICKFRAME", 0x00040000),
                      ("WS_SYSMENU", 0x00080000), ("WS_MINIMIZEBOX", 0x00020000),
                      ("WS_MAXIMIZEBOX", 0x00010000)):
        print(f"    {name:<16} = {bool(s & bit)}")
    menu = user32.GetSystemMenu(wt.HWND(hwnd), False)
    print(f"    GetSystemMenu = 0x{menu:X}"
          f"{'  items=' + str(user32.GetMenuItemCount(wt.HWND(menu))) if menu else '  (NULL)'}")


report("① 仅 Frameless（路线 B）")

from backupstudio.gui.winframe import HTCAPTION, HTCLIENT, HTLEFT, HTTOP, FramelessChrome

toggled = []
chrome = FramelessChrome(win, title_height=34)
chrome.on_toggle_max = lambda: toggled.append(1)
ok = chrome.install()
print(f"\n  install() -> {ok}")
for _ in range(8):
    app.processEvents()

report("② 装上 FramelessChrome（路线 C）")

# 命中测试
r = wt.RECT()
user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
print(f"\n  窗口物理矩形 ({r.left},{r.top})-({r.right},{r.bottom})")


def hit(x: int, y: int) -> int:
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    return user32.SendMessageW(wt.HWND(hwnd), 0x0084, 0, lp)


HT = {1: "HTCLIENT", 2: "HTCAPTION", 10: "HTLEFT", 11: "HTRIGHT",
      12: "HTTOP", 13: "HTTOPLEFT", 14: "HTTOPRIGHT", 15: "HTBOTTOM",
      16: "HTBOTTOMLEFT", 17: "HTBOTTOMRIGHT"}

CX, CY = (r.left + r.right) // 2, (r.top + r.bottom) // 2
cases = {
    "标题栏中央": (CX, r.top + 18),
    "上边缘(2px)": (CX, r.top + 2),
    "左上角(2px)": (r.left + 2, r.top + 2),
    "左边缘": (r.left + 2, CY),
    "右边缘": (r.right - 3, CY),
    "下边缘": (CX, r.bottom - 3),
    "客户区中央": (CX, CY),
}
print("\n  WM_NCHITTEST：")
for label, (x, y) in cases.items():
    res = hit(x, y)
    print(f"    {label:<14} -> {res:>2} {HT.get(res, '?')}")

win.close()
