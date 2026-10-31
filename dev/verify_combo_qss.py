"""确认 QComboBox 在玻璃卡片里是否真的应用了 QSS 背景。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="vq-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui import stylesheet
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
ts.settings.ui_style = "glass"
ts.save()
st = AppState(ts)
app.setStyleSheet(stylesheet(st.palette))
w = MainWindow(st)
w.resize(1100, 700)
w.show()
for _ in range(12):
    app.processEvents()
w._select("settings")
for _ in range(16):
    app.processEvents()

sp = w._pages["settings"]
cb = sp.theme_combo
print(f"  风格 = {st.store.settings.ui_style}")
print(f"  cb 尺寸 = {cb.width()}x{cb.height()}")
print(f"  cb styleSheet = {cb.styleSheet()!r}")
print(f"  cb 继承的 QSS 是否含 QComboBox: {'QComboBox' in (app.styleSheet() or '')}")

# 单独 grab 这个下拉框 —— 它会自己画背景
own = cb.grab().toImage()
print(f"\n  cb.grab() 中心 = {own.pixelColor(cb.width() // 2, cb.height() // 2).name()}")
print(f"  cb.grab() 左边缘 = {own.pixelColor(1, cb.height() // 2).name()}")

# 从整窗截图取同一位置
win = w.grab().toImage()
tl = cb.mapTo(w, QPoint(0, 0))
_dpr = win.devicePixelRatio()
c = win.pixelColor(int((tl.x() + cb.width() // 2) * _dpr), int((tl.y() + cb.height() // 2) * _dpr))
e = win.pixelColor(int((tl.x() + 1) * _dpr), int((tl.y() + cb.height() // 2) * _dpr))
print(f"  整窗截图 中心 = {c.name()}   左边缘 = {e.name()}")

# 关键：把 cb 放进一个纯白父容器再截图，排除玻璃影响
from PySide6.QtWidgets import QWidget, QVBoxLayout

host = QWidget()
host.setStyleSheet("background: #FFFFFF;")
hl = QVBoxLayout(host)
cb2 = type(cb)()
cb2.addItems(["A", "B"])
hl.addWidget(cb2)
host.resize(300, 80)
host.show()
for _ in range(8):
    app.processEvents()
h_img = host.grab().toImage()
c2 = h_img.pixelColor(cb2.mapTo(host, QPoint(0, 0)).x() + cb2.width() // 2,
                      cb2.mapTo(host, QPoint(0, 0)).y() + cb2.height() // 2)
print(f"\n  在白底容器里 中心 = {c2.name()}   (应为 F7FAFF)")
print(f"  => 差值大的话说明玻璃卡片里的合成有问题")
w.close()
