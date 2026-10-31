"""复现审计 Agent 报告的 NameError，并修复。"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="nf-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

st = AppState(TaskStore(CFG / "tasks.json"))
w = MainWindow(st)
w.resize(900, 600)
w.show()
app.processEvents()

print(f"  _frameless = {w._frameless}")

# 直接调用 _edge_at —— 在 offscreen 下 frameless 仍为 True（sys.platform 判定）
try:
    e = w._edge_at(QPoint(2, 2))
    print(f"  _edge_at(QPoint(2,2)) -> {e}   OK")
except NameError as exc:
    print(f"  _edge_at 抛 NameError: {exc}   <<< 缺陷复现")

# 模拟鼠标移动（Windows 上必走这条）
from PySide6.QtCore import QEvent
from PySide6.QtGui import QMouseEvent

try:
    ev = QMouseEvent(QEvent.Type.MouseMove, QPoint(100, 100), QPoint(100, 100),
                     QPoint(100, 100), __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.MouseButton.NoButton,
                     __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.MouseButton.NoButton,
                     __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.KeyboardModifier.NoModifier)
    w.mouseMoveEvent(ev)
    print("  mouseMoveEvent -> OK")
except NameError as exc:
    print(f"  mouseMoveEvent 抛 NameError: {exc}   <<< 缺陷复现")
except Exception as exc:  # noqa: BLE001
    print(f"  mouseMoveEvent 抛 {type(exc).__name__}: {exc}")

w.close()
