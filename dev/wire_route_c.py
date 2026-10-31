"""把路线 C（FramelessChrome）接入 MainWindow（1.1.5）。

替换掉路线 B 的「Qt 自己实现拖动/缩放」：
* **原生拖拽**：HTCAPTION → 系统走 SC_DRAGMOVE 回路 → Aero Snap 可用
* **原生缩放**：边缘 HTLEFT/HTTOP/... → 系统处理，无需 startSystemResize
* **双击最大化**：WM_NCLBUTTONDBLCLK（Qt 收不到，必须 native 处理）
* 保留自绘标题栏的**外观**（配色仍随主题）
"""
from pathlib import Path

M = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\main_window.py")
s = M.read_text(encoding="utf-8")

# ── 1. 创建标题栏后安装 FramelessChrome ──
old = """            ol.addWidget(self.titlebar)
            ol.addWidget(central, 1)
            self.setCentralWidget(outer)
            self.setMouseTracking(True)
            outer.setMouseTracking(True)"""
new = """            ol.addWidget(self.titlebar)
            ol.addWidget(central, 1)
            self.setCentralWidget(outer)
            # **路线 C**（1.1.5）：把原生框架位 OR 回去 + NCCALCSIZE 归零 +
            # NCHITTEST 分区。这样拖拽/缩放/Snap 都走系统原生回路，
            # 而不是 Qt 自己实现的版本（后者 WM_NCHITTEST 全返回 HTCLIENT，
            # 实测无原生 Snap）。
            self._chrome = None
            self._install_native_chrome()"""
if old in s:
    s = s.replace(old, new, 1)
    print("1) 已改装配逻辑")
else:
    print("1) 未匹配装配段")

# ── 2. 加安装方法（放在 _apply_titlebar_theme 之前）──
anchor = "    def _apply_titlebar_theme(self, palette) -> None:  # noqa: ANN001"
method = '''    def _install_native_chrome(self) -> None:
        """安装路线 C 的原生框架处理（仅 Windows）。

        `winId()` 在窗口未显示时可能尚未创建，所以这里用 `singleShot(0)`
        等窗口真正创建后再装；同时避免在 `__init__` 里过早触碰 HWND。
        """
        from PySide6.QtCore import QTimer

        def _do() -> None:
            try:
                from .winframe import FramelessChrome

                chrome = FramelessChrome(self, title_height=TITLE_H)
                # native 双击（Qt 的 mouseDoubleClickEvent 在 HTCAPTION 下收不到）
                chrome.on_toggle_max = self._toggle_max
                if chrome.install():
                    self._chrome = chrome
                    log.debug("原生框架已安装（路线 C）")
            except Exception:  # noqa: BLE001
                log.debug("原生框架安装失败，退回 Qt 拖动", exc_info=True)

        QTimer.singleShot(0, _do)

    def _toggle_max(self) -> None:
        """最大化/还原（供原生双击与标题栏按钮共用）。"""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        if self.titlebar is not None:
            self.titlebar.sync_max_glyph()

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        """卸载窗口子类化，避免回调悬空。"""
        chrome = getattr(self, "_chrome", None)
        if chrome is not None:
            try:
                chrome.uninstall()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)

'''
if "def _install_native_chrome" not in s:
    s = s.replace(anchor, method + anchor, 1)
    print("2) 已加 _install_native_chrome / _toggle_max")

M.write_text(s, encoding="utf-8", newline="")
import py_compile
py_compile.compile(str(M), doraise=True)
print("3) 语法 OK")
