"""把 Material You 动态配色接入 mag 风格（1.1.3）。"""
from pathlib import Path

# ── 1. styles.py：mag 风格改用 mcu.scheme() ──
S = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\styles.py")
s = S.read_text(encoding="utf-8")

old = """    if k == "mag":
        if p.dark:
            return replace(p, bg="#121212", surface="#1E1E1E", surface_alt="#2A2A2A",
                           border="#2E2E2E", border_strong="#3A3A3A",
                           accent="#8AB4F8", accent_hover="#A5C6FA", accent_text="#121212",
                           text="#E8EAED", text_dim="#9AA0A6",
                           selection="#39445A", nav_active="#2A3444")
        return replace(p, bg="#F8F9FA", surface="#FFFFFF", surface_alt="#F1F3F4",
                       border="#E8EAED", border_strong="#DADCE0",
                       accent="#1A73E8", accent_hover="#1765CC", accent_text="#FFFFFF",
                       text="#202124", text_dim="#5F6368",
                       selection="#E8F0FE", nav_active="#E8F0FE")"""

new = '''    if k == "mag":
        # **Material You 动态配色**（1.1.3）：由单一源色生成 65 色体系
        # （5 板 × 13 档），再按文档的颜色角色映射到 UI。
        # 源色与 theme_style 从设置读取，用户可换色。
        from .mcu import scheme

        _seed = getattr(_MAG_CONF, "seed", None) or "#6750A4"
        _style = getattr(_MAG_CONF, "theme_style", None) or "TONAL_SPOT"
        sc = scheme(_seed, p.dark, _style)
        # surface_alt 用 neutral2 的高一档做"表面容器"（M3 的 surface-container）
        _alt = sc["surface_variant"]
        return replace(
            p,
            bg=sc["background"], surface=sc["surface"], surface_alt=_alt,
            border=sc["surface_variant"], border_strong=sc["on_surface_variant"],
            accent=sc["primary"], accent_hover=sc["primary"],
            accent_text=sc["on_primary"],
            text=sc["on_surface"], text_dim=sc["on_surface_variant"],
            selection=sc["surface_variant"], nav_active=sc["surface_variant"],
        )'''

if old in s:
    s = s.replace(old, new, 1)
    print("1) mag 已接入 Material You 动态配色")
else:
    print("1) 未匹配 mag 分支")

# 加一个配置容器
if "_MAG_CONF" not in s:
    s = s.replace(
        "_CURRENT: Style = STYLES[DEFAULT_STYLE]",
        '''_CURRENT: Style = STYLES[DEFAULT_STYLE]


class _MagConf:
    """Material You 的运行时配置（种子色 + 主题风格）。"""

    seed: str = "#6750A4"
    theme_style: str = "TONAL_SPOT"


_MAG_CONF = _MagConf()


def set_material_you(seed: str | None = None, theme_style: str | None = None) -> None:
    """设置 Material You 的源色与风格（Android 13+ 的 TONAL_SPOT/VIBRANT/EXPRESSIVE/SPRITZ）。"""
    from .mcu import normalize_seed

    if seed is not None:
        _MAG_CONF.seed = normalize_seed(seed)
    if theme_style:
        _MAG_CONF.theme_style = theme_style.upper()''',
        1,
    )
    print("2) 已加 Material You 配置与 set_material_you()")

S.write_text(s, encoding="utf-8", newline="")
import py_compile
py_compile.compile(str(S), doraise=True)
print("3) 语法 OK")
