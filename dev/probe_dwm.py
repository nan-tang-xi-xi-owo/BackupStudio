"""实测 Windows 10 19044 上 DWM 标题栏相关属性的真实能力。"""
import ctypes
import ctypes.wintypes as wt
import sys

dwm = ctypes.windll.dwmapi

PROPS = {
    "USE_IMMERSIVE_DARK_MODE": 20,
    "WINDOW_CORNER_PREFERENCE": 33,
    "BORDER_COLOR": 34,
    "CAPTION_COLOR": 35,
    "TEXT_COLOR": 36,
}

HR = {0: "S_OK", 0x80070057: "E_INVALIDARG", 0x80070005: "E_ACCESSDENIED"}


def check(hwnd: int) -> None:
    for name, attr in PROPS.items():
        val = ctypes.c_int(1)
        hr = dwm.DwmSetWindowAttribute(
            wt.HWND(hwnd), ctypes.c_uint(attr),
            ctypes.byref(val), ctypes.sizeof(val)
        )
        print(f"    {name:<26} attr={attr:<3} HRESULT=0x{hr & 0xFFFFFFFF:08X} "
              f"({HR.get(hr, 'other')})")

    # 颜色属性要用 COLORREF
    for name, attr in (("CAPTION_COLOR(ref)", 35), ("TEXT_COLOR(ref)", 36),
                       ("BORDER_COLOR(ref)", 34)):
        col = 0x00332211  # COLORREF (BGR)
        hr = dwm.DwmSetWindowAttribute(
            wt.HWND(hwnd), ctypes.c_uint(attr),
            ctypes.byref(ctypes.c_int(col)), ctypes.sizeof(ctypes.c_int)
        )
        print(f"    {name:<26} attr={attr:<3} HRESULT=0x{hr & 0xFFFFFFFF:08X} "
              f"({HR.get(hr, 'other')})")


from PySide6.QtWidgets import QApplication, QWidget

app = QApplication(sys.argv)
w = QWidget()
w.setWindowTitle("DWM probe")
w.resize(300, 160)
w.show()
app.processEvents()

hwnd = int(w.winId())
print(f"  HWND = 0x{hwnd:X}")
print("\n  === SetWindowAttribute 返回码 ===")
check(hwnd)

# 读取回显：看属性是否真的被接受（失败时读回会是默认值）
print("\n  === 读回 CAPTION_COLOR ===")
val = ctypes.c_int(0)
got = ctypes.c_uint(0)
hr = dwm.DwmGetWindowAttribute(wt.HWND(hwnd), ctypes.c_uint(35),
                               ctypes.byref(val), ctypes.sizeof(val))
print(f"    DwmGetWindowAttribute(35) HRESULT=0x{hr & 0xFFFFFFFF:08X} "
      f"value=0x{val.value & 0xFFFFFFFF:08X}")
print(f"    (写入的是 0x00332211，若读回相同说明属性生效)")

w.close()
