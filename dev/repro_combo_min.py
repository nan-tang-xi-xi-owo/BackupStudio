"""最小复现：Card 内联样式是否影响子 QComboBox 的背景渲染。"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, r"C:\APP\deepseek\work\定时备份及其压缩项目")

from PySide6.QtWidgets import QApplication, QComboBox, QVBoxLayout, QWidget

app = QApplication([])
from backupstudio.gui import LIGHT, stylesheet
from backupstudio.gui.styles import set_current

set_current("glass")
app.setStyleSheet(stylesheet(LIGHT))

from backupstudio.gui.widgets import Card


def probe(card: Card, tag: str) -> None:
    cb = card.findChild(QComboBox)
    img = card.grab().toImage()
    # 取下拉框中心（相对卡片）
    from PySide6.QtCore import QPoint

    p = cb.mapTo(card, QPoint(0, 0))
    c = img.pixelColor(p.x() + cb.width() // 2, p.y() + cb.height() // 2)
    e = img.pixelColor(p.x() + 1, p.y() + cb.height() // 2)
    print(f"  {tag:<40} 中心={c.name()} 左边={e.name()}")


# ① Card 不上玻璃（无内联样式）
host1 = QWidget()
l1 = QVBoxLayout(host1)
c1 = Card()
cb1 = QComboBox()
cb1.addItems(["浅色", "深色"])
c1.body().addWidget(cb1)
l1.addWidget(c1)
host1.resize(400, 200)
host1.show()
app.processEvents()
probe(c1, "① Card 无玻璃（无内联样式）")

# ② Card 上玻璃（有内联样式）
host2 = QWidget()
l2 = QVBoxLayout(host2)
c2 = Card()
cb2 = QComboBox()
cb2.addItems(["浅色", "深色"])
c2.body().addWidget(cb2)
l2.addWidget(c2)
host2.resize(400, 200)
host2.move(0, 220)
host2.show()
app.processEvents()
c2.set_glass(True, radius=14, tint=LIGHT.surface, alpha=0.2, dark=False)
app.processEvents()
probe(c2, "② Card 上玻璃（有内联样式）")
print(f"     Card 内联样式 = {c2.styleSheet()!r}")

# ③ 上玻璃后手动清掉内联样式，只留 _glass_on
c2.setStyleSheet("")
app.processEvents()
probe(c2, "③ 清掉内联样式但 _glass_on=True")

# ④ 直接看 cb2 的 palette 与是否 WA_StyledBackground
from PySide6.QtCore import Qt

print(f"\n  cb2 autoFillBackground = {cb2.autoFillBackground()}")
print(f"  cb2 WA_StyledBackground = "
      f"{cb2.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)}")
pal = cb2.palette()
print(f"  cb2 palette Base = {pal.color(pal.ColorRole.Base).name()}")
print(f"  cb2 palette Window = {pal.color(pal.ColorRole.Window).name()}")
