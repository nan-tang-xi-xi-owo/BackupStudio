"""放大用户截图的关键边缘，定位"不协调"的具体位置。"""
from pathlib import Path

from PySide6.QtGui import QImage

SRC = Path(r"C:\Users\Admin\.dsh\attachments\v1\objects\c2"
           r"\c22ea35edf5f783381508ea3d504dabdb3e990f8dd167c108c12597573a42981")
OUT = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\docs\screenshots")
OUT.mkdir(parents=True, exist_ok=True)

img = QImage(str(SRC))
print(f"  源图 {img.width()}x{img.height()}")

regions = {
    "corner-top-left": (0, 0, 120, 120),
    "corner-top-right": (img.width() - 120, 0, 120, 120),
    "corner-bottom-left": (0, img.height() - 120, 120, 120),
    "edge-left": (0, 200, 90, 260),
    "edge-right": (img.width() - 90, 200, 90, 260),
    "edge-bottom": (300, img.height() - 70, 400, 70),
    "sidebar-main-seam": (150, 220, 140, 200),
    "titlebar": (0, 0, img.width(), 46),
}
for name, (x, y, w, h) in regions.items():
    x = max(0, min(x, img.width() - 1))
    y = max(0, min(y, img.height() - 1))
    w = min(w, img.width() - x)
    h = min(h, img.height() - y)
    crop = img.copy(x, y, w, h)
    # 放大 3 倍便于观察
    big = crop.scaled(w * 3, h * 3)
    p = OUT / f"edge-{name}.png"
    big.save(str(p))
    print(f"    {name:<20} 从 ({x},{y}) {w}x{h} → {p.name}")
