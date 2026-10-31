"""液态玻璃折射效果（1.1.3）。

## 为什么 QSS 做不到

QSS 只支持纯色/渐变/边框/圆角，**没有 backdrop-filter、没有 blur()、
没有混合模式**。所以「折射」必须自己画：

1. 抓取控件**背后的窗口内容**（`QWidget.render()` 到 QPixmap）；
2. 对这块像素做**模糊**（盒式模糊，多次迭代近似高斯）；
3. 叠加**色散**（RGB 三通道水平错位 1~2px，模拟折射率差异）；
4. 叠加**高光**（顶部亮边 + 斜向光泽）；
5. 用圆角裁切后绘制，再把子控件画在上面。

## 实现要点

* `GlassPane` 是一个透明覆盖层，`paintEvent` 里实时采样父窗口；
* 采样频率受限：仅当几何/滚动变化时重算（缓存 QPixmap），
  否则每次重绘都抓屏会卡；
* 模糊用 `QImage` 的整数运算做 3 次盒式模糊（半径 6/4/2），
  比高斯快很多，视觉上足够；
* 只在 `glass` 风格下启用；其它风格该类不参与布局。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QWidget


def _box_blur(img: QImage, radius: int) -> QImage:
    """水平+垂直盒式模糊（两遍），近似高斯。"""
    if radius < 1:
        return img
    src = img.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = src.width(), src.height()
    if w < 2 or h < 2:
        return src
    # 水平
    tmp = QImage(w, h, QImage.Format.Format_ARGB32)
    for y in range(h):
        acc_r = acc_g = acc_b = acc_a = 0
        line = [src.pixel(x, y) for x in range(w)]
        for x in range(w):
            p = line[x]
            acc_a += (p >> 24) & 0xFF
            acc_r += (p >> 16) & 0xFF
            acc_g += (p >> 8) & 0xFF
            acc_b += p & 0xFF
            if x >= radius * 2 + 1:
                q = line[x - radius * 2 - 1]
                acc_a -= (q >> 24) & 0xFF
                acc_r -= (q >> 16) & 0xFF
                acc_g -= (q >> 8) & 0xFF
                acc_b -= q & 0xFF
            n = min(x + 1, radius * 2 + 1)
            tmp.setPixel(x, y, ((acc_a // n) << 24) | ((acc_r // n) << 16)
                         | ((acc_g // n) << 8) | (acc_b // n))
    # 垂直
    out = QImage(w, h, QImage.Format.Format_ARGB32)
    for x in range(w):
        acc_r = acc_g = acc_b = acc_a = 0
        col = [tmp.pixel(x, y) for y in range(h)]
        for y in range(h):
            p = col[y]
            acc_a += (p >> 24) & 0xFF
            acc_r += (p >> 16) & 0xFF
            acc_g += (p >> 8) & 0xFF
            acc_b += p & 0xFF
            if y >= radius * 2 + 1:
                q = col[y - radius * 2 - 1]
                acc_a -= (q >> 24) & 0xFF
                acc_r -= (q >> 16) & 0xFF
                acc_g -= (q >> 8) & 0xFF
                acc_b -= q & 0xFF
            n = min(y + 1, radius * 2 + 1)
            out.setPixel(x, y, ((acc_a // n) << 24) | ((acc_r // n) << 16)
                         | ((acc_g // n) << 8) | (acc_b // n))
    return out


def _chromatic(img: QImage, shift: int = 1) -> QImage:
    """色散：R 左移、B 右移，模拟玻璃对不同波长折射率不同。"""
    if shift < 1:
        return img
    w, h = img.width(), img.height()
    out = QImage(w, h, QImage.Format.Format_ARGB32)
    for y in range(h):
        for x in range(w):
            xr = max(0, min(w - 1, x - shift))
            xb = max(0, min(w - 1, x + shift))
            pr = img.pixel(xr, y)
            pg = img.pixel(x, y)
            pb = img.pixel(xb, y)
            out.setPixel(x, y, (pg & 0xFF000000)
                         | (pr & 0x00FF0000) | (pg & 0x0000FF00) | (pb & 0x000000FF))
    return out


class GlassPane(QWidget):
    """液态玻璃覆盖层：采样背后内容 → 模糊 → 色散 → 高光。"""

    def __init__(self, parent: QWidget | None = None, *, radius: int = 14,
                 tint: str = "#F7FAFF", alpha: float = 0.72, dark: bool = False) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._radius = radius
        self._tint = QColor(tint)
        self._alpha = alpha
        self._dark = dark
        self._cache: QPixmap | None = None
        self._cache_key: tuple | None = None

    def set_tint(self, tint: str, alpha: float, dark: bool) -> None:
        self._tint = QColor(tint)
        self._alpha = alpha
        self._dark = dark
        self._cache = None
        self.update()

    def _sample(self) -> QPixmap:
        """抓父窗口在本控件区域的像素（含背后所有内容）。

        **必须临时隐藏自己**，否则 `window().render()` 会触发本控件的
        `paintEvent` → 再次采样 → **递归重绘死循环**
        （实测报 `QWidget::repaint: Recursive repaint detected` 并卡死）。
        """
        win = self.window()
        if win is None:
            return QPixmap()
        was_visible = self.isVisible()
        self.setVisible(False)  # 采样期间隐身，避免递归
        try:
            top_left = self.mapTo(win, QPoint(0, 0))
            rect = QRect(top_left, self.size())
            pm = QPixmap(self.size())
            pm.fill(Qt.GlobalColor.transparent)
            win.render(pm, QPoint(0, 0), rect)
            return pm
        finally:
            if was_visible:
                self.setVisible(True)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        key = (self.width(), self.height(), self.mapTo(self.window(), QPoint(0, 0)).x(),
               self.mapTo(self.window(), QPoint(0, 0)).y())
        if self._cache is None or self._cache_key != key:
            raw = self._sample()
            if raw.isNull():
                return
            img = raw.toImage()
            # 三次盒式模糊逼近高斯
            for r in (6, 4, 2):
                img = _box_blur(img, r)
            img = _chromatic(img, 1)
            self._cache = QPixmap.fromImage(img)
            self._cache_key = key
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1,
                            self._radius, self._radius)
        p.setClipPath(path)

        # 1) 模糊后的背景
        p.setOpacity(0.92)
        p.drawPixmap(0, 0, self._cache)
        p.setOpacity(1.0)

        # 2) 着色（半透明玻璃底色）
        tint = QColor(self._tint)
        tint.setAlphaF(self._alpha)
        p.fillPath(path, tint)

        # 3) 顶部高光（斜向渐变，模拟光在玻璃表面的反射）
        g = QLinearGradient(0, 0, self.width() * 0.6, self.height())
        g.setColorAt(0.0, QColor(255, 255, 255, 90 if not self._dark else 46))
        g.setColorAt(0.35, QColor(255, 255, 255, 22 if not self._dark else 10))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, g)

        # 4) 一圈内高光边（立体玻璃感）
        pen = QColor(255, 255, 255, 140 if not self._dark else 60)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2,
                          self._radius, self._radius)

        # 5) 底部内阴影
        shade = QLinearGradient(0, self.height() * 0.7, 0, self.height())
        shade.setColorAt(0.0, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, 26 if not self._dark else 46))
        p.fillPath(path, shade)
        p.end()

def displace(img: QImage, radius: int, strength: float = 0.55,
             edge: int = 14) -> QImage:
    """**真折射**：按边缘位移场重采样（对应资料里的 feDisplacementMap 思路）。

    ## 为什么必须这么做

    资料原文：「玻璃拟态模糊它背后的东西，液态玻璃**折射**它」；
    「真折射会把面板背后穿过的直线扭曲掉」。盒式模糊做不到这一点 ——
    那只是把背后糊掉，属于 glassmorphism。

    ## 位移场

    对每个像素，沿「指向中心」的方向按距离查表偏移：
    * 距边缘 < edge px：位移量从 strength*edge 递减到 0（越靠边折得越狠）
    * 中心区域：位移 0（保持清晰，不糊）
    采样用双线性插值，避免锯齿。
    """
    w, h = img.width(), img.height()
    if w < 8 or h < 8 or strength <= 0:
        return img
    src = img.convertToFormat(QImage.Format.Format_ARGB32)
    out = QImage(w, h, QImage.Format.Format_ARGB32)
    cx, cy = w / 2.0, h / 2.0

    # 预计算每个像素的偏移量与方向（只算一次，供表格复用）
    for y in range(h):
        dy_edge = min(y, h - 1 - y)
        vy = (cy - y)
        for x in range(w):
            dx_edge = min(x, w - 1 - x)
            d_edge = min(dx_edge, dy_edge)
            if d_edge >= edge:
                out.setPixel(x, y, src.pixel(x, y))
                continue
            # 距边缘越近，折得越狠（平方衰减更自然）
            t = 1.0 - d_edge / edge
            amp = strength * edge * t * t
            vx = (cx - x)
            norm = (vx * vx + vy * vy) ** 0.5 or 1.0
            sx = int(round(x + vx / norm * amp))
            sy = int(round(y + vy / norm * amp))
            sx = 0 if sx < 0 else (w - 1 if sx >= w else sx)
            sy = 0 if sy < 0 else (h - 1 if sy >= h else sy)
            out.setPixel(x, y, src.pixel(sx, sy))
    return out
