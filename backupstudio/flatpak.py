"""Flatpak 沙箱探测与能力边界（审查 N3 / N4）。

为什么需要这个模块
------------------
Flatpak 的沙箱有三条「看起来能用、实际不生效」的陷阱，本程序此前全中：

1. **自启动写进沙箱内的 ``~/.config/autostart``**（N3）
   Flatpak 里 ``$HOME`` 被重定向到 ``~/.var/app/<app-id>/``，写进去的
   ``.desktop`` 宿主桌面环境**永远看不到**。设置页却提示「已写入，下次登录生效」
   → 用户永远不会得到开机自启，而且没有任何报错。

2. **守护进程与 GUI 不是同一个沙箱实例**（N4）
   文件门户（xdg-desktop-portal）的授权绑定在**发起请求的那个进程/沙箱实例**上。
   ``spawn_daemon()`` 起的是一个独立进程，不继承 GUI 拿到的门户授权；
   于是「手动备份成功、定时备份必失败」，而且失败发生在用户看不见的后台。

3. **配置目录被强制指向不可写路径**（N2，manifest 侧修复）
   manifest 里的 ``--env=BACKUPSTUDIO_CONFIG_DIR=/var/config/backupstudio``
   让程序每次都回退到 tmpfs，重启即丢配置。

本模块只做**检测与如实告知**，不假装支持沙箱做不到的能力。
"""

from __future__ import annotations

import configparser
import os
import sys
from functools import lru_cache
from pathlib import Path

from . import APP_ID

# 沙箱标记文件：flatpak 在每个沙箱内固定提供（内容是 ini 格式的实例信息）
FLATPAK_INFO = Path("/.flatpak-info")

# 门户总线名（N4：GUI 需要它才能在 home 之外用文件选择器）
PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"


def _parse_info_file(path: Path) -> dict[str, str]:
    """把 ``/.flatpak-info`` 拍平成 ``{"section/key": value}``（解析失败返回空）。

    用 configparser 而不是手写解析：该文件是标准 ini（含重复的 key，
    如 ``[Session Bus Policy]`` 下多条），configparser 的 strict=False 能容忍。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.strip():
        return {}
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # 保留大小写：总线名对大小写敏感
    try:
        parser.read_string(text)
    except configparser.Error:
        # 极端情况（被截断/编码异常）：退化为逐行 key=value，仍然尽量给出答案
        flat: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if line and not line.startswith(("[", "#", ";")) and "=" in line:
                key, _, value = line.partition("=")
                flat.setdefault(key.strip(), value.strip())
        return flat
    flat: dict[str, str] = {}
    for section in parser.sections():
        for key, value in parser.items(section):
            flat[f"{section}/{key}"] = value
    return flat


@lru_cache(maxsize=1)
def sandbox_info() -> dict[str, str]:
    """返回当前进程所处的 Flatpak 沙箱信息（不在沙箱内返回空 dict）。"""
    info = _parse_info_file(FLATPAK_INFO)
    if info:
        return info
    # 兜底：部分工具链/测试环境只有环境变量、没有标记文件
    if os.environ.get("FLATPAK_ID"):
        return {"Application/name": os.environ["FLATPAK_ID"], "Instance/instance-id": "env"}
    return {}


def is_flatpak() -> bool:
    """当前进程是否运行在 Flatpak 沙箱内。"""
    return bool(sandbox_info())


def app_id() -> str:
    """Flatpak 应用 ID（不在沙箱内时回退到本项目的 APP_ID）。"""
    return str(sandbox_info().get("Application/name") or APP_ID)


def granted_filesystems() -> list[str]:
    """沙箱实际拿到的文件系统授权（``[Context] filesystems=``）。"""
    raw = str(sandbox_info().get("Context/filesystems") or "")
    return [item for item in raw.split(";") if item]


def portal_declared() -> bool:
    """沙箱是否**显式**声明了文件门户授权（``--talk-name=org.freedesktop.portal.Desktop``）。

    只有 ``/.flatpak-info`` 的 ``[Session Bus Policy]`` / ``[System Bus Policy]``
    里把该总线写成 talk/see/own 时才算「显式声明」。
    注意：Flatpak 默认会代理门户（portal 是官方推荐的访问外部文件的通道），
    所以「未显式声明」**不等于**门户一定不可用——本函数只回答「manifest 里写了没」。
    不在沙箱内时返回 True（普通安装没有这层限制）。
    """
    info = sandbox_info()
    if not info:
        return True
    for key, value in info.items():
        if "/" not in key:
            continue
        section, _, bus = key.partition("/")
        if section not in ("Session Bus Policy", "System Bus Policy"):
            continue
        if bus == PORTAL_BUS_NAME and value in ("talk", "own", "see"):
            return True
    return False



# --------------------------------------------------------------------------
# 能力边界（如实告知，不做假支持）
# --------------------------------------------------------------------------

def autostart_supported() -> bool:
    """Flatpak 版能否真正设置开机自启。

    **不能**：写进沙箱的 XDG autostart 文件宿主看不见。这不是配置问题，
    是 Flatpak 的安全模型决定的（除非用 ``--filesystem=xdg-config/autostart``
    越权，那是把宿主的自启动目录整目录暴露给沙箱，代价过大，不做）。
    """
    return not is_flatpak()


def autostart_hint() -> str:
    """给用户的替代方案（可直接展示在界面/CLI）。"""
    app = app_id()
    return (
        "Flatpak 版暂不支持在程序内设置开机自启："
        "沙箱内的 ~/.config/autostart 宿主桌面环境看不到。\n"
        f"请改用系统设置添加自启命令：flatpak run {app} start --minimized"
    )


def daemon_supported() -> bool:
    """Flatpak 版能否使用独立的守护进程。

    **不建议**：守护进程是独立进程，不继承 GUI 通过文件门户拿到的授权，
    home 之外的备份会在后台静默失败（手动能备份、定时不备份）。
    沙箱内请使用 GUI 内置调度器（``AppState.start_scheduler``）。
    """
    return not is_flatpak()


def daemon_hint() -> str:
    app = app_id()
    return (
        "Flatpak 版请使用应用内置调度器：保持程序运行（可最小化到托盘）即可按计划备份。\n"
        "独立的守护进程不会继承文件门户授权，home 之外的目录会备份失败，"
        "因此本版本在沙箱内不再启动子进程守护进程。\n"
        f"若需要关闭窗口后仍执行计划，可把程序加入会话自启：flatpak run {app} start --minimized"
    )


def portal_hint() -> str:
    return (
        f"当前 Flatpak 未获得文件门户权限（{PORTAL_BUS_NAME}），"
        "文件选择器与 home 之外的目录可能无法使用。\n"
        "请安装包含该权限的新版本，或临时把要备份的目录放到 ~/ 下。"
    )


def describe() -> list[tuple[str, str]]:
    """环境页/CLI ``info`` 用的沙箱摘要（不在沙箱内返回空列表）。"""
    if not is_flatpak():
        return []
    declared = portal_declared()
    rows: list[tuple[str, str]] = [
        ("运行沙箱", f"Flatpak ({app_id()})"),
        ("文件系统授权", " ".join(granted_filesystems()) or "-"),
        (
            "文件门户",
            "manifest 已声明" if declared else "manifest 未声明（依赖 Flatpak 默认代理）",
        ),
        ("开机自启", "不支持（见设置页说明）" if not autostart_supported() else "支持"),
        ("守护进程", "改用内置调度器" if not daemon_supported() else "支持"),
    ]
    return rows


def reset_cache() -> None:
    """测试用：清空沙箱探测缓存。"""
    sandbox_info.cache_clear()


def python_is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
