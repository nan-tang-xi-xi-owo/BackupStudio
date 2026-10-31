"""验证无边框窗口的关键能力（Qt 6.12 / Windows 10）。

要验证：
1. FramelessWindowHint 下窗口能否正常显示
2. windowHandle().startSystemMove() 是否存在且调用返回 True
3. startSystemResize(edges) 是否存在
4. 双击标题栏最大化是否能自己实现
5. Aero Snap（贴边吸附）在无边框下是否失效
"""
import sys

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication(sys.argv)

w = QWidget()
w.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
w.setWindowTitle("frameless probe")
w.resize(300, 160)
w.show()
app.processEvents()

h = w.windowHandle()
print(f"  windowHandle: {h is not None}")
print(f"  startSystemMove 存在: {hasattr(h, 'startSystemMove')}")
print(f"  startSystemResize 存在: {hasattr(h, 'startSystemResize')}")

# 实测 startSystemMove 返回值（无鼠标按下时可能返回 False，但不应抛异常）
try:
    r = h.startSystemMove()
    print(f"  startSystemMove() -> {r}  (无按键时 False 属正常)")
except Exception as e:  # noqa: BLE001
    print(f"  startSystemMove() 抛异常: {type(e).__name__}: {e}")

try:
    r = h.startSystemResize(Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
    print(f"  startSystemResize() -> {r}")
except Exception as e:  # noqa: BLE001
    print(f"  startSystemResize() 抛异常: {type(e).__name__}: {e}")

# 检查窗口实际几何与系统边框
print(f"  geometry={w.geometry()}  frameGeometry={w.frameGeometry()}")
print(f"  边框差(物理像素): {w.frameGeometry().width() - w.geometry().width()} x "
      f"{w.frameGeometry().height() - w.geometry().height()}")

# 双击最大化：自己实现的可行性（isMaximized / showMaximized）
print(f"  初始 isMaximized: {w.isMaximized()}")
w.showMaximized()
app.processEvents()
print(f"  showMaximized 后: {w.isMaximized()}")
w.showNormal()
app.processEvents()
print(f"  showNormal 后: {w.isMaximized()}")

w.close()
print("  done")
