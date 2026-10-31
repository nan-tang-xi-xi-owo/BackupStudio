"""实测当前无边框实现丢失了哪些系统行为（对照 Agent 报告的路线 B 缺陷）。"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定時備份及其压缩項目".replace("時", "时").replace("備", "备").replace("壓", "压"))
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目")
sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="fb-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000

from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

st = AppState(TaskStore(CFG / "tasks.json"))
w = MainWindow(st)
w.resize(900, 600)
w.show()
for _ in range(10):
    app.processEvents()

hwnd = int(w.winId())
style = user32.GetWindowLongW(wt.HWND(hwnd), GWL_STYLE) & 0xFFFFFFFF
print(f"  HWND = 0x{hwnd:X}")
print(f"  窗口样式 = 0x{style:08X}   (纯 frameless 应为 0x96000000)")
print(f"    WS_CAPTION      = {bool(style & WS_CAPTION)}")
print(f"    WS_THICKFRAME   = {bool(style & WS_THICKFRAME)}")
print(f"    WS_SYSMENU      = {bool(style & WS_SYSMENU)}")
print(f"    WS_MINIMIZEBOX  = {bool(style & WS_MINIMIZEBOX)}")
print(f"    WS_MAXIMIZEBOX  = {bool(style & WS_MAXIMIZEBOX)}")

menu = user32.GetSystemMenu(wt.HWND(hwnd), False)
print(f"\n  GetSystemMenu = 0x{menu:X}  ({'NULL，系统菜单已丢失' if not menu else '可用'})")
if menu:
    n = user32.GetMenuItemCount(wt.HWND(menu))
    print(f"    菜单项数 = {n}")

# 检查 DWM 阴影：看窗口外侧若干像素是否有渐变
from PySide6.QtGui import QGuiApplication

scr = QGuiApplication.primaryScreen()
g = w.frameGeometry()
print(f"\n  窗口几何(逻辑) = {g.x()},{g.y()} {g.width()}x{g.height()}")

# 抓屏幕，看窗口左右外侧 20px 的亮度变化（有阴影则应有渐变）
img = scr.grabWindow(0).toImage()
dpr = scr.devicePixelRatio()
left = int((g.x() - 12) * dpr)
top = int((g.y() + 20) * dpr)
if 0 <= left < img.width() and 0 <= top < img.height():
    print("  窗口左侧 12px 处的像素（有 DWM 阴影时应比背景暗）：")
    for dx in (16, 12, 8, 4, 1):
        x = int((g.x() - dx) * dpr)
        if 0 <= x < img.width():
            c = img.pixelColor(x, top)
            print(f"    左 {dx:>2}px: {c.name()}")

w.close()
