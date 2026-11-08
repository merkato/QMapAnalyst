"""
Ikony przycisków generowania warstw – rysowane programowo w kolorach stylów warstw wynikowych,
dzięki czemu przycisk wygląda jak legenda warstwy, którą tworzy.
"""

import math

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

S = 64  # rozmiar roboczy; Qt skaluje ikonę do rozmiaru przycisku

RED = QColor(220, 30, 30)
GREEN = QColor(0, 160, 60)
BLUE = QColor(30, 136, 229)
VIOLET = QColor(103, 58, 183)
PURPLE = QColor(138, 43, 226)
GREY = QColor(140, 140, 140)
TEAL = QColor(0, 137, 123)
ORANGE = QColor(230, 126, 34)
DEEP_BLUE = QColor(25, 118, 210)
TRASH = QColor(207, 34, 46)


def _canvas():
    pm = QPixmap(S, S)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pm, p


def _pen(color, width, style=Qt.PenStyle.SolidLine):
    pen = QPen(color, width, style)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _finish(pm, p):
    p.end()
    return QIcon(pm)


def _arrow(p, a, b, color, width=4.5, head=13):
    p.setPen(_pen(color, width))
    p.drawLine(a, b)
    ang = math.atan2(b.y() - a.y(), b.x() - a.x())
    pts = [b, QPointF(b.x() - head * math.cos(ang - 0.45), b.y() - head * math.sin(ang - 0.45)),
           QPointF(b.x() - head * math.cos(ang + 0.45), b.y() - head * math.sin(ang + 0.45))]
    p.setBrush(QBrush(color))
    p.setPen(_pen(color, 1.5))
    p.drawPolygon(QPolygonF(pts))


def _gcp(p, c, color, r=7):
    p.setPen(_pen(color, 3.5))
    p.drawLine(QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y()))
    p.drawLine(QPointF(c.x(), c.y() - r), QPointF(c.x(), c.y() + r))


def points():
    pm, p = _canvas()
    for (x, y), (dx, dy) in (((16, 44), (8, -6)), ((40, 20), (-7, 8)), ((46, 48), (6, 5))):
        a, b = QPointF(x, y), QPointF(x + dx, y + dy)
        p.setPen(_pen(GREY, 2.5))
        p.drawLine(a, b)
        p.setPen(_pen(QColor(255, 255, 255), 1.5))
        p.setBrush(QBrush(GREEN))
        p.drawEllipse(b, 4.5, 4.5)
        _gcp(p, a, RED)
    return _finish(pm, p)


def vectors():
    pm, p = _canvas()
    _arrow(p, QPointF(10, 52), QPointF(36, 26), RED)
    _arrow(p, QPointF(34, 54), QPointF(54, 44), RED, 3.5, 11)
    _arrow(p, QPointF(14, 22), QPointF(26, 8), RED, 3.5, 11)
    return _finish(pm, p)


def circles():
    pm, p = _canvas()
    for (x, y, r, col) in ((22, 24, 15, BLUE), (44, 42, 11, BLUE), (18, 50, 7, RED)):
        c = QPointF(x, y)
        fill = QColor(col)
        fill.setAlpha(45)
        p.setBrush(QBrush(fill))
        p.setPen(_pen(col, 3))
        p.drawEllipse(c, r, r)
        p.setBrush(QBrush(QColor(40, 40, 40)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(c, 2.5, 2.5)
    return _finish(pm, p)


def distortion_grid():
    """Siatka TPS: pewna (fiolet) i ekstrapolowana (szara przerywana)."""
    pm, p = _canvas()

    def warp(x, y):
        return QPointF(x + 4 * math.sin(y / 14.0), y + 4 * math.sin(x / 16.0))

    for k, t in enumerate((10, 24, 38, 52)):
        for horizontal in (True, False):
            path = QPainterPath()
            for i in range(0, 61, 4):
                q = warp(i + 2, t) if horizontal else warp(t, i + 2)
                path.moveTo(q) if i == 0 else path.lineTo(q)
            outer = k == 3
            p.setPen(_pen(GREY, 2.5, Qt.PenStyle.DashLine) if outer else _pen(VIOLET, 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
    return _finish(pm, p)


def reference_grid():
    """Siatka układu odniesienia przeniesiona na skan (obrócona)."""
    pm, p = _canvas()
    p.translate(S / 2, S / 2)
    p.rotate(-12)
    p.setPen(_pen(PURPLE, 3))
    for t in (-21, -7, 7, 21):
        p.drawLine(QPointF(t, -30), QPointF(t, 30))
        p.drawLine(QPointF(-30, t), QPointF(30, t))
    return _finish(pm, p)


def _isolines(color, label):
    pm, p = _canvas()
    p.setBrush(Qt.BrushStyle.NoBrush)
    for k, off in enumerate((-14, 0, 14)):
        path = QPainterPath()
        for i in range(0, 65, 4):
            q = QPointF(i, 30 + off + 7 * math.sin((i + 9 * k) / 10.0))
            path.moveTo(q) if i == 0 else path.lineTo(q)
        p.setPen(_pen(color, 3.5))
        p.drawPath(path)
    f = QFont("Arial")
    f.setPixelSize(22)
    f.setBold(True)
    p.setFont(f)
    rect = QRectF(14, 18, 36, 26)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(255, 255, 255, 230)))
    p.drawRoundedRect(rect, 6, 6)
    p.setPen(_pen(color.darker(130), 1))
    p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)
    return _finish(pm, p)


def iso_scale():
    return _isolines(TEAL, "1:")


def iso_rotation():
    return _isolines(ORANGE, "∠")


def iso_omega():
    return _isolines(DEEP_BLUE, "2ω")


def trash():
    pm, p = _canvas()
    p.setPen(_pen(TRASH, 4))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(QPointF(12, 16), QPointF(52, 16))
    p.drawLine(QPointF(25, 16), QPointF(27, 9))
    p.drawLine(QPointF(27, 9), QPointF(37, 9))
    p.drawLine(QPointF(37, 9), QPointF(39, 16))
    body = QPolygonF([QPointF(17, 20), QPointF(47, 20), QPointF(44, 56), QPointF(20, 56)])
    fill = QColor(TRASH)
    fill.setAlpha(40)
    p.setBrush(QBrush(fill))
    p.drawPolygon(body)
    for x in (26, 32, 38):
        p.drawLine(QPointF(x, 27), QPointF(x, 49))
    return _finish(pm, p)
