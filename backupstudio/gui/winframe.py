"""路线 C：原生框架位 + NCCALCSIZE 归零 + NCHITTEST 分区（1.1.5）。

## 为什么从路线 B 升级到 C

路线 B（纯 Frameless）实测结果（本机 Win10 19044）：

    窗口样式 0x960B0000  → 无 WS_CAPTION / WS_THICKFRAME
    GetSystemMenu       → 7 项（**没丢**，与 Agent 对裸 WS_POPUP 的结论不同，
                           Qt 的 Frameless 保留了 SYSMENU）
    WM_NCHITTEST        → 全部返回 HTCLIENT (1)
                          ⇒ **没有系统级边缘缩放、没有原生 Snap 判定**

路线 C 的做法（本模块实现）：

1. 保留 Qt 的 Frameless（不要系统绘制的标题栏）；
2. 用 `SetWindowLongW` 把 `WS_CAPTION|WS_THICKFRAME|WS_SYSMENU|
   WS_MINIMIZEBOX|WS_MAXIMIZEBOX` **OR 回去**，让系统认为这是普通可缩放窗口
   → 系统菜单、Snap、DWM 阴影判定都恢复；
3. 子类化窗口过程，把 `WM_NCCALCSIZE` 的客户区设为**整个窗口矩形**
   （系统不画标题栏，我们画）—— 注意 `wParam=TRUE` 时返回 0 即可；
4. `WM_NCHITTEST` 自己分区：
   * 边缘 6px → `HTLEFT/HTRIGHT/HTTOP/HTBOTTOM/HTTOPLEFT/...`
     **边缘判定必须优先于标题带**（否则顶边被当成 HTCAPTION 就拖不了）
   * 标题带（y < 标题栏高）→ `HTCAPTION`
   * 其余 → `HTCLIENT`
5. `WM_NCLBUTTONDBLCLK` 在 HTCAPTION 上系统不再自动最大化（NCCALCSIZE 归零后），
   必须自己处理 —— 同时注意 Qt 的 `mouseDoubleClickEvent` 收不到。
6. `DwmExtendFrameIntoClientArea(MARGINS(1,1,1,1))` 找回被 NCCALCSIZE 弄丢的阴影。

## DPI 陷阱（Agent 实测踩到，我也遵守）

win32 消息 `lParam` 里是**物理像素**，Qt 的 `mapFromGlobal` 要**逻辑像素**。
本机 dpr=1.25，必须先除以 dpr 再映射，否则命中点整体偏移 25%
（表现为「拖不动 / 双击没反应 / 边缘抓不到」）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys

# ── Win32 常量 ────────────────────────────────────────────────────
GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000

#: 需要恢复的原生框架位
FRAME_BITS = WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDBLCLK = 0x00A3
WM_NCLBUTTONDOWN = 0x00A1
WM_NCRBUTTONUP = 0x00A5

HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

#: 边缘缩放热区（物理像素；乘 dpr 后使用）
RESIZE_BORDER = 6


class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [
        ("rgrc", wt.RECT * 3),
        ("lppos", ctypes.c_void_p),
    ]


def _is_windows() -> bool:
    return sys.platform == "win32"


class FramelessChrome:
    """给一个 Qt 顶层窗口装上「原生框架 + 自绘外观」。

    用法（Windows）：
        self._chrome = FramelessChrome(self, title_height=34)
        self._chrome.install()

    之后窗口的 WM_NCCALCSIZE / WM_NCHITTEST / WM_NCLBUTTONDBLCLK 都由本类处理，
    Qt 侧只需在 `titlebar` 里画像素。
    """

    def __init__(self, window, title_height: int = 34) -> None:  # noqa: ANN001
        self.win = window
        self.title_h = title_height
        self._old_proc = None
        self._hwnd: int | None = None
        self._new_proc = None
        self._installed = False
        #: 双击标题栏时的回调（由 MainWindow 注入，避免依赖具体类）
        self.on_toggle_max = None

    # ── 安装 ──
    def install(self) -> bool:
        if not _is_windows():
            return False
        try:
            import win32con  # noqa: F401  (不存在时直接走 ctypes 路径)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._hwnd = int(self.win.winId())
            self._restore_frame_bits()
            self._subclass()
            self._extend_frame_for_shadow()
            self._installed = True
            return True
        except Exception:  # noqa: BLE001
            return False

    def _restore_frame_bits(self) -> None:
        """把原生框架位 OR 回去，让系统恢复 Snap / 阴影 / 系统菜单判定。"""
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(wt.HWND(self._hwnd), GWL_STYLE)
        user32.SetWindowLongW(wt.HWND(self._hwnd), GWL_STYLE, style | FRAME_BITS)
        # 让系统重新计算框架
        SWP_FRAMECHANGED = 0x0020
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(
            wt.HWND(self._hwnd), None, 0, 0, 0, 0,
            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )

    def _extend_frame_for_shadow(self) -> None:
        """恢复被 NCCALCSIZE 弄丢的 DWM 阴影（实测 HRESULT=0）。"""
        try:
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                wt.HWND(self._hwnd), ctypes.byref(MARGINS(1, 1, 1, 1))
            )
        except Exception:  # noqa: BLE001
            pass

    def _subclass(self) -> None:
        """子类化窗口过程，接管 NCCALCSIZE / NCHITTEST / NCLBUTTONDBLCLK。"""
        user32 = ctypes.windll.user32
        # 64 位下必须用 SetWindowLongPtrW，且参数与返回值都是指针宽度。
        # 关键：必须显式声明 argtypes/restype，否则 ctypes 按 int 处理会
        # 抛 OverflowError: int too long to convert（实测踩到）。
        set_ptr = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        get_ptr = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        set_ptr.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        set_ptr.restype = ctypes.c_void_p
        get_ptr.argtypes = [wt.HWND, ctypes.c_int]
        get_ptr.restype = ctypes.c_void_p

        LGWLP_WNDPROC = -4
        prototype = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
        )
        old = get_ptr(wt.HWND(self._hwnd), LGWLP_WNDPROC)
        self._old_proc = old
        this = self

        def wndproc(hwnd, msg, wparam, lparam):  # noqa: ANN001
            try:
                if msg == WM_NCCALCSIZE and wparam:
                    # 客户区 = 整个窗口矩形（系统不画标题栏）。
                    # 返回 0 表示使用 proposed 矩形，最大化时尺寸才正确。
                    return 0
                if msg == WM_NCHITTEST:
                    return this._hit_test(hwnd, lparam)
                if msg == WM_NCLBUTTONDBLCLK:
                    pos = this._lparam_to_client(lparam)
                    if pos is not None and 0 <= pos[1] < this._title_height_px():
                        if callable(this.on_toggle_max):
                            this.on_toggle_max()
                        return 0
            except Exception:  # noqa: BLE001
                pass
            call = getattr(ctypes.windll.user32, "CallWindowProcW")
            call.argtypes = [ctypes.c_void_p, wt.HWND, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t]
            call.restype = ctypes.c_ssize_t
            return call(this._old_proc, hwnd, msg, wparam, lparam)

        self._new_proc = prototype(wndproc)
        set_ptr(wt.HWND(self._hwnd), LGWLP_WNDPROC,
                ctypes.cast(self._new_proc, ctypes.c_void_p))

    # ── 命中测试 ──
    def _dpr(self) -> float:
        try:
            return float(self.win.devicePixelRatioF() or 1.0)
        except Exception:  # noqa: BLE001
            return 1.0

    def _title_height_px(self) -> int:
        return int(self.title_h * self._dpr())

    def _lparam_to_client(self, lparam: int) -> tuple[int, int] | None:
        """物理屏幕坐标 → 窗口客户区**物理**坐标。

        lParam 低 16 位是 x、高 16 位是 y，都是有符号 short。
        """
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        pt = wt.POINT(x, y)
        if not ctypes.windll.user32.ScreenToClient(wt.HWND(self._hwnd), ctypes.byref(pt)):
            return None
        return pt.x, pt.y

    def _hit_test(self, hwnd, lparam) -> int:  # noqa: ANN001
        pos = self._lparam_to_client(lparam)
        if pos is None:
            return HTCLIENT
        x, y = pos
        r = wt.RECT()
        ctypes.windll.user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        m = int(RESIZE_BORDER * self._dpr())

        # **边缘优先于标题带**（Agent 提醒：顶边必须判 HTTOP，不能判 HTCAPTION，
        # 否则拖到屏幕顶部的 Snap 与上边缘缩放都会失效）
        left, right = x <= m, x >= w - m
        top, bottom = y <= m, y >= h - m
        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
        if 0 <= y < self._title_height_px():
            return HTCAPTION
        return HTCLIENT

    # ── 卸载 ──
    def uninstall(self) -> None:
        if not self._installed or self._old_proc is None:
            return
        try:
            user32 = ctypes.windll.user32
            set_ptr = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            set_ptr.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
            set_ptr.restype = ctypes.c_void_p
            set_ptr(wt.HWND(self._hwnd), -4, ctypes.c_void_p(self._old_proc))
        except Exception:  # noqa: BLE001
            pass
        self._installed = False
