"""
Núcleo del Creador de Texturas.
Contiene modelo de datos, comandos, IO, utilidades e iconos.
No importa nada de QtWidgets → 100% reutilizable en desktop y Android.
"""
from __future__ import annotations

import json
import math
import os
import sys
import zipfile
from array import array
from collections import deque

from PySide6.QtCore import (
    QBuffer, QByteArray, QIODevice, QObject, QPointF, QRect, QRectF,
    QSize, Qt, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QIcon, QImage, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient,
    QUndoCommand, QUndoStack,
)


# ============================================================
#  CONSTANTES
# ============================================================
APP_NAME = "Creador de Texturas"
PROJECT_EXT = ".texproj"
MAX_DOC_SIZE = 4096
UNDO_LIMIT = 100

BG0 = "#121215"
BG1 = "#1a1a20"
BG2 = "#23232b"
BG3 = "#2d2d37"
BORDER = "#33333e"
TEXT = "#e6e6ee"
MUTED = "#8b8b9a"
ACCENT = "#6e7bff"
CANVAS_BG = "#16161a"

IS_ANDROID = (sys.platform == "android"
              or os.environ.get("QT_QPA_PLATFORM") == "android"
              or os.environ.get("ANDROID_ARGUMENT") is not None)


# ============================================================
#  TEMA
# ============================================================
STYLE = """
* { font-family: "Segoe UI", "Inter", "Noto Sans", "Roboto", sans-serif; font-size: 13px; }
QWidget { color: @TEXT; }
QMainWindow { background: @BG0; }
#Root { background: @BG0; }
#MobileRoot { background: @BG0; }
#TopBar { background: @BG1; border-bottom: 1px solid @BORDER; }
#BottomBar { background: @BG1; border-top: 1px solid @BORDER; }
#Drawer { background: @BG1; border: 1px solid @BORDER; border-radius: 10px; }
#TitleBar { background: @BG0; }
#AppTitle { color: @MUTED; font-size: 12px; }
#Sidebar, #RightPanel { background: @BG1; }
#ToolBar { background: @BG1; border-right: 1px solid @BORDER; }
#StatusBar { background: @BG0; border-top: 1px solid @BORDER; }
#StatusBar QLabel { color: @MUTED; }
QLabel#Muted { color: @MUTED; }
QLabel#PanelTitle { font-size: 13px; font-weight: 600; padding: 2px 0; }

QMenuBar { background: transparent; }
QMenuBar::item { padding: 6px 10px; border-radius: 5px; background: transparent; }
QMenuBar::item:selected { background: @BG3; }
QMenu { background: @BG2; border: 1px solid @BORDER; padding: 5px; border-radius: 8px; }
QMenu::item { padding: 10px 34px 10px 22px; border-radius: 6px; }
QMenu::item:selected { background: @ACCENT_DIM; }
QMenu::separator { height: 1px; background: @BORDER; margin: 5px 8px; }

QPushButton { background: @BG2; border: 1px solid @BORDER; border-radius: 8px;
    padding: 8px 14px; min-height: 34px; }
QPushButton:hover { background: @BG3; }
QPushButton:disabled { color: #5d5d6a; }
QPushButton#SectionHeader { text-align: left; padding: 10px 12px; font-weight: 600;
    background: @BG2; border: none; border-radius: 8px; min-height: 40px; }
QPushButton#SectionHeader:hover { background: @BG3; }

QToolButton { background: transparent; border: 1px solid transparent; border-radius: 8px;
    padding: 6px; min-width: 40px; min-height: 40px; }
QToolButton:hover { background: @BG3; }
QToolButton:checked { background: @ACCENT_DIM; border: 1px solid @ACCENT; }
QToolButton#ToolBtnMobile { min-width: 44px; min-height: 44px; }
QToolButton#ToolBtnMobile:checked { background: @ACCENT; color: white; border: 1px solid @ACCENT; }

QLineEdit, QSpinBox, QComboBox { background: @BG0; border: 1px solid @BORDER;
    border-radius: 6px; padding: 6px 8px; min-height: 32px;
    selection-background-color: @ACCENT; }
QComboBox QAbstractItemView { background: @BG2; border: 1px solid @BORDER;
    selection-background-color: @ACCENT_DIM; outline: 0; padding: 4px; }

QSlider::groove:horizontal { height: 6px; background: @BG3; border-radius: 3px; }
QSlider::sub-page:horizontal { background: @ACCENT; border-radius: 3px; }
QSlider::handle:horizontal { background: white; width: 20px; height: 20px;
    margin: -8px 0; border-radius: 12px; border: 2px solid @ACCENT; }

QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #4a4a55; border-radius: 6px; min-height: 40px; margin: 2px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }

QListView { background: @BG0; border: 1px solid @BORDER; border-radius: 8px; outline: 0; }
QListView::item { margin: 3px 4px; border-radius: 8px; min-height: 56px; }
#LayerList::item:selected { background: transparent; }
QUndoView::item:selected { background: @ACCENT_DIM; color: @TEXT; }

QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 20px; height: 20px; border-radius: 4px;
    border: 1px solid #4a4a58; background: @BG0; }
QCheckBox::indicator:checked { background: @ACCENT; border: 1px solid @ACCENT; }

QDialog, QMessageBox { background: @BG1; }

#LayerRow { border-radius: 8px; background: transparent; border: 1px solid transparent; }
#LayerRow[active="true"] { background: @ACCENT_DIM; border: 1px solid @ACCENT; }
"""


def build_stylesheet() -> str:
    tokens = {
        "@BG0": BG0, "@BG1": BG1, "@BG2": BG2, "@BG3": BG3, "@BORDER": BORDER,
        "@TEXT": TEXT, "@MUTED": MUTED, "@ACCENT_DIM": "rgba(110,123,255,70)",
        "@ACCENT": ACCENT,
    }
    s = STYLE
    for k in sorted(tokens, key=len, reverse=True):
        s = s.replace(k, tokens[k])
    return s


# ============================================================
#  UTILIDADES
# ============================================================
def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def mid(a: QPointF, b: QPointF) -> QPointF:
    return QPointF((a.x() + b.x()) * 0.5, (a.y() + b.y()) * 0.5)


def quad_point(a: QPointF, c: QPointF, b: QPointF, t: float) -> QPointF:
    u = 1.0 - t
    return QPointF(u * u * a.x() + 2 * u * t * c.x() + t * t * b.x(),
                   u * u * a.y() + 2 * u * t * c.y() + t * t * b.y())


def premultiplied_argb(color: QColor) -> int:
    a = color.alpha()
    r = (color.red() * a + 127) // 255
    g = (color.green() * a + 127) // 255
    b = (color.blue() * a + 127) // 255
    return (a << 24) | (r << 16) | (g << 8) | b


def image_pixels_u32(image: QImage):
    mv = memoryview(image.bits())
    if mv.format != "B":
        mv = mv.cast("B")
    return mv.cast("I")


def flood_fill_u32(pixels, width, height, x, y, fill_value, tolerance=0):
    if not (0 <= x < width and 0 <= y < height):
        return None
    target = pixels[y * width + x]

    if tolerance <= 0:
        def match(v, t=target):
            return v == t
    else:
        ta, tr, tg, tb = target >> 24, (target >> 16) & 255, (target >> 8) & 255, target & 255
        tol = tolerance
        cache = {}

        def match(v):
            r = cache.get(v)
            if r is None:
                r = (abs((v >> 24) - ta) <= tol and abs(((v >> 16) & 255) - tr) <= tol
                     and abs(((v >> 8) & 255) - tg) <= tol and abs((v & 255) - tb) <= tol)
                cache[v] = r
            return r

    seen = bytearray(width * height)
    stack = [(x, y)]
    x0, y0, x1, y1 = width, height, -1, -1
    while stack:
        sx, sy = stack.pop()
        row = sy * width
        if seen[row + sx] or not match(pixels[row + sx]):
            continue
        xl = sx
        while xl > 0 and not seen[row + xl - 1] and match(pixels[row + xl - 1]):
            xl -= 1
        xr = sx
        while xr < width - 1 and not seen[row + xr + 1] and match(pixels[row + xr + 1]):
            xr += 1
        n = xr - xl + 1
        pixels[row + xl:row + xr + 1] = array("I", [fill_value]) * n
        seen[row + xl:row + xr + 1] = b"\x01" * n
        x0 = min(x0, xl); x1 = max(x1, xr)
        y0 = min(y0, sy); y1 = max(y1, sy)
        for ny in (sy - 1, sy + 1):
            if 0 <= ny < height:
                nrow = ny * width
                i = xl
                while i <= xr:
                    if not seen[nrow + i] and match(pixels[nrow + i]):
                        stack.append((i, ny)); i += 1
                        while i <= xr and not seen[nrow + i] and match(pixels[nrow + i]):
                            i += 1
                    else:
                        i += 1
    if x1 < 0:
        return None
    return (x0, y0, x1, y1)


def offset_image(img: QImage, dx: int, dy: int) -> QImage:
    w, h = img.width(), img.height()
    out = QImage(w, h, img.format())
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode_Source)
    bx, by = dx % w, dy % h
    for ox in (0, -w):
        for oy in (0, -h):
            p.drawImage(bx + ox, by + oy, img)
    p.end()
    return out


def invert_image(img: QImage) -> QImage:
    straight = img.convertToFormat(QImage.Format_ARGB32)
    straight.invertPixels(QImage.InvertRgb)
    return straight.convertToFormat(QImage.Format_ARGB32_Premultiplied)


def grayscale_image(img: QImage) -> QImage:
    straight = img.convertToFormat(QImage.Format_ARGB32)
    gray = (straight.convertToFormat(QImage.Format_Grayscale8)
            .convertToFormat(QImage.Format_ARGB32_Premultiplied))
    p = QPainter(gray)
    p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    p.drawImage(0, 0, img)
    p.end()
    return gray


_checker_cache = {}


def checker_pixmap(cell=8, c1="#2b2b31", c2="#35353c") -> QPixmap:
    key = (cell, c1, c2)
    pm = _checker_cache.get(key)
    if pm is None:
        pm = QPixmap(cell * 2, cell * 2)
        p = QPainter(pm)
        p.fillRect(0, 0, cell * 2, cell * 2, QColor(c1))
        p.fillRect(0, 0, cell, cell, QColor(c2))
        p.fillRect(cell, cell, cell, cell, QColor(c2))
        p.end()
        _checker_cache[key] = pm
    return pm


# ============================================================
#  ICONOS VECTORIALES
# ============================================================
def _pts(*coords):
    return QPolygonF([QPointF(x, y) for x, y in coords])


def _ic_brush(p, c):
    p.drawLine(QPointF(16, 3.5), QPointF(9.5, 10))
    p.setBrush(c); p.drawEllipse(QPointF(7.2, 12.8), 3.6, 3.6)


def _ic_eraser(p, c):
    p.save(); p.translate(10, 10); p.rotate(-40)
    p.drawRoundedRect(QRectF(-7, -3.8, 14, 7.6), 2, 2)
    p.drawLine(QPointF(-1.5, -3.8), QPointF(-1.5, 3.8))
    p.restore()


def _ic_picker(p, c):
    p.save(); p.translate(12.5, 7.5); p.rotate(45)
    p.drawRoundedRect(QRectF(-2.2, -6, 4.4, 9), 1.6, 1.6)
    p.restore()
    p.drawLine(QPointF(9.2, 11), QPointF(4, 16.2))


def _ic_fill(p, c):
    p.drawPolygon(_pts((4, 10), (10, 4), (16, 10), (10, 16)))
    p.drawLine(QPointF(4, 10), QPointF(16, 10))
    p.setBrush(c); p.drawEllipse(QPointF(16.6, 14.6), 1.6, 1.6)


def _ic_line(p, c):
    p.drawLine(QPointF(4.5, 15.5), QPointF(15.5, 4.5))
    p.setBrush(c)
    p.drawEllipse(QPointF(4.5, 15.5), 1.6, 1.6)
    p.drawEllipse(QPointF(15.5, 4.5), 1.6, 1.6)


def _ic_rect(p, c):
    p.drawRoundedRect(QRectF(3.5, 5.5, 13, 9), 1.5, 1.5)


def _ic_ellipse(p, c):
    p.drawEllipse(QRectF(3, 5, 14, 10))


def _ic_hand(p, c):
    p.drawLine(QPointF(10, 3), QPointF(10, 17)); p.drawLine(QPointF(3, 10), QPointF(17, 10))
    p.drawPolyline(_pts((8, 5), (10, 3), (12, 5)))
    p.drawPolyline(_pts((8, 15), (10, 17), (12, 15)))
    p.drawPolyline(_pts((5, 8), (3, 10), (5, 12)))
    p.drawPolyline(_pts((15, 8), (17, 10), (15, 12)))


def _eye_path():
    path = QPainterPath()
    path.moveTo(2, 10); path.quadTo(10, 2, 18, 10); path.quadTo(10, 18, 2, 10)
    return path


def _ic_eye(p, c):
    p.drawPath(_eye_path()); p.setBrush(c); p.drawEllipse(QPointF(10, 10), 2.4, 2.4)


def _ic_eye_off(p, c):
    dim = QColor(c); dim.setAlpha(90)
    p.setPen(QPen(dim, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(_eye_path())
    p.drawLine(QPointF(4, 16), QPointF(16, 4))


def _ic_lock(p, c):
    p.setBrush(c); p.drawRoundedRect(QRectF(5, 9, 10, 8), 1.8, 1.8)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    path.moveTo(7.5, 9); path.lineTo(7.5, 7)
    path.arcTo(QRectF(7.5, 3.5, 5, 7), 180, -180); path.lineTo(12.5, 9)
    p.drawPath(path)


def _ic_unlock(p, c):
    dim = QColor(c); dim.setAlpha(120)
    p.setPen(QPen(dim, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawRoundedRect(QRectF(5, 9, 10, 8), 1.8, 1.8)
    path = QPainterPath()
    path.moveTo(7.5, 9); path.lineTo(7.5, 7)
    path.arcTo(QRectF(7.5, 3.5, 5, 7), 180, -180); path.lineTo(12.5, 6.3)
    p.drawPath(path)


def _ic_plus(p, c):
    p.drawLine(QPointF(10, 4), QPointF(10, 16)); p.drawLine(QPointF(4, 10), QPointF(16, 10))


def _ic_trash(p, c):
    p.drawRoundedRect(QRectF(6, 7.5, 8, 9), 1.2, 1.2)
    p.drawLine(QPointF(4.5, 6.5), QPointF(15.5, 6.5))
    p.drawPolyline(_pts((8.3, 6.5), (8.3, 4.5), (11.7, 4.5), (11.7, 6.5)))


def _ic_copy(p, c):
    p.drawRoundedRect(QRectF(7.5, 3.5, 9, 9), 1.5, 1.5)
    p.drawRoundedRect(QRectF(3.5, 7.5, 9, 9), 1.5, 1.5)


def _ic_up(p, c):
    p.drawPolyline(_pts((5, 12), (10, 7), (15, 12)))


def _ic_down(p, c):
    p.drawPolyline(_pts((5, 8), (10, 13), (15, 8)))


def _ic_merge(p, c):
    p.drawLine(QPointF(10, 3), QPointF(10, 13))
    p.drawPolyline(_pts((6, 9.5), (10, 13.5), (14, 9.5)))
    p.drawLine(QPointF(4, 17), QPointF(16, 17))


def _ic_menu(p, c):
    for y in (6, 10, 14):
        p.drawLine(QPointF(4, y), QPointF(16, y))


def _ic_undo(p, c):
    path = QPainterPath()
    path.moveTo(6, 8); path.lineTo(12, 8)
    path.arcTo(QRectF(6, 8, 8, 8), 90, -180)
    p.drawPath(path)
    p.drawPolyline(_pts((9, 5), (6, 8), (9, 11)))


def _ic_redo(p, c):
    path = QPainterPath()
    path.moveTo(14, 8); path.lineTo(8, 8)
    path.arcTo(QRectF(6, 8, 8, 8), 90, 180)
    p.drawPath(path)
    p.drawPolyline(_pts((11, 5), (14, 8), (11, 11)))


def _ic_layers(p, c):
    p.drawPolygon(_pts((10, 3), (17, 6.5), (10, 10), (3, 6.5)))
    p.drawPolyline(_pts((3, 10), (10, 13.5), (17, 10)))
    p.drawPolyline(_pts((3, 13.5), (10, 17), (17, 13.5)))


def _ic_color(p, c):
    p.setBrush(c); p.drawEllipse(QPointF(10, 10), 6, 6)


def _ic_close(p, c):
    p.drawLine(QPointF(5, 5), QPointF(15, 15)); p.drawLine(QPointF(15, 5), QPointF(5, 15))


def _ic_min(p, c):
    p.drawLine(QPointF(5, 10), QPointF(15, 10))
    
def _ic_max(p, c):
    p.drawRect(QRectF(5, 5, 10, 10))


def _ic_restore(p, c):
    p.drawRect(QRectF(4.5, 7.5, 8, 8))
    p.drawPolyline(_pts((7.5, 7.5), (7.5, 4.5), (15.5, 4.5), (15.5, 12.5), (12.5, 12.5)))


ICON_PAINTERS = {
    "brush": _ic_brush, "eraser": _ic_eraser, "picker": _ic_picker, "fill": _ic_fill,
    "line": _ic_line, "rect": _ic_rect, "ellipse": _ic_ellipse, "hand": _ic_hand,
    "eye": _ic_eye, "eye_off": _ic_eye_off, "lock": _ic_lock, "unlock": _ic_unlock,
    "plus": _ic_plus, "trash": _ic_trash, "copy": _ic_copy, "up": _ic_up, "down": _ic_down,
    "merge": _ic_merge, "menu": _ic_menu, "undo": _ic_undo, "redo": _ic_redo,
    "layers": _ic_layers, "color": _ic_color, "close": _ic_close,
    "min": _ic_min, "max": _ic_max, "restore": _ic_restore,
}

_icon_cache = {}


def _render_icon(name, color, size):
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 20.0, size / 20.0)
    col = QColor(color)
    p.setPen(QPen(col, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    ICON_PAINTERS[name](p, col)
    p.end()
    return pm


def make_icon(name, color="#cfcfdc", on_color="#ffffff", size=20) -> QIcon:
    key = (name, color, on_color, size)
    ic = _icon_cache.get(key)
    if ic is None:
        ic = QIcon()
        ic.addPixmap(_render_icon(name, color, size), QIcon.Normal, QIcon.Off)
        ic.addPixmap(_render_icon(name, on_color, size), QIcon.Normal, QIcon.On)
        ic.addPixmap(_render_icon(name, "#5d5d6a", size), QIcon.Disabled, QIcon.Off)
        _icon_cache[key] = ic
    return ic


def make_logo(size=20) -> QPixmap:
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor("#8f9bff")); g.setColorAt(1, QColor("#5b46d9"))
    p.setPen(Qt.NoPen); p.setBrush(QBrush(g))
    p.drawRoundedRect(QRectF(0, 0, size, size), 5, 5)
    p.setPen(QPen(QColor(255, 255, 255, 230), 1.4))
    s = size / 4.0
    for i in (1, 2, 3):
        p.drawLine(QPointF(s * i, 3), QPointF(s * i, size - 3))
        p.drawLine(QPointF(3, s * i), QPointF(size - 3, s * i))
    p.end()
    return pm


# ============================================================
#  CAPAS Y DOCUMENTO
# ============================================================
BLEND_MODES = {
    "normal": ("Normal", QPainter.CompositionMode_SourceOver),
    "multiply": ("Multiplicar", QPainter.CompositionMode_Multiply),
    "screen": ("Trama", QPainter.CompositionMode_Screen),
    "overlay": ("Superponer", QPainter.CompositionMode_Overlay),
    "darken": ("Oscurecer", QPainter.CompositionMode_Darken),
    "lighten": ("Aclarar", QPainter.CompositionMode_Lighten),
    "color_dodge": ("Sobreexponer", QPainter.CompositionMode_ColorDodge),
    "color_burn": ("Subexponer", QPainter.CompositionMode_ColorBurn),
    "hard_light": ("Luz fuerte", QPainter.CompositionMode_HardLight),
    "soft_light": ("Luz suave", QPainter.CompositionMode_SoftLight),
    "difference": ("Diferencia", QPainter.CompositionMode_Difference),
    "plus": ("Sumar", QPainter.CompositionMode_Plus),
}


class Layer:
    def __init__(self, name, width, height, fill=None, image=None):
        self.name = name
        if image is not None:
            self.image = image
        else:
            self.image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
            self.image.fill(fill if fill is not None else QColor(0, 0, 0, 0))
        self.visible = True
        self.locked = False
        self.opacity = 1.0
        self.blend = "normal"

    def clone(self):
        c = Layer(self.name, 0, 0, image=self.image.copy())
        c.visible, c.locked = self.visible, self.locked
        c.opacity, c.blend = self.opacity, self.blend
        return c


class PatchCommand(QUndoCommand):
    """Cambio de píxeles por región. Undo eficiente."""
    def __init__(self, doc, layer, rect, before, after, text, applied=True):
        super().__init__(text)
        self.doc, self.layer, self.rect = doc, layer, QRect(rect)
        self.before, self.after = before, after
        self._skip_first = applied

    def _apply(self, img):
        p = QPainter(self.layer.image)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.drawImage(self.rect.topLeft(), img)
        p.end()
        self.doc.notify_content(self.rect)

    def undo(self):
        self._apply(self.before)

    def redo(self):
        if self._skip_first:
            self._skip_first = False
            return
        self._apply(self.after)


class StackCommand(QUndoCommand):
    def __init__(self, doc, before_layers, before_active, after_layers, after_active, text):
        super().__init__(text)
        self.doc = doc
        self.before_layers, self.before_active = before_layers, before_active
        self.after_layers, self.after_active = after_layers, after_active

    def undo(self):
        self.doc.apply_stack(self.before_layers, self.before_active)

    def redo(self):
        self.doc.apply_stack(self.after_layers, self.after_active)


class PropertyCommand(QUndoCommand):
    MERGE_OPACITY_ID = 1001

    def __init__(self, doc, layer, attr, new_value, text):
        super().__init__(text)
        self.doc, self.layer, self.attr = doc, layer, attr
        self.old, self.new = getattr(layer, attr), new_value

    def id(self):
        return self.MERGE_OPACITY_ID if self.attr == "opacity" else -1

    def mergeWith(self, other):
        if (not isinstance(other, PropertyCommand) or other.layer is not self.layer
                or other.attr != self.attr):
            return False
        self.new = other.new
        return True

    def undo(self):
        self.doc.apply_property(self.layer, self.attr, self.old)

    def redo(self):
        self.doc.apply_property(self.layer, self.attr, self.new)


class Document(QObject):
    structure_changed = Signal()
    active_changed = Signal()
    content_changed = Signal(QRect)

    def __init__(self, width, height, white_background=True, name="Sin título",
                 create_background=True, parent=None):
        super().__init__(parent)
        self.width_px, self.height_px = width, height
        self.name = name
        self.path = None
        self.layers = []
        self.active_index = 0
        self._layer_counter = 1
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(UNDO_LIMIT)
        self.flat = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        self.flat.fill(QColor(0, 0, 0, 0))
        if create_background:
            fill = QColor(255, 255, 255) if white_background else QColor(0, 0, 0, 0)
            self.layers.append(Layer("Fondo", width, height, fill))
            self.rebuild_flat()
        self.undo_stack.setClean()

    @property
    def active_layer(self):
        if 0 <= self.active_index < len(self.layers):
            return self.layers[self.active_index]
        return None

    def full_rect(self):
        return QRect(0, 0, self.width_px, self.height_px)

    def can_paint(self):
        L = self.active_layer
        if L is None:
            return False, "No hay capa activa"
        if L.locked:
            return False, "La capa está bloqueada"
        if not L.visible:
            return False, "La capa está oculta"
        return True, ""

    def rebuild_flat(self, rect=None):
        full = self.full_rect()
        rect = full if rect is None else rect.intersected(full)
        if rect.isEmpty():
            return
        p = QPainter(self.flat)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(rect, QColor(0, 0, 0, 0))
        for L in self.layers:
            if L.visible and L.opacity > 0.0:
                p.setCompositionMode(BLEND_MODES.get(L.blend, BLEND_MODES["normal"])[1])
                p.setOpacity(L.opacity)
                p.drawImage(rect.topLeft(), L.image, rect)
        p.end()

    def notify_content(self, rect=None):
        rect = self.full_rect() if rect is None else rect
        self.rebuild_flat(rect)
        self.content_changed.emit(rect)

    def flatten(self, background: QColor = None) -> QImage:
        out = QImage(self.width_px, self.height_px, QImage.Format_ARGB32_Premultiplied)
        out.fill(background if background is not None else QColor(0, 0, 0, 0))
        p = QPainter(out)
        p.drawImage(0, 0, self.flat)
        p.end()
        return out

    def apply_stack(self, layers, active_layer):
        self.layers = list(layers)
        if active_layer in self.layers:
            self.active_index = self.layers.index(active_layer)
        else:
            self.active_index = clamp(self.active_index, 0, max(0, len(self.layers) - 1))
        self.rebuild_flat()
        self.structure_changed.emit()
        self.content_changed.emit(self.full_rect())

    def apply_property(self, layer, attr, value):
        setattr(layer, attr, value)
        if attr in ("visible", "opacity", "blend"):
            self.rebuild_flat()
            self.content_changed.emit(self.full_rect())
        self.structure_changed.emit()

    def set_active(self, idx):
        if 0 <= idx < len(self.layers) and idx != self.active_index:
            self.active_index = idx
            self.active_changed.emit()

    def _push_stack(self, new_layers, new_active, text):
        self.undo_stack.push(StackCommand(
            self, list(self.layers), self.active_layer, new_layers, new_active, text))

    def add_layer(self, name=None, image=None):
        self._layer_counter += 1
        name = name or f"Capa {self._layer_counter}"
        layer = Layer(name, self.width_px, self.height_px, image=image)
        new = list(self.layers)
        new.insert(self.active_index + 1, layer)
        self._push_stack(new, layer, "Añadir capa")
        return layer

    def duplicate_active(self):
        L = self.active_layer
        if L is None:
            return
        c = L.clone()
        c.name = L.name + " copia"
        new = list(self.layers)
        new.insert(self.active_index + 1, c)
        self._push_stack(new, c, "Duplicar capa")

    def remove_active(self):
        if len(self.layers) <= 1:
            return
        i = self.active_index
        new = list(self.layers)
        new.pop(i)
        self._push_stack(new, new[min(i, len(new) - 1)], "Eliminar capa")

    def move_active(self, delta):
        i, j = self.active_index, self.active_index + delta
        if not (0 <= j < len(self.layers)):
            return
        new = list(self.layers)
        new[i], new[j] = new[j], new[i]
        self._push_stack(new, self.active_layer, "Mover capa")

    def reorder(self, ordered_layers):
        if sorted(map(id, ordered_layers)) != sorted(map(id, self.layers)):
            return
        if all(a is b for a, b in zip(ordered_layers, self.layers)):
            return
        self._push_stack(list(ordered_layers), self.active_layer, "Reordenar capas")

    def merge_down(self):
        i = self.active_index
        if i <= 0:
            return False, "No hay capa inferior"
        upper, lower = self.layers[i], self.layers[i - 1]
        if lower.locked:
            return False, "La capa inferior está bloqueada"
        self.undo_stack.beginMacro("Combinar hacia abajo")
        if upper.visible:
            before = lower.image.copy()
            after = before.copy()
            p = QPainter(after)
            p.setCompositionMode(BLEND_MODES.get(upper.blend, BLEND_MODES["normal"])[1])
            p.setOpacity(upper.opacity)
            p.drawImage(0, 0, upper.image)
            p.end()
            self.undo_stack.push(PatchCommand(
                self, lower, self.full_rect(), before, after, "Combinar", applied=False))
        new = [L for L in self.layers if L is not upper]
        self._push_stack(new, lower, "Combinar")
        self.undo_stack.endMacro()
        return True, ""

    def set_layer_prop(self, layer, attr, value, text):
        if getattr(layer, attr) == value:
            return
        self.undo_stack.push(PropertyCommand(self, layer, attr, value, text))

    def apply_to_active(self, text, fn):
        ok, why = self.can_paint()
        if not ok:
            return False, why
        L = self.active_layer
        before = L.image.copy()
        after = fn(before.copy())
        self.undo_stack.push(PatchCommand(
            self, L, self.full_rect(), before, after, text, applied=False))
        return True, ""

    def clear_active(self):
        def fn(img):
            img.fill(QColor(0, 0, 0, 0))
            return img
        return self.apply_to_active("Limpiar capa", fn)

    def fill_active(self, color: QColor):
        def fn(img):
            img.fill(color)
            return img
        return self.apply_to_active("Rellenar capa", fn)


# ============================================================
#  PROYECTOS
# ============================================================
def _image_to_png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def save_project(doc: Document, path: str):
    meta = {
        "version": 1, "width": doc.width_px, "height": doc.height_px,
        "active": doc.active_index, "layers": [],
    }
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, L in enumerate(doc.layers):
            fname = f"layer_{i}.png"
            zf.writestr(fname, _image_to_png_bytes(L.image))
            meta["layers"].append({
                "name": L.name, "visible": L.visible, "locked": L.locked,
                "opacity": L.opacity, "blend": L.blend, "file": fname,
            })
        zf.writestr("project.json", json.dumps(meta, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def load_project(path: str) -> Document:
    with zipfile.ZipFile(path, "r") as zf:
        meta = json.loads(zf.read("project.json").decode("utf-8"))
        w, h = int(meta["width"]), int(meta["height"])
        if not (1 <= w <= MAX_DOC_SIZE and 1 <= h <= MAX_DOC_SIZE):
            raise ValueError("Dimensiones de proyecto no válidas")
        doc = Document(w, h, create_background=False,
                       name=os.path.splitext(os.path.basename(path))[0])
        for info in meta["layers"]:
            img = QImage.fromData(zf.read(info["file"]), "PNG")
            if img.isNull():
                raise ValueError(f"No se pudo leer la capa «{info['name']}»")
            img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            if img.width() != w or img.height() != h:
                canvas = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
                canvas.fill(QColor(0, 0, 0, 0))
                p = QPainter(canvas); p.drawImage(0, 0, img); p.end()
                img = canvas
            L = Layer(info.get("name", "Capa"), w, h, image=img)
            L.visible = bool(info.get("visible", True))
            L.locked = bool(info.get("locked", False))
            L.opacity = float(clamp(info.get("opacity", 1.0), 0.0, 1.0))
            L.blend = info.get("blend", "normal") if info.get("blend") in BLEND_MODES else "normal"
            doc.layers.append(L)
    if not doc.layers:
        raise ValueError("El proyecto no contiene capas")
    doc._layer_counter = len(doc.layers)
    doc.active_index = clamp(int(meta.get("active", 0)), 0, len(doc.layers) - 1)
    doc.path = path
    doc.rebuild_flat()
    doc.undo_stack.setClean()
    return doc


def document_from_image(img: QImage, name: str, path=None) -> Document:
    img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    doc = Document(img.width(), img.height(), create_background=False, name=name)
    doc.layers.append(Layer("Fondo", img.width(), img.height(), image=img))
    doc.rebuild_flat()
    doc.undo_stack.setClean()
    return doc


# ============================================================
#  AJUSTES DE HERRAMIENTA
# ============================================================
class ToolSettings:
    def __init__(self):
        self.tool = "brush"
        self.color = QColor(20, 20, 20)
        self.size = 12          # móvil: pincel más grande por defecto
        self.opacity = 1.0
        self.hardness = 1.0
        self.smoothing = 15     # móvil: algo de suavizado por defecto (dedo tiembla)
        self.stroke_type = "deterministic"
        self.pressure_mode = "velocity"
        self.tolerance = 24
        self.shape_mode = "outline"
        self.tile_x = False
        self.tile_y = False
        self.hand_mode = False  # solo móvil: pan con 1 dedo