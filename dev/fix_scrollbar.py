"""滚动条透明化，避免在玻璃风格下成为一条"脏边"（1.1.6）。

## 用户反馈"边缘不协调"的实证

逐像素分析用户截图右缘（y=300）：

    x=1146~1144  #A0DCEB   窗口外桌面
    x=1143~1137  #AFC0DA   ← **7px 宽的滚动条 handle**
    x=1136~1134  #9FDCEB   窗口外桌面
    x=1133~1117  #E6ECF7   窗口背景

滚动条紧贴窗口右缘（只隔 1~2px），而 `border_strong #AFC0DA` 比背景
`#E6ECF7` 深得多 → 视觉上是一条突兀的深色竖条。

## 修法

1. 滚动条更窄（10px → 6px）、handle 更圆、与边缘留 3px 间距
2. handle 用**半透明**（用 `rgba()`），让它融入玻璃底色而不是硬邦邦的实色
3. 玻璃风格下进一步降低 alpha
"""
from pathlib import Path

P = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\__init__.py")
s = P.read_text(encoding="utf-8")

old_v = """    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}"""
new_v = """    /* 滚动条（1.1.6）：更窄、handle 半透明、与窗口边缘留间距。
       原来 10px + 实色 border_strong 在玻璃风格下是突兀的深色竖条
       （用户反馈"边缘不协调"经逐像素证实就是这个）。 */
    QScrollBar:vertical {{
        background: transparent; width: 8px;
        margin: 4px 3px 4px 0;
    }}"""
if old_v in s:
    s = s.replace(old_v, new_v, 1)
    print("1) 竖向滚动条已收窄")

old_h = """    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}"""
new_h = """    QScrollBar:horizontal {{
        background: transparent; height: 8px;
        margin: 0 4px 3px 4px;
    }}"""
if old_h in s:
    s = s.replace(old_h, new_h, 1)
    print("2) 横向滚动条已收窄")

# handle 改半透明 + 更圆
old_hv = """    QScrollBar::handle:vertical {{ background: {p.border_strong}; border-radius: 5px; min-height: 28px; }}"""
new_hv = """    QScrollBar::handle:vertical {{
        background: rgba({_rgb(p.border_strong)}, 0.55);
        border-radius: 3px; min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: rgba({_rgb(p.text_dim)}, 0.75);
    }}"""
if old_hv in s:
    s = s.replace(old_hv, new_hv, 1)
    print("3) 竖向 handle 已改半透明")

old_hh = """    QScrollBar::handle:horizontal {{ background: {p.border_strong}; border-radius: 5px; min-width: 28px; }}"""
new_hh = """    QScrollBar::handle:horizontal {{
        background: rgba({_rgb(p.border_strong)}, 0.55);
        border-radius: 3px; min-width: 28px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: rgba({_rgb(p.text_dim)}, 0.75);
    }}"""
if old_hh in s:
    s = s.replace(old_hh, new_hh, 1)
    print("4) 横向 handle 已改半透明")

# 需要 _rgb 辅助函数（把 #RRGGBB 转 "r, g, b"）
if "def _rgb(" not in s.split("def stylesheet")[1][:4000]:
    anchor = "    def _hex2rgb(h: str) -> str:"
    if anchor in s:
        print("5) _hex2rgb 已存在，复用")
    else:
        # 在 _lighter/_darker 定义处加一个 _rgb
        old_mix = "    def _lighter(h: str, ratio: float = 0.35) -> str:"
        new_mix = '''    def _rgb(h: str) -> str:
        """'#RRGGBB' → 'r, g, b'（供 rgba() 用）。"""
        h = h.lstrip("#")
        return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"

    def _lighter(h: str, ratio: float = 0.35) -> str:'''
        if old_mix in s:
            s = s.replace(old_mix, new_mix, 1)
            print("5) 已加 _rgb 辅助函数")

P.write_text(s, encoding="utf-8", newline="")
import py_compile
py_compile.compile(str(P), doraise=True)
print("6) 语法 OK")
