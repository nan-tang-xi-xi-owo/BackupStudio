"""诊断托盘图标：在当前会话里实际创建窗口 + 托盘，并检查系统侧状态。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(ROOT / "build" / "tray-diag")
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])

print("== 基本环境 ==")
print("platform:", app.platformName())
print("QSystemTrayIcon.isSystemTrayAvailable():", QSystemTrayIcon.isSystemTrayAvailable())
print("QSystemTrayIcon.supportsMessages():", QSystemTrayIcon.supportsMessages())

from backupstudio.gui import app_icon, tray_state_icon  # noqa: E402
from backupstudio.store import TaskStore  # noqa: E402
from backupstudio.gui.appstate import AppState  # noqa: E402
from backupstudio.gui.main_window import MainWindow  # noqa: E402

config = Path(os.environ["BACKUPSTUDIO_CONFIG_DIR"])
config.mkdir(parents=True, exist_ok=True)
store = TaskStore(config / "tasks.json")
state = AppState(store)
win = MainWindow(state)
win.show()

print("== 窗口 ==")
print("窗口可见:", win.isVisible(), "标题:", win.windowTitle())
print("tray_available 标志:", getattr(win, "tray_available", None))
tray = getattr(win, "tray", None)
if tray is not None:
    print("托盘对象存在:", True)
    print("托盘可见:", tray.isVisible())
    print("托盘图标为空:", tray.icon().isNull())
    print("托盘支持消息:", tray.supportsMessages())
    print("托盘 tooltip:", tray.toolTip())
else:
    print("托盘对象: 不存在")

# 列出当前所有可见的托盘图标（Qt 内部抽象层）
try:
    from PySide6.QtGui import QGuiApplication

    print("== 系统托盘区域 ==")
    print("QGuiApplication.platformName:", QGuiApplication.platformName())
except Exception as exc:  # noqa: BLE001
    print("读取平台信息失败:", exc)

# 枚举 Windows 通知区域窗口是否存在（Shell_TrayWnd）
if sys.platform == "win32":
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    print("== Windows 外壳 ==")
    print("任务栏窗口 Shell_TrayWnd:", hwnd)
    print("explorer.exe 存在:", bool(ctypes.windll.kernel32.OpenProcess(0x1000, False, _explorer_pid())) if False else "-")
    try:
        import psutil

        print("explorer 进程:", [p.info["pid"] for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() == "explorer.exe"])
    except Exception:  # noqa: BLE001
        pass


def finish() -> None:
    time.sleep(0.3)
    app.quit()


QTimer.singleShot(1500, finish)
app.exec()
print("done")
