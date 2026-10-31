"""量化玻璃"透不透"：卡片区域与背后光斑的亮度/色相差。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="tr-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui import stylesheet
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
ts.settings.ui_style = "glass"
ts.save()
st = AppState(ts)
app.setStyleSheet(stylesheet(st.palette))
w = MainWindow(st)
w.resize(1000, 680)
w.show()
for _ in range(14):
    app.processEvents()

img = w.grab().toImage()
W, H = img.width(), img.height()
print(f"  窗口截图 {W}x{H}（物理像素）")

# 采样卡片区域 vs 旁边背景区域
samples = []
for y in range(120, H - 120, 40):
    row = []
    for x in range(200, W - 200, 40):
        c = img.pixelColor(x, y)
        row.append((c.red(), c.green(), c.blue()))
    samples.append(row)

# 统计色彩饱和度分布 —— 玻璃下有彩光时应该有明显色相变化
import colorsys

sats = []
for row in samples:
    for r, g, b in row:
        mx, mn = max(r, g, b), min(r, g, b)
        sats.append((mx - mn) / mx if mx else 0)

avg_sat = sum(sats) / len(sats)
print(f"  平均饱和度 = {avg_sat:.3f}  （玻璃若透出彩色光斑应 > 0.06）")

# 直接看 Card 的 paint 参数
from backupstudio.gui.widgets import Card

cards = [c for c in w.findChildren(Card) if c._glass_on]
print(f"\n  已开玻璃的卡片 {len(cards)} 张")
for c in cards[:3]:
    print(f"    alpha={c._glass_alpha}  radius={c._glass_radius}  "
          f"tint={c._glass_tint}  highlight={getattr(c,'_glass_highlight','未设')}")

# 关键：检查 paintEvent 里 drawPixmap 的不透明度
from pathlib import Path as _P

src = _P(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\widgets.py").read_text(encoding="utf-8")
i = src.find("p.setOpacity(")
while i != -1 and i < len(src):
    line = src[i:src.find("\n", i)]
    print(f"    {line.strip()}")
    i = src.find("p.setOpacity(", i + 1)
w.close()
