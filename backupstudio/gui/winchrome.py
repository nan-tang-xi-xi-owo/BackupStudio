"""原生标题栏的深色适配（1.1.4 补充）—— DWM 探测报告的实用结论。

## 依据（实测，两条独立路径结论一致）

    DWMWA_USE_IMMERSIVE_DARK_MODE (20)  S_OK          生效（白→黑，ΔL=-255）
    DWMWA_CAPTION_COLOR (35)            E_INVALIDARG  不可用
    DWMWA_BORDER_COLOR (34)             E_INVALIDARG  不可用
    DWMWA_TEXT_COLOR (36)               E_INVALIDARG  不可用
    DWMWA_CORNER_PREFERENCE (33)        E_INVALIDARG  不可用

另有一条**容易踩的坑**（报告实测 3/3 复现）：

> 运行期把 attr20 从 0 改成 1，仅调用 DwmSetWindowAttribute **不会重绘**
> （3 次全部仍是白色）。`ShowWindow(SW_HIDE)`→`ShowWindow(SW_SHOW)` 是
> 最可靠的强制刷新（3/3），改 1px 尺寸也可（3/3）。

## 为什么需要它

自绘标题栏（`prefer_frameless()`=True）时系统标题栏被去掉，DWM 属性无关；
但以下两种情况仍有系统标题栏，深色主题下它会保持白色：

* `BACKUPSTUDIO_FRAMELESS=0`（排查用开关）
* 未来若在非 Windows 上不使用无边框

所以补一个「原生标题栏深色」适配，**只在有系统标题栏时调用**。
"""
from __future__ import annotations

import sys

#: DWMWA_USE_IMMERSIVE_DARK_MODE
#: **必须用 20**：19 是 1809 时期的索引，在 19044 上返回 E_INVALIDARG（实测）。
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20


def apply_native_dark_titlebar(hwnd: int, dark: bool) -> bool:
    """让**系统**标题栏切到深色/浅色（仅 Windows）。

    返回 True 表示属性设置成功。注意：运行期切换时 DWM 可能不重绘，
    调用方应在窗口已隐藏或即将重建时调用，或接受 reset_signal 带来的闪烁。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import ctypes.wintypes as wt

        dwm = ctypes.windll.dwmapi
        val = ctypes.c_int(1 if dark else 0)
        hr = dwm.DwmSetWindowAttribute(
            wt.HWND(hwnd),
            ctypes.c_uint(_DWMWA_USE_IMMERSIVE_DARK_MODE),
            ctypes.byref(val),
            ctypes.sizeof(val),
        )
        return hr == 0
    except Exception:  # noqa: BLE001
        return False


def force_native_repaint(hwnd: int) -> None:
    """强制 DWM 重绘非客户区（运行期切换深色后必须调用）。

    报告实测的可靠手段是 hide→show（3/3），但会闪烁；
    这里用「改 1px 尺寸再改回」（同样 3/3），闪烁更轻。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        rect = wt.RECT()
        if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
            return
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        SWP_NOMOVE = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        flags = SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
        user32.SetWindowPos(wt.HWND(hwnd), None, 0, 0, w + 1, h, flags)
        user32.SetWindowPos(wt.HWND(hwnd), None, 0, 0, w, h, flags)
    except Exception:  # noqa: BLE001
        pass
