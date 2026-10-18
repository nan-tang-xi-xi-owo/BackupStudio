"""导航图标：用 Qt 绘制简单线性图标（避免依赖 emoji 字体）。"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _pen(painter: QPainter, color: QColor, width: float = 1.8) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    return pen


def draw_icon(kind: str, painter: QPainter, rect: QRectF, color: QColor) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = rect.width(), rect.height()
    x, y = rect.x(), rect.y()
    _pen(painter, color, max(1.4, w * 0.09))
    p = QPainterPath()
    if kind == "tasks":
        # 三个叠放的盒子 + 勾
        for i in range(3):
            p.addRoundedRect(QRectF(x + w * 0.14, y + h * (0.2 + i * 0.2), w * 0.72, h * 0.13), 2, 2)
        painter.drawPath(p)
        painter.drawPolyline([QPointF(x + w * 0.6, y + h * 0.86), QPointF(x + w * 0.7, y + h * 0.94), QPointF(x + w * 0.88, y + h * 0.74)])
    elif kind == "history":
        painter.drawEllipse(QRectF(x + w * 0.12, y + h * 0.12, w * 0.76, h * 0.76))
        painter.drawLine(QPointF(x + w * 0.5, y + h * 0.3), QPointF(x + w * 0.5, y + h * 0.52))
        painter.drawLine(QPointF(x + w * 0.5, y + h * 0.52), QPointF(x + w * 0.68, y + h * 0.62))
    elif kind == "env":
        p.addRoundedRect(QRectF(x + w * 0.12, y + h * 0.16, w * 0.76, h * 0.5), 3, 3)
        p.moveTo(x + w * 0.5, y + h * 0.66)
        p.lineTo(x + w * 0.5, y + h * 0.82)
        p.moveTo(x + w * 0.3, y + h * 0.86)
        p.lineTo(x + w * 0.7, y + h * 0.86)
        painter.drawPath(p)
    elif kind == "settings":
        p.addEllipse(QRectF(x + w * 0.33, y + h * 0.33, w * 0.34, h * 0.34))
        painter.drawPath(p)
        import math

        for i in range(8):
            ang = math.pi / 4 * i
            cx, cy = x + w / 2, y + h / 2
            painter.drawLine(
                QPointF(cx + math.cos(ang) * w * 0.26, cy + math.sin(ang) * h * 0.26),
                QPointF(cx + math.cos(ang) * w * 0.4, cy + math.sin(ang) * h * 0.4),
            )
    elif kind == "run":
        p.moveTo(x + w * 0.32, y + h * 0.22)
        p.lineTo(x + w * 0.78, y + h * 0.5)
        p.lineTo(x + w * 0.32, y + h * 0.78)
        p.closeSubpath()
        painter.setBrush(color)
        painter.drawPath(p)
    elif kind == "stop":
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(x + w * 0.25, y + h * 0.25, w * 0.5, h * 0.5), 3, 3)
    elif kind == "plus":
        painter.drawLine(QPointF(x + w * 0.5, y + h * 0.22), QPointF(x + w * 0.5, y + h * 0.78))
        painter.drawLine(QPointF(x + w * 0.22, y + h * 0.5), QPointF(x + w * 0.78, y + h * 0.5))
    elif kind == "trash":
        painter.drawLine(QPointF(x + w * 0.2, y + h * 0.28), QPointF(x + w * 0.8, y + h * 0.28))
        p.addRoundedRect(QRectF(x + w * 0.28, y + h * 0.34, w * 0.44, h * 0.46), 3, 3)
        painter.drawPath(p)
    elif kind == "folder":
        p.addRoundedRect(QRectF(x + w * 0.12, y + h * 0.24, w * 0.76, h * 0.56), 4, 4)
        painter.drawPath(p)
        painter.drawLine(QPointF(x + w * 0.12, y + h * 0.38), QPointF(x + w * 0.88, y + h * 0.38))
    elif kind == "check":
        painter.drawPolyline(
            [QPointF(x + w * 0.18, y + h * 0.55), QPointF(x + w * 0.42, y + h * 0.78), QPointF(x + w * 0.84, y + h * 0.24)]
        )
    painter.restore()


def line_icon(kind: str, color: str = "#6B7280", size: int = 40) -> QIcon:
    pm = QPixmap(QSize(size, size))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    draw_icon(kind, painter, QRectF(0, 0, size, size), QColor(color))
    painter.end()
    return QIcon(pm)
