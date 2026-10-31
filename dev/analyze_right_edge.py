"""逐像素分析用户截图右边缘，判断那条"不协调的竖条"是什么。"""
from pathlib import Path

from PySide6.QtGui import QImage

SRC = Path(r"C:\Users\Admin\.dsh\attachments\v1\objects\c2"
           r"\c22ea35edf5f783381508ea3d504dabdb3e990f8dd167c108c12597573a42981")
img = QImage(str(SRC))
W, H = img.width(), img.height()
print(f"  图 {W}x{H}")

print("\n  水平扫描（y=300，从右往左 30px）：")
for x in range(W - 1, W - 31, -1):
    c = img.pixelColor(x, 300)
    print(f"    x={x:>4}  {c.name()}  rgb({c.red()},{c.green()},{c.blue()})")

print("\n  检查右侧竖条在不同 y 处是否一致：")
for y in (100, 200, 300, 400, 500, 600):
    c = img.pixelColor(W - 8, y)
    c2 = img.pixelColor(W - 20, y)
    print(f"    y={y:>3}  x=W-8: {c.name()}   x=W-20: {c2.name()}")
