"""外观风格（UI style）注册表 —— 1.1.3。

## 设计

配色与风格**正交**：`Palette` 负责颜色（light/dark/system），
`Style` 负责圆角/边框/渐变/阴影/字号/控件形态。这样 6 种风格 × 2 种配色
= 12 套组合，但只需要维护 6 份风格参数。

## 风格清单

| key | 名称 | 特征 |
|---|---|---|
| `win` | Windows 11 | 亚克力感、8px 圆角、细边框、Mica 底色 |
| `mac` | macOS | 大圆角 12px、无边框分层、柔和阴影、SF 风格字距 |
| `98` | Windows 98 | 直角、3D 凸起/凹陷边框、经典灰、无圆角 |
| `skeuo` | 拟物 | 皮革/金属质感渐变、内阴影、强立体感 |
| `mag` | Google Material | 药丸按钮、12px 圆角、明显投影、色彩块 |
| `glass` | 液态玻璃 | 半透明、背景折射、高光边、模糊感 |
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import DARK, LIGHT, Palette


@dataclass(frozen=True)
class Style:
    """一套外观风格的几何与质感参数。"""

    key: str
    name: str
    #: 卡片/按钮圆角半径（px）
    radius: int
    #: 输入框圆角
    radius_input: int
    #: 按钮圆角（Material 用大圆角做药丸）
    radius_button: int
    #: 边框宽度（px）
    border: int
    #: 是否用 3D 凸起边框（Win98/拟物）
    bevel: bool
    #: 卡片是否用渐变（拟物）
    gradient: bool
    #: 是否半透明玻璃（圆角外可见背景）
    translucent: bool
    #: 玻璃风格的高光边强度 0~1
    highlight: float
    #: 卡片阴影（Material/玻璃用）；0 = 无
    shadow: int
    #: 标题字重
    title_weight: int
    #: 轻微字距（mac 风格用负值更紧凑）
    letter_spacing: float


STYLES: dict[str, Style] = {
    "win": Style(
        key="win", name="Windows 11",
        radius=8, radius_input=6, radius_button=6, border=1,
        bevel=False, gradient=False, translucent=False,
        highlight=0.0, shadow=0, title_weight=600, letter_spacing=0.0,
    ),
    "mac": Style(
        key="mac", name="macOS",
        radius=12, radius_input=7, radius_button=8, border=1,
        bevel=False, gradient=False, translucent=False,
        highlight=0.0, shadow=6, title_weight=600, letter_spacing=-0.2,
    ),
    "98": Style(
        key="98", name="Windows 98",
        radius=0, radius_input=0, radius_button=0, border=2,
        bevel=True, gradient=False, translucent=False,
        highlight=0.0, shadow=0, title_weight=700, letter_spacing=0.0,
    ),
    "skeuo": Style(
        key="skeuo", name="拟物",
        radius=9, radius_input=6, radius_button=7, border=1,
        # 拟物风格要**同时**有立体边框和渐变（bevel 与 gradient 不互斥，
        # 见 __init__.stylesheet 里的组合分支）
        bevel=True, gradient=True, translucent=False,
        highlight=0.35, shadow=4, title_weight=700, letter_spacing=0.1,
    ),
    "mag": Style(
        key="mag", name="Google Material",
        radius=12, radius_input=8, radius_button=20, border=0,
        bevel=False, gradient=False, translucent=False,
        highlight=0.0, shadow=8, title_weight=500, letter_spacing=0.15,
    ),
    "glass": Style(
        key="glass", name="液态玻璃",
        radius=14, radius_input=9, radius_button=10, border=1,
        bevel=False, gradient=True, translucent=True,
        highlight=0.8, shadow=10, title_weight=600, letter_spacing=0.0,
    ),
}

DEFAULT_STYLE = "win"
ORDER = ["win", "mac", "98", "skeuo", "mag", "glass"]

#: 当前生效的风格（进程级）。GUI 启动时由设置页/配置初始化，
#: `stylesheet()` 每次读取它 —— 这样不用把 Style 塞进 Palette，
#: 避免所有调用点都要改签名。
_CURRENT: Style = STYLES[DEFAULT_STYLE]


def style_for(key: str | None) -> Style:
    return STYLES.get((key or "").strip().lower(), STYLES[DEFAULT_STYLE])


def current() -> Style:
    return _CURRENT


def set_current(key: str | None) -> Style:
    global _CURRENT
    _CURRENT = style_for(key)
    return _CURRENT


def style_names() -> list[tuple[str, str]]:
    """[(key, 显示名)]，按展示顺序。"""
    return [(k, STYLES[k].name) for k in ORDER]


# ---------------------------------------------------------------------------
# 风格化的调色板：不同风格对同一配色做微调
# ---------------------------------------------------------------------------
def styled_palette(p: Palette, key: str) -> Palette:
    """按风格微调配色（保留 light/dark 语义，只改质感相关字段）。

    * **98**：经典灰底、锐利边框，去掉现代柔和感
    * **拟物**：底色带暖调、边框加深做立体
    * **Material**：纯白卡 + 更明显的分隔
    * **玻璃**：底色更透、边框用高光色
    """
    s = style_for(key)
    if s.key == "98":
        if p.dark:
            return replace(p, bg="#2B2B2B", surface="#3C3C3C", surface_alt="#4A4A4A",
                           border="#1F1F1F", border_strong="#141414")
        return replace(p, bg="#C0C0C0", surface="#D4D0C8", surface_alt="#E4E0D8",
                       border="#808080", border_strong="#404040")
    if s.key == "skeuo":
        if p.dark:
            return replace(p, bg="#1D1A17", surface="#2A2622", surface_alt="#35302B",
                           border="#4A423A", border_strong="#5C5248")
        return replace(p, bg="#EFE9E0", surface="#FAF6F0", surface_alt="#F2ECE3",
                       border="#D6CCBE", border_strong="#BCAE9C")
    if s.key == "mag":
        if p.dark:
            return replace(p, bg="#121212", surface="#1E1E1E", surface_alt="#2A2A2A",
                           border="#2E2E2E", border_strong="#3A3A3A")
        return replace(p, bg="#F5F5F5", surface="#FFFFFF", surface_alt="#EFEFEF",
                       border="#E0E0E0", border_strong="#CFCFCF")
    if s.key == "glass":
        if p.dark:
            return replace(p, bg="#0E1018", surface="#1A1E2B", surface_alt="#232838",
                           border="#3A4258", border_strong="#4E5872")
        return replace(p, bg="#E6ECF7", surface="#F7FAFF", surface_alt="#EDF2FB",
                       border="#C8D4E8", border_strong="#AFC0DA")
    return p


def light_of(key: str) -> Palette:
    return styled_palette(LIGHT, key)


def dark_of(key: str) -> Palette:
    return styled_palette(DARK, key)
