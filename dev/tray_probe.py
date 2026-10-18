"""在隐藏桌面探针里验证托盘图标能否被系统接收，并导出托盘图标的实际渲染结果。

做法：在进程内同时创建一个窗口和一个 QSystemTrayIcon，等外壳处理完
Shell_NotifyIcon 后再查询状态；同时把托盘图标各尺寸导出成 PNG 供人工查看。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(ROOT / "build" / "tray-probe")
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QSize, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon  # noqa: E402

from backupstudio.gui import app_icon, tray_state_icon  # noqa: E402

out_dir = ROOT / "build" / "tray-shots"
out_dir.mkdir(parents=True, exist_ok=True)

app = QApplication.instance() or QApplication(sys.argv[:1])
print("platform:", app.platformName())
print("isSystemTrayAvailable:", QSystemTrayIcon.isSystemTrayAvailable())

# 导出图标各尺寸，确认图标本身不是空白
for state in ("idle", "running", "success", "failure"):
    icon = tray_state_icon(state)
    for size in (16, 32, 48):
        pm: QPixmap = icon.pixmap(QSize(size, size))
        pm.save(str(out_dir / f"tray-{state}-{size}.png"))
    print(f"tray_state_icon({state}) null={icon.isNull()} sizes={[mask for mask in (16, 24, 32, 48, 64)]}")

tray = QSystemTrayIcon(tray_state_icon("idle"))
tray.setToolTip("BackupStudio 托盘探针")
menu = QMenu()
menu.addAction("探针")
tray.setContextMenu(menu)
tray.show()
print("托盘 show() 后 isVisible:", tray.isVisible(), " icon null:", tray.icon().isNull())

# 直接用 Win32 找一个属于本进程的托盘窗口（Qt 会用自身的 message 窗口接收回调）
user32 = ctypes.windll.user32
found: list[tuple[str, str]] = []
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def enum_cb(hwnd, lparam):  # noqa: ANN001
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        found.append((cls.value, str(user32.IsWindowVisible(hwnd))))
    return True


user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
print("本进程顶层窗口:", found[:10])

# 让外壳处理 Shell_NotifyIcon（需要消息循环跑一会儿）
deadline = time.time() + 2.5
while time.time() < deadline:
    app.processEvents()
    time.sleep(0.05)

# 查询通知区域里是否有我们的图标：遍历 TrayNotifyWnd 的子窗口里的 Toolbar 按钮
shell = user32.FindWindowW("Shell_TrayWnd", None)
print("Shell_TrayWnd:", shell)
if shell:
    notify = user32.FindWindowExW(shell, None, "TrayNotifyWnd", None)
    print("TrayNotifyWnd:", notify)
    for cls_name in ("SysPager", "ToolbarWindow32"):
        h = user32.FindWindowExW(notify, None, cls_name, None)
        print(f"  {cls_name}:", h)

print("探针完成")
tray.hide()
app.quit()
