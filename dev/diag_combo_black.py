"""逐步定位：glass 风格下 QComboBox 为何纯黑。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="bd-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtWidgets import QApplication, QComboBox, QWidget, QVBoxLayout

app = QApplication([])
from backupstudio.gui import stylesheet
from backupstudio.gui.styles import set_current, styled_palette
from backupstudio.gui import LIGHT

set_current("glass")
qss = stylesheet(LIGHT)
app.setStyleSheet(qss)


def probe(cb: QComboBox, tag: str) -> None:
    img = cb.grab().toImage()
    c = img.pixelColor(img.width() // 2, img.height() // 2).name()
    l = img.pixelColor(0, img.height() // 2).name()
    print(f"  {tag:<34} 中心={c} 左={l}")


# ① 裸 QComboBox（无父窗口、无 scroll）
cb1 = QComboBox()
cb1.addItems(["浅色", "深色"])
cb1.resize(200, 34)
cb1.show()
for _ in range(6):
    app.processEvents()
probe(cb1, "① 裸 QComboBox（全局 glass QSS）")

# ② 放进 QScrollArea（模拟设置页）
from PySide6.QtWidgets import QScrollArea

sa = QScrollArea()
sa.setWidgetResizable(True)
sa.setFrameShape(QScrollArea.Shape.NoFrame)
inner = QWidget()
il = QVBoxLayout(inner)
cb2 = QComboBox()
cb2.addItems(["浅色", "深色"])
il.addWidget(cb2)
il.addStretch(1)
sa.setWidget(inner)
sa.resize(400, 200)
sa.show()
for _ in range(6):
    app.processEvents()
probe(cb2, "② 放进 QScrollArea（未设透明）")

# ③ 给 scroll 设透明 QSS
sa2 = QScrollArea()
sa2.setWidgetResizable(True)
sa2.setFrameShape(QScrollArea.Shape.NoFrame)
sa2.setStyleSheet(
    "QScrollArea { background: transparent; border: none; }"
    "QScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }"
)
sa2.viewport().setAutoFillBackground(False)
inner2 = QWidget()
il2 = QVBoxLayout(inner2)
cb3 = QComboBox()
cb3.addItems(["浅色", "深色"])
il2.addWidget(cb3)
il2.addStretch(1)
sa2.setWidget(inner2)
sa2.resize(400, 200)
sa2.move(0, 300)
sa2.show()
for _ in range(6):
    app.processEvents()
probe(cb3, "③ QScrollArea + 透明 QSS（当前做法）")

# ④ 看 cb3 的实际调色板
pal = cb3.palette()
print(f"\n  cb3 palette Base = {pal.color(pal.ColorRole.Base).name()}")
print(f"  cb3 palette Window = {pal.color(pal.ColorRole.Window).name()}")
print(f"  cb3 autoFillBackground = {cb3.autoFillBackground()}")
print(f"  cb3 testAttribute(WA_TranslucentBackground) = "
      f"{cb3.testAttribute(__import__('PySide6.QtCore', fromlist=['Qt']).Qt.WidgetAttribute.WA_TranslucentBackground)}")
print(f"  cb3 样式表 = {cb3.styleSheet()!r}")
