"""液态玻璃「水感」重做（1.1.7）—— 用户反馈"没有那么透，没有水一样的感觉"。

## 实测诊断

当前参数与其问题：

    alpha = 0.20            tint 很淡 —— 这条是对的
    p.setOpacity(0.92)      模糊层 92% 不透明 —— **这是主因**
    tint = #FFFFFF          纯白 tint
    highlight 未传入        TitleBar 之外没设高光强度

叠加结果：模糊背景（92% 不透明）→ 再叠白色 20% → 磨砂白板。
视觉上「糊住了」而不是「透过去」。

## 真玻璃（Liquid Glass）该有的四条

1. **更透明**：模糊层不透明度从 0.92 降到 ~0.55，让背后的光斑/内容真正透上来
2. **更强折射**：边缘位移量加大（strength 0.6 → 1.0），才看得出"弯折"
3. **玻璃厚度感**：内高光边 + 顶部亮线 + 底部暗线（模拟玻璃截面）
4. **色调随背景**：tint 不用纯白，而用 surface 的低饱和版本，避免"糊白"

## 另外：模糊范围要限制在卡片内

现在是把整窗渲染到 pixmap 再裁，模糊半径相对整窗太小。
改为对卡片区域单独采样后模糊，半径按卡片尺寸比例算。
"""
from pathlib import Path

W = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\widgets.py")
s = W.read_text(encoding="utf-8")

# ── 1. 模糊层不透明度：0.92 → 0.55 ──
old = """        p.setOpacity(0.92)
        p.drawPixmap(0, 0, QPixmap.fromImage(img))
        p.setOpacity(1.0)"""
new = """        # **玻璃的"透"**：模糊层不透明度从 0.92 降到 0.55。
        # 0.92 等于把背景糊成实心，再叠 tint 就是磨砂白板 ——
        # 用户反馈"没有那么透、没有水一样的感觉"的根因。
        p.setOpacity(0.55)
        p.drawPixmap(0, 0, QPixmap.fromImage(img))
        p.setOpacity(1.0)"""
if old in s:
    s = s.replace(old, new, 1)
    print("1) 模糊层不透明度 0.92 → 0.55")
else:
    print("1) 未匹配 setOpacity")

# ── 2. tint 不透明度：0.20 → 0.10（更透）──
# 由 main_window._install_glass 传入，改那里
M = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\main_window.py")
m = M.read_text(encoding="utf-8")
old2 = """        alpha = 0.20 if not palette.dark else 0.26"""
new2 = """        # 更透：0.20 → 0.10（深色 0.14）。玻璃要"看得穿"而不是"糊一层白"。
        alpha = 0.10 if not palette.dark else 0.14"""
if old2 in m:
    m = m.replace(old2, new2, 1)
    M.write_text(m, encoding="utf-8", newline="")
    print("2) tint alpha 0.20 → 0.10")
else:
    print("2) 未匹配 alpha")

# ── 3. 折射强度加大 0.6 → 1.0 ──
old3 = """            small = displace(small, radius=16, strength=0.6,
                             edge=max(6, min(small.width(), small.height()) // 4))"""
new3 = """            # 折射加大：0.6 → 1.0，边缘弯折更明显（"水"的感觉来自这里）
            small = displace(small, radius=18, strength=1.0,
                             edge=max(8, min(small.width(), small.height()) // 3))"""
if old3 in s:
    s = s.replace(old3, new3, 1)
    print("3) 折射强度 0.6 → 1.0")
else:
    print("3) 未匹配 displace")

W.write_text(s, encoding="utf-8", newline="")
import py_compile
py_compile.compile(str(W), doraise=True)
py_compile.compile(str(M), doraise=True)
print("4) 语法 OK")
