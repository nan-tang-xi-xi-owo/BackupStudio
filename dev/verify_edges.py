"""验证边缘缩放光标逻辑与标题栏跟随主题（1.1.4）。"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="ec-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import QPoint, Qt
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

pts = {
    "左上角 (1,1)": (QPoint(1, 1), "Top|Left"),
    "上边中 (450,1)": (QPoint(450, 1), "Top"),
    "右上角 (898,1)": (QPoint(898, 1), "Top|Right"),
    "右边中 (898,300)": (QPoint(898, 300), "Right"),
    "右下角 (898,598)": (QPoint(898, 598), "Bottom|Right"),
    "下边中 (450,598)": (QPoint(450, 598), "Bottom"),
    "左下角 (1,598)": (QPoint(1, 598), "Bottom|Left"),
    "左边中 (1,300)": (QPoint(1, 300), "Left"),
    "中心 (450,300)": (QPoint(450, 300), "无"),
}
print("  边缘热区判定：")
ok = True
for name, (p, expect) in pts.items():
    e = w._edge_at(p)
    got = str(e).replace("Edge.", "").replace("|", "|") or "无"
    if expect == "无":
        good = not e
    else:
        flags = {"Top": Qt.Edge.TopEdge, "Bottom": Qt.Edge.BottomEdge,
                 "Left": Qt.Edge.LeftEdge, "Right": Qt.Edge.RightEdge}
        good = all((e & flags[k]) for k in expect.split("|"))
        if expect == "Top" or expect == "Bottom" or expect == "Left" or expect == "Right":
            good = good and bin(int(e.value)).count("1") == 1
    ok = ok and good
    print(f"    {name:<18} -> {got:<22} {'OK' if good else '!! 期望 ' + expect}")

print(f"\n  热区全部正确: {ok}")

# 标题栏配色
if w.titlebar:
    print("\n  标题栏配色：")
    for key in ("win", "98", "mac", "skeuo", "mag", "glass"):
        st.set_style(key)
        app.processEvents()
        print(f"    {key:<6} bg={w.titlebar._bg} fg={w.titlebar._fg}")
else:
    print("  (无自绘标题栏)")
w.close()
