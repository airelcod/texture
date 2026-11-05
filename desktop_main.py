"""
Frontal de escritorio del Creador de Texturas.
Todo lo específico de desktop: menú bar, sidebar, panel de capas, atajos, grips.
"""
from __future__ import annotations

import math
import os
import sys

from PySide6.QtCore import (
    QEvent, QPoint, QPointF, QRect, QRectF, QRegularExpression,
    QSize, Qt, QTimer, QObject, Signal,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QBrush, QColor, QIcon, QImage, QKeySequence,
    QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap,
    QPolygonF, QRadialGradient, QRegularExpressionValidator,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QButtonGroup,
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenuBar,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSpinBox, QToolButton, QUndoView, QVBoxLayout, QWidget,
)

from core import (
    APP_NAME, PROJECT_EXT, MAX_DOC_SIZE, UNDO_LIMIT,
    BG0, BG1, BG2, BG3, BORDER, TEXT, MUTED, ACCENT, CANVAS_BG,
    build_stylesheet, clamp, mid, quad_point, premultiplied_argb,
    image_pixels_u32, flood_fill_u32, offset_image, invert_image,
    grayscale_image, checker_pixmap, make_icon, make_logo, ICON_PAINTERS,
    BLEND_MODES, Layer, PatchCommand, StackCommand, PropertyCommand,
    Document, save_project, load_project, document_from_image, ToolSettings,
)


def apply_dark_palette(app: QApplication):
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG1))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor(BG0))
    pal.setColor(QPalette.AlternateBase, QColor(BG2))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(BG2))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ToolTipBase, QColor(BG2))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("white"))
    pal.setColor(QPalette.PlaceholderText, QColor(MUTED))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#5d5d6a"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5d5d6a"))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#5d5d6a"))
    app.setPalette(pal)


# ============================================================
#  LIENZO (desktop)
# ============================================================
class Canvas(QWidget):
    zoomChanged = Signal(float)
    cursorMoved = Signal(int, int)
    colorPicked = Signal(QColor)
    colorUsed = Signal(QColor)
    message = Signal(str)

    DRAW_TOOLS = ("brush", "eraser")
    SHAPE_TOOLS = ("line", "rect", "ellipse")

    def __init__(self, settings: ToolSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.doc = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(300, 300)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)

        # vista
        self.zoom = 1.0
        self.min_zoom, self.max_zoom = 0.02, 64.0
        self.offset = QPointF(0, 0)
        self._fitted = True
        self.show_grid = False
        self.grid_size = 64
        self.tile_preview = False

        # interacción
        self._hover = None
        self._cursor_rect = QRect()
        self._space = False
        self._panning = False
        self._pan_button = None
        self._pan_start = QPointF()
        self._pan_off0 = QPointF()
        self._picking = False
        self._pressure = 1.0

        # trazo
        self._stroking = False
        self._layer = None
        self._base = None
        self._buf = None
        self._dirty = QRect()
        self._buf_dirty = QRect()
        self._pending = {}
        self._pts = []
        self._sp = QPointF()
        self._speed = 0.0
        self._last_screen = QPointF()
        self._remain = 0.0
        self._path = QPainterPath()
        self._shape_start = QPointF()
        self._shape_end = QPointF()
        self._eraser = False
        self._opacity = 1.0
        self._pen_color = QColor(0, 0, 0)
        self._dab = None
        self._dab_key = None
        self._dab_ref = 1.0

        # gestos táctiles (laptop táctil / pantalla táctil)
        self._touch_points = {}
        self._gesture_active = False
        self._pinch_start = None

    # ---------- Documento ----------
    def set_document(self, doc: Document):
        if self.doc is not None:
            for sig, slot in ((self.doc.content_changed, self._on_content),
                              (self.doc.structure_changed, self._on_structure)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._abort_stroke_silently()
        self.doc = doc
        doc.content_changed.connect(self._on_content)
        doc.structure_changed.connect(self._on_structure)
        self._buf = QImage(doc.width_px, doc.height_px, QImage.Format_ARGB32_Premultiplied)
        self._buf.fill(QColor(0, 0, 0, 0))
        self._fitted = True
        self.fit_view()
        self.update()

    def is_stroking(self):
        return self._stroking

    def _on_structure(self):
        self.update()

    def _on_content(self, rect: QRect):
        if self.tile_preview:
            self.update()
        else:
            self.update(self._image_rect_to_widget(rect))

    # ---------- Coordenadas / vista ----------
    def screen_to_image(self, pos: QPointF) -> QPointF:
        return QPointF((pos.x() - self.offset.x()) / self.zoom,
                       (pos.y() - self.offset.y()) / self.zoom)

    def _image_rect_to_widget(self, r: QRect) -> QRect:
        x = self.offset.x() + r.x() * self.zoom
        y = self.offset.y() + r.y() * self.zoom
        return QRectF(x, y, r.width() * self.zoom, r.height() * self.zoom) \
            .toAlignedRect().adjusted(-2, -2, 2, 2)

    def set_zoom(self, new_zoom, anchor: QPointF = None):
        if self.doc is None:
            return
        new_zoom = clamp(new_zoom, self.min_zoom, self.max_zoom)
        if anchor is None:
            anchor = QPointF(self.width() / 2, self.height() / 2)
        img_pt = self.screen_to_image(anchor)
        self.zoom = new_zoom
        self.offset = QPointF(anchor.x() - img_pt.x() * self.zoom,
                              anchor.y() - img_pt.y() * self.zoom)
        self._fitted = False
        self.zoomChanged.emit(self.zoom)
        self.update()

    def zoom_in(self):
        self.set_zoom(self.zoom * 1.25)

    def zoom_out(self):
        self.set_zoom(self.zoom / 1.25)

    def zoom_actual(self):
        self.set_zoom(1.0)

    def fit_view(self, margin=36):
        if self.doc is None or self.width() < 10 or self.height() < 10:
            return
        k = 3 if self.tile_preview else 1
        dw, dh = self.doc.width_px * k, self.doc.height_px * k
        z = min((self.width() - 2 * margin) / dw, (self.height() - 2 * margin) / dh)
        z = clamp(z, self.min_zoom, 8.0)
        self.zoom = z
        self.offset = QPointF((self.width() - self.doc.width_px * z) / 2,
                              (self.height() - self.doc.height_px * z) / 2)
        self._fitted = True
        self.zoomChanged.emit(self.zoom)
        self.update()

    def set_tile_preview(self, on: bool):
        self.tile_preview = on
        self.fit_view()
        self.update()

    def set_show_grid(self, on: bool):
        self.show_grid = on
        self.update()

    def resizeEvent(self, event):
        if self._fitted:
            self.fit_view()
        super().resizeEvent(event)

    # ---------- Cursor ----------
    def refresh_cursor(self):
        t = self.settings.tool
        if self._panning:
            self.setCursor(Qt.ClosedHandCursor)
        elif self._space or t == "hand":
            self.setCursor(Qt.OpenHandCursor)
        elif t in ("fill", "picker"):
            self.setCursor(Qt.CrossCursor)
        else:
            self.setCursor(Qt.BlankCursor)
        self.update()

    def _cursor_radius(self):
        return max(1.5, self.settings.size * self.zoom / 2.0)

    def _update_cursor_region(self):
        old = self._cursor_rect
        if self._hover is None:
            self._cursor_rect = QRect()
        else:
            r = int(self._cursor_radius()) + 6
            self._cursor_rect = QRect(int(self._hover.x()) - r,
                                      int(self._hover.y()) - r, 2 * r, 2 * r)
        region = old.united(self._cursor_rect)
        if not region.isEmpty():
            self.update(region)

    # ---------- Pintado del widget ----------
    def paintEvent(self, event):
        p = QPainter(self)
        clip = QRectF(event.rect())
        p.fillRect(event.rect(), QColor(CANVAS_BG))
        if self.doc is None:
            return
        doc = self.doc
        w, h, z = doc.width_px, doc.height_px, self.zoom
        center = QRectF(self.offset.x(), self.offset.y(), w * z, h * z)
        rng = (-1, 0, 1) if self.tile_preview else (0,)
        smooth = z < 1.0
        p.setRenderHint(QPainter.SmoothPixmapTransform, smooth)

        if not self.tile_preview:
            p.fillRect(center.translated(4, 4), QColor(0, 0, 0, 90))

        checker = QBrush(checker_pixmap())
        for j in rng:
            for i in rng:
                target = QRectF(self.offset.x() + i * w * z,
                                self.offset.y() + j * h * z, w * z, h * z)
                vis = target.intersected(clip)
                if vis.isEmpty():
                    continue
                p.setBrushOrigin(target.topLeft())
                p.fillRect(vis, checker)
                src = QRectF((vis.left() - target.left()) / z,
                             (vis.top() - target.top()) / z,
                             vis.width() / z, vis.height() / z)
                p.drawImage(vis, doc.flat, src)

        # Rejilla de píxeles (zoom alto)
        if z >= 8.0:
            vis = center.intersected(clip)
            if not vis.isEmpty():
                p.setPen(QPen(QColor(0, 0, 0, 55), 1))
                x0 = int(math.floor((vis.left() - center.left()) / z))
                x1 = int(math.ceil((vis.right() - center.left()) / z))
                y0 = int(math.floor((vis.top() - center.top()) / z))
                y1 = int(math.ceil((vis.bottom() - center.top()) / z))
                for ix in range(x0, x1 + 1):
                    sx = center.left() + ix * z
                    p.drawLine(QPointF(sx, vis.top()), QPointF(sx, vis.bottom()))
                for iy in range(y0, y1 + 1):
                    sy = center.top() + iy * z
                    p.drawLine(QPointF(vis.left(), sy), QPointF(vis.right(), sy))

        # Rejilla del usuario
        if self.show_grid and self.grid_size * z >= 6:
            vis = center.intersected(clip)
            if not vis.isEmpty():
                p.setPen(QPen(QColor(110, 123, 255, 120), 1))
                step = self.grid_size
                k0 = int(math.floor((vis.left() - center.left()) / z / step))
                k1 = int(math.ceil((vis.right() - center.left()) / z / step))
                for k in range(k0, k1 + 1):
                    sx = center.left() + k * step * z
                    p.drawLine(QPointF(sx, vis.top()), QPointF(sx, vis.bottom()))
                k0 = int(math.floor((vis.top() - center.top()) / z / step))
                k1 = int(math.ceil((vis.bottom() - center.top()) / z / step))
                for k in range(k0, k1 + 1):
                    sy = center.top() + k * step * z
                    p.drawLine(QPointF(vis.left(), sy), QPointF(vis.right(), sy))

        p.setRenderHint(QPainter.Antialiasing, False)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(ACCENT) if self.tile_preview else QColor(90, 90, 105), 1))
        p.drawRect(center)

        t = self.settings.tool
        if (self._hover is not None and not self._panning and not self._space
                and t in self.DRAW_TOOLS + self.SHAPE_TOOLS):
            p.setRenderHint(QPainter.Antialiasing, True)
            r = self._cursor_radius()
            p.setPen(QPen(QColor(0, 0, 0, 200), 2))
            p.drawEllipse(self._hover, r, r)
            p.setPen(QPen(QColor(255, 255, 255, 230), 1))
            p.drawEllipse(self._hover, r, r)

    # ---------- Eventos ----------
    def event(self, e):
        t = e.type()
        if t in (QEvent.TouchBegin, QEvent.TouchUpdate,
                 QEvent.TouchEnd, QEvent.TouchCancel):
            return self._handle_touch(e)
        return super().event(e)

    def _handle_touch(self, e) -> bool:
        for tp in e.points():
            if e.type() in (QEvent.TouchEnd, QEvent.TouchCancel):
                self._touch_points.pop(tp.id(), None)
            else:
                self._touch_points[tp.id()] = QPointF(tp.position())
        n = len(self._touch_points)
        if n >= 2:
            if self._stroking:
                self.cancel_stroke()
            self._gesture_active = True
            self._update_pinch()
            e.accept()
            return True
        if n == 1 and not self._gesture_active:
            e.ignore()
            return False
        if n == 0:
            self._gesture_active = False
            self._pinch_start = None
            e.ignore()
            return False
        return True

    def _update_pinch(self):
        pts = list(self._touch_points.values())[:2]
        center = (pts[0] + pts[1]) / 2
        dx = pts[1].x() - pts[0].x()
        dy = pts[1].y() - pts[0].y()
        dist = math.hypot(dx, dy)
        if self._pinch_start is None:
            self._pinch_start = (dist, self.zoom, center, QPointF(self.offset))
            return
        d0, z0, c0, o0 = self._pinch_start
        if d0 > 5:
            new_zoom = clamp(z0 * (dist / d0), self.min_zoom, self.max_zoom)
        else:
            new_zoom = z0
        self.offset = o0 + (center - c0)
        self.set_zoom(new_zoom, anchor=center)

    def tabletEvent(self, e):
        if e.type() in (QEvent.TabletPress, QEvent.TabletMove):
            self._pressure = float(e.pressure())
        else:
            self._pressure = 1.0
        e.ignore()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._space = True
            self.refresh_cursor()
            return
        if e.key() == Qt.Key_Escape and self._stroking:
            self.cancel_stroke()
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._space = False
            self.refresh_cursor()
            return
        super().keyReleaseEvent(e)

    def _start_pan(self, e):
        self._panning = True
        self._pan_button = e.button()
        self._pan_start = e.position()
        self._pan_off0 = QPointF(self.offset)
        self.refresh_cursor()

    def mousePressEvent(self, e):
        if self._gesture_active:
            return
        self.setFocus()
        self._hover = e.position()
        if self.doc is None or self._stroking:
            return
        btn = e.button()
        if btn in (Qt.RightButton, Qt.MiddleButton):
            self._start_pan(e)
            return
        if btn != Qt.LeftButton:
            return
        if self._space or self.settings.tool == "hand":
            self._start_pan(e)
            return
        pos = self.screen_to_image(e.position())
        tool = self.settings.tool
        if tool == "picker" or (e.modifiers() & Qt.AltModifier):
            self._picking = True
            self._pick(pos)
            return
        if tool == "fill":
            self._do_fill(pos)
            return
        self._begin_stroke(pos, e)

    def mouseMoveEvent(self, e):
        if self._gesture_active:
            return
        self._hover = e.position()
        if self.doc is not None:
            ip = self.screen_to_image(e.position())
            ix, iy = int(math.floor(ip.x())), int(math.floor(ip.y()))
            inside = 0 <= ix < self.doc.width_px and 0 <= iy < self.doc.height_px
            self.cursorMoved.emit(ix if inside else -1, iy if inside else -1)
        if self._panning:
            self.offset = self._pan_off0 + (e.position() - self._pan_start)
            self._fitted = False
            self.update()
            return
        if self._picking:
            self._pick(self.screen_to_image(e.position()))
        elif self._stroking:
            self._stroke_move(e)
        self._update_cursor_region()

    def mouseReleaseEvent(self, e):
        if self._panning and e.button() == self._pan_button:
            self._panning = False
            self._pan_button = None
            self.refresh_cursor()
            return
        if e.button() != Qt.LeftButton:
            return
        if self._picking:
            self._picking = False
        elif self._stroking:
            self._finish_stroke()

    def leaveEvent(self, e):
        self._hover = None
        self.cursorMoved.emit(-1, -1)
        self._update_cursor_region()
        super().leaveEvent(e)

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if dy == 0:
            return
        self.set_zoom(self.zoom * (1.15 if dy > 0 else 1 / 1.15), anchor=e.position())

    # ---------- Cuentagotas / relleno ----------
    def _pick(self, pos: QPointF):
        x, y = int(math.floor(pos.x())), int(math.floor(pos.y()))
        if not (0 <= x < self.doc.width_px and 0 <= y < self.doc.height_px):
            return
        c = self.doc.flat.pixelColor(x, y)
        if c.alpha() == 0:
            return
        c.setAlpha(255)
        self.colorPicked.emit(c)

    def _do_fill(self, pos: QPointF):
        ok, why = self.doc.can_paint()
        if not ok:
            self.message.emit(why)
            return
        x, y = int(math.floor(pos.x())), int(math.floor(pos.y()))
        layer = self.doc.active_layer
        w, h = self.doc.width_px, self.doc.height_px
        if not (0 <= x < w and 0 <= y < h):
            return
        before_full = layer.image.copy()
        c = QColor(self.settings.color)
        c.setAlphaF(clamp(c.alphaF() * self.settings.opacity, 0.0, 1.0))
        px = image_pixels_u32(layer.image)
        bbox = flood_fill_u32(px, w, h, x, y, premultiplied_argb(c),
                              int(self.settings.tolerance * 2.55))
        del px
        if bbox is None:
            return
        x0, y0, x1, y1 = bbox
        rect = QRect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
        self.doc.undo_stack.push(PatchCommand(
            self.doc, layer, rect, before_full.copy(rect),
            layer.image.copy(rect), "Relleno"))
        self.doc.notify_content(rect)
        self.colorUsed.emit(self.settings.color)

    # ---------- Trazo: utilidades ----------
    def _wrap_offsets(self, bounds: QRectF):
        s = self.settings
        w, h = self.doc.width_px, self.doc.height_px
        xs = (-w, 0, w) if s.tile_x else (0,)
        ys = (-h, 0, h) if s.tile_y else (0,)
        img = QRectF(0, 0, w, h)
        out = []
        for dy in ys:
            for dx in xs:
                if bounds.translated(dx, dy).intersects(img):
                    out.append((dx, dy))
        return out

    def _add_pending(self, key, rect_f: QRectF):
        r = rect_f.toAlignedRect().adjusted(-1, -1, 1, 1).intersected(self.doc.full_rect())
        if r.isEmpty():
            return
        cur = self._pending.get(key)
        self._pending[key] = r if cur is None else cur.united(r)
        self._dirty = self._dirty.united(r)
        self._buf_dirty = self._buf_dirty.united(r)

    def _clear_buffer(self):
        if self._buf_dirty.isEmpty():
            return
        p = QPainter(self._buf)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self._buf_dirty, QColor(0, 0, 0, 0))
        p.end()
        cur = self._pending.get("clear")
        self._pending["clear"] = self._buf_dirty if cur is None else cur.united(self._buf_dirty)
        self._buf_dirty = QRect()

    def _flush(self):
        if not self._pending or self._layer is None:
            self._pending = {}
            return
        rects = list(self._pending.values())
        self._pending = {}
        p = QPainter(self._layer.image)
        for r in rects:
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.setOpacity(1.0)
            p.drawImage(r.topLeft(), self._base, r)
            p.setCompositionMode(QPainter.CompositionMode_DestinationOut
                                 if self._eraser
                                 else QPainter.CompositionMode_SourceOver)
            p.setOpacity(self._opacity)
            p.drawImage(r.topLeft(), self._buf, r)
        p.end()
        for r in rects:
            self.doc.notify_content(r)

    def _make_pen(self, width):
        return QPen(self._pen_color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)

    # ---------- Trazo: ciclo de vida ----------
    def _begin_stroke(self, pos: QPointF, e):
        ok, why = self.doc.can_paint()
        if not ok:
            self.message.emit(why)
            return
        s = self.settings
        tool = s.tool
        self._layer = self.doc.active_layer
        self._base = self._layer.image.copy()
        self._dirty = QRect()
        self._buf_dirty = QRect()
        self._pending = {}
        self._stroking = True
        self._eraser = tool == "eraser"
        self._opacity = s.opacity * (1.0 if self._eraser else s.color.alphaF())
        self._pen_color = (QColor(0, 0, 0) if self._eraser
                           else QColor(s.color.red(), s.color.green(), s.color.blue()))
        self._pts = [QPointF(pos)]
        self._sp = QPointF(pos)
        self._speed = 0.0
        self._last_screen = e.position()

        if tool in self.DRAW_TOOLS:
            if s.stroke_type == "free":
                self._prepare_dab()
                self._remain = self._spacing()
                p = QPainter(self._buf)
                p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
                self._stamp(p, pos, self._dab_diameter())
                p.end()
            else:
                self._path = QPainterPath()
                self._path.moveTo(pos)
                self._redraw_deterministic()
        else:
            self._shape_start = QPointF(pos)
            self._shape_end = QPointF(pos)
        self._flush()

    def _stroke_move(self, e):
        s = self.settings
        pos = self.screen_to_image(e.position())
        d = math.hypot(e.position().x() - self._last_screen.x(),
                       e.position().y() - self._last_screen.y())
        self._speed = 0.75 * self._speed + 0.25 * d
        self._last_screen = e.position()

        if s.tool in self.DRAW_TOOLS:
            if s.stroke_type == "free":
                k = 1.0 - min(0.95, s.smoothing / 100.0 * 0.95)
                self._sp = QPointF(self._sp.x() + (pos.x() - self._sp.x()) * k,
                                   self._sp.y() + (pos.y() - self._sp.y()) * k)
                self._add_free_point(QPointF(self._sp))
            else:
                last = self._pts[-1]
                if math.hypot(pos.x() - last.x(), pos.y() - last.y()) >= 0.35:
                    self._add_det_point(QPointF(pos))
                    self._redraw_deterministic()
        else:
            end = QPointF(pos)
            if e.modifiers() & Qt.ShiftModifier:
                end = self._constrain(self._shape_start, end, s.tool)
            self._shape_end = end
            self._redraw_shape()
        self._flush()

    def _finish_stroke(self):
        s = self.settings
        if s.tool in self.DRAW_TOOLS:
            if s.stroke_type == "free":
                if len(self._pts) >= 2:
                    a = mid(self._pts[-2], self._pts[-1])
                    self._stamp_polyline([a, self._pts[-1]])
            else:
                self._redraw_deterministic()
        self._flush()
        self._clear_buffer()
        self._pending.pop("clear", None)

        rect = self._dirty
        layer, base = self._layer, self._base
        self._stroking = False
        self._layer = None
        self._base = None
        self._pending = {}
        if rect.isEmpty():
            return
        text = {"brush": "Pincel", "eraser": "Borrador", "line": "Línea",
                "rect": "Rectángulo", "ellipse": "Elipse"}.get(s.tool, "Trazo")
        self.doc.undo_stack.push(PatchCommand(
            self.doc, layer, rect, base.copy(rect),
            layer.image.copy(rect), text))
        if not self._eraser:
            self.colorUsed.emit(s.color)

    def cancel_stroke(self):
        if not self._stroking:
            return
        rect = self._dirty
        self._clear_buffer()
        self._pending = {}
        if not rect.isEmpty():
            p = QPainter(self._layer.image)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.drawImage(rect.topLeft(), self._base, rect)
            p.end()
            self.doc.notify_content(rect)
        self._stroking = False
        self._layer = None
        self._base = None
        self.message.emit("Trazo cancelado")

    def _abort_stroke_silently(self):
        self._stroking = False
        self._layer = None
        self._base = None
        self._pending = {}

    # ---------- Pincel libre ----------
    def _spacing(self):
        return max(0.7, self.settings.size * 0.08)

    def _dab_diameter(self):
        s = self.settings
        size = float(s.size)
        if s.pressure_mode == "velocity":
            v = min(1.0, self._speed / 40.0)
            return max(0.5, size * (1.0 - 0.65 * v))
        if s.pressure_mode == "tablet":
            return max(0.5, size * (0.1 + 0.9 * self._pressure))
        return size

    def _prepare_dab(self):
        s = self.settings
        size = max(1, int(s.size))
        hard = clamp(s.hardness, 0.0, 0.99)
        if s.hardness >= 0.995:
            hard = 1.0
        key = (size, round(hard, 3), self._pen_color.rgb())
        self._dab_ref = float(size)
        if key == self._dab_key and self._dab is not None:
            return
        S = size + 2
        dab = QImage(S, S, QImage.Format_ARGB32_Premultiplied)
        dab.fill(QColor(0, 0, 0, 0))
        p = QPainter(dab)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        c0 = QColor(self._pen_color); c0.setAlpha(255)
        circle = QRectF((S - size) / 2.0, (S - size) / 2.0, size, size)
        if hard >= 1.0:
            p.setBrush(c0)
        else:
            c1 = QColor(self._pen_color); c1.setAlpha(0)
            g = QRadialGradient(QPointF(S / 2.0, S / 2.0), size / 2.0)
            g.setColorAt(0.0, c0)
            g.setColorAt(hard, c0)
            g.setColorAt(1.0, c1)
            p.setBrush(QBrush(g))
        p.drawEllipse(circle)
        p.end()
        self._dab, self._dab_key = dab, key

    def _stamp(self, p: QPainter, c: QPointF, diameter: float):
        k = diameter / self._dab_ref
        half = self._dab.width() * k / 2.0
        target = QRectF(c.x() - half, c.y() - half, 2 * half, 2 * half)
        for off in self._wrap_offsets(target):
            tr = target.translated(off[0], off[1])
            p.drawImage(tr, self._dab)
            self._add_pending(off, tr)

    def _stamp_polyline(self, poly):
        sp = self._spacing()
        rem = self._remain
        diameter = self._dab_diameter()
        p = QPainter(self._buf)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        for i in range(1, len(poly)):
            a, b = poly[i - 1], poly[i]
            dx, dy = b.x() - a.x(), b.y() - a.y()
            seg = math.hypot(dx, dy)
            if seg < 1e-9:
                continue
            d = rem
            while d <= seg:
                t = d / seg
                self._stamp(p, QPointF(a.x() + dx * t, a.y() + dy * t), diameter)
                d += sp
            rem = d - seg
        p.end()
        self._remain = rem

    def _add_free_point(self, pt: QPointF):
        pts = self._pts
        pts.append(pt)
        n = len(pts)
        if n == 2:
            poly = [pts[0], mid(pts[0], pts[1])]
        else:
            a = mid(pts[-3], pts[-2])
            c = pts[-2]
            b = mid(pts[-2], pts[-1])
            length = math.hypot(c.x() - a.x(), c.y() - a.y()) + \
                     math.hypot(b.x() - c.x(), b.y() - c.y())
            steps = max(2, min(48, int(length / 2.0) + 1))
            poly = [quad_point(a, c, b, i / steps) for i in range(steps + 1)]
        self._stamp_polyline(poly)

    # ---------- Determinista ----------
    def _add_det_point(self, pt: QPointF):
        pts = self._pts
        pts.append(pt)
        n = len(pts)
        if n == 2:
            self._path.lineTo(mid(pts[0], pts[1]))
        else:
            self._path.quadTo(pts[-2], mid(pts[-2], pts[-1]))

    def _redraw_deterministic(self):
        self._clear_buffer()
        size = float(self.settings.size)
        pts = self._pts
        p = QPainter(self._buf)
        p.setRenderHint(QPainter.Antialiasing, True)
        if len(pts) == 1:
            c = pts[0]
            bounds = QRectF(c.x() - size, c.y() - size, 2 * size, 2 * size)
            p.setPen(Qt.NoPen)
            p.setBrush(self._pen_color)
            for off in self._wrap_offsets(bounds):
                p.drawEllipse(QPointF(c.x() + off[0], c.y() + off[1]), size / 2, size / 2)
                self._add_pending(off, bounds.translated(off[0], off[1]))
        else:
            path = QPainterPath(self._path)
            path.lineTo(pts[-1])
            bounds = path.controlPointRect().adjusted(-size, -size, size, size)
            p.setPen(self._make_pen(size))
            p.setBrush(Qt.NoBrush)
            for off in self._wrap_offsets(bounds):
                p.save()
                p.translate(off[0], off[1])
                p.drawPath(path)
                p.restore()
                self._add_pending(off, bounds.translated(off[0], off[1]))
        p.end()

    # ---------- Formas ----------
    @staticmethod
    def _constrain(a: QPointF, b: QPointF, tool: str) -> QPointF:
        dx, dy = b.x() - a.x(), b.y() - a.y()
        if tool == "line":
            ang = math.atan2(dy, dx)
            ang = round(ang / (math.pi / 4)) * (math.pi / 4)
            ln = math.hypot(dx, dy)
            return QPointF(a.x() + ln * math.cos(ang), a.y() + ln * math.sin(ang))
        side = max(abs(dx), abs(dy))
        return QPointF(a.x() + math.copysign(side, dx if dx else 1),
                       a.y() + math.copysign(side, dy if dy else 1))

    def _redraw_shape(self):
        self._clear_buffer()
        s = self.settings
        size = float(s.size)
        a, b = self._shape_start, self._shape_end
        rect = QRectF(a, b).normalized()
        fill = s.shape_mode == "fill" and s.tool != "line"
        p = QPainter(self._buf)
        p.setRenderHint(QPainter.Antialiasing, True)
        if fill:
            p.setPen(Qt.NoPen)
            p.setBrush(self._pen_color)
        else:
            pen = self._make_pen(size)
            if s.tool == "rect":
                pen.setJoinStyle(Qt.MiterJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
        bounds = rect.adjusted(-size, -size, size, size)
        for off in self._wrap_offsets(bounds):
            p.save()
            p.translate(off[0], off[1])
            if s.tool == "line":
                p.drawLine(a, b)
            elif s.tool == "rect":
                p.drawRect(rect)
            else:
                p.drawEllipse(rect)
            p.restore()
            self._add_pending(off, bounds.translated(off[0], off[1]))
        p.end()


# ============================================================
#  CHROMA RECT (desktop)
# ============================================================
class ChromaRect(QWidget):
    colorChanged = Signal(QColor)

    def __init__(self, color: QColor = QColor(255, 0, 0), parent=None):
        super().__init__(parent)
        self._h, self._s, self._v, self._a = 0.0, 1.0, 1.0, 255
        self._sq = QRectF()
        self._hue_rect = QRectF()
        self._alpha_rect = QRectF()
        self._sq_pix = None
        self._drag = None
        self.setMinimumSize(140, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_color(color)

    def sizeHint(self):
        return QSize(240, 190)

    def color(self) -> QColor:
        return QColor.fromHsvF(self._h, self._s, self._v, self._a / 255.0)

    def set_color(self, color: QColor):
        h, s, v, a = color.getHsvF()
        if h >= 0:
            self._h = min(h, 0.9999)
        self._s, self._v, self._a = s, v, int(round(a * 255))
        self._rebuild_square()
        self.update()

    def _emit(self):
        self.colorChanged.emit(self.color())

    def _layout(self):
        m, bar, gap = 2.0, 14.0, 9.0
        w = self.width() - 2 * m
        h = self.height() - 2 * m - 2 * (bar + gap)
        self._sq = QRectF(m, m, max(10.0, w), max(10.0, h))
        self._hue_rect = QRectF(m, self._sq.bottom() + gap, w, bar)
        self._alpha_rect = QRectF(m, self._hue_rect.bottom() + gap, w, bar)
        self._rebuild_square()

    def resizeEvent(self, e):
        self._layout()
        super().resizeEvent(e)

    def _rebuild_square(self):
        w, h = max(1, int(self._sq.width())), max(1, int(self._sq.height()))
        pm = QPixmap(w, h)
        p = QPainter(pm)
        gh = QLinearGradient(0, 0, w, 0)
        gh.setColorAt(0, QColor(255, 255, 255))
        gh.setColorAt(1, QColor.fromHsvF(self._h, 1.0, 1.0))
        p.fillRect(pm.rect(), QBrush(gh))
        gv = QLinearGradient(0, 0, 0, h)
        gv.setColorAt(0, QColor(0, 0, 0, 0))
        gv.setColorAt(1, QColor(0, 0, 0, 255))
        p.fillRect(pm.rect(), QBrush(gv))
        p.end()
        self._sq_pix = pm

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath(); path.addRoundedRect(self._sq, 5, 5)
        p.save(); p.setClipPath(path)
        if self._sq_pix is not None:
            p.drawPixmap(self._sq.toRect(), self._sq_pix)
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 35), 1)); p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(self._sq, 5, 5)
        x = self._sq.x() + self._s * self._sq.width()
        y = self._sq.y() + (1.0 - self._v) * self._sq.height()
        p.setPen(QPen(QColor(0, 0, 0, 200), 3)); p.drawEllipse(QPointF(x, y), 6, 6)
        p.setPen(QPen(QColor(255, 255, 255), 1.5)); p.drawEllipse(QPointF(x, y), 6, 6)

        g = QLinearGradient(self._hue_rect.topLeft(), self._hue_rect.topRight())
        for i in range(7):
            g.setColorAt(i / 6.0, QColor.fromHsvF(min(i / 6.0, 0.9999), 1, 1))
        p.setPen(Qt.NoPen); p.setBrush(QBrush(g))
        p.drawRoundedRect(self._hue_rect, 5, 5)
        self._draw_handle(p, self._hue_rect, self._h, QColor.fromHsvF(self._h, 1, 1))

        p.setBrush(QBrush(checker_pixmap(4, "#3a3a42", "#55555f")))
        p.setBrushOrigin(self._alpha_rect.topLeft())
        p.drawRoundedRect(self._alpha_rect, 5, 5)
        base = QColor.fromHsvF(self._h, self._s, self._v)
        ga = QLinearGradient(self._alpha_rect.topLeft(), self._alpha_rect.topRight())
        c0 = QColor(base); c0.setAlpha(0)
        ga.setColorAt(0, c0); ga.setColorAt(1, QColor(base))
        p.setBrush(QBrush(ga)); p.drawRoundedRect(self._alpha_rect, 5, 5)
        self._draw_handle(p, self._alpha_rect, self._a / 255.0, QColor(base))

    @staticmethod
    def _draw_handle(p, bar, t, fill):
        x = bar.x() + t * bar.width()
        r = bar.height() / 2.0 + 1.5
        p.setPen(QPen(QColor(0, 0, 0, 160), 3)); p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(x, bar.center().y()), r, r)
        p.setPen(QPen(QColor(255, 255, 255), 1.6)); p.setBrush(fill)
        p.drawEllipse(QPointF(x, bar.center().y()), r, r)

    def _update_from(self, pos: QPointF):
        if self._drag == "square":
            r = self._sq
            self._s = clamp((pos.x() - r.x()) / r.width(), 0.0, 1.0)
            self._v = 1.0 - clamp((pos.y() - r.y()) / r.height(), 0.0, 1.0)
        elif self._drag == "hue":
            r = self._hue_rect
            self._h = clamp((pos.x() - r.x()) / r.width(), 0.0, 1.0) * 0.9999
            self._rebuild_square()
        elif self._drag == "alpha":
            r = self._alpha_rect
            self._a = int(round(clamp((pos.x() - r.x()) / r.width(), 0.0, 1.0) * 255))
        self.update()
        self._emit()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        if self._hue_rect.adjusted(-4, -5, 4, 5).contains(pos):
            self._drag = "hue"
        elif self._alpha_rect.adjusted(-4, -5, 4, 5).contains(pos):
            self._drag = "alpha"
        elif self._sq.adjusted(-4, -4, 4, 4).contains(pos):
            self._drag = "square"
        else:
            return
        self._update_from(pos)

    def mouseMoveEvent(self, e):
        if self._drag:
            self._update_from(e.position())

    def mouseReleaseEvent(self, e):
        self._drag = None


# ============================================================
#  WIDGETS AUXILIARES
# ============================================================
class Swatch(QWidget):
    clicked = Signal(QColor)

    def __init__(self, color: QColor, size=18, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._color.alpha() < 255:
            p.setBrush(QBrush(checker_pixmap(4, "#3a3a42", "#55555f")))
            p.setPen(Qt.NoPen); p.drawRoundedRect(r, 4, 4)
        p.setBrush(self._color)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawRoundedRect(r, 4, 4)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(QColor(self._color))


class ColorChip(QWidget):
    def __init__(self, color: QColor, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(32, 32)

    def set_color(self, c: QColor):
        self._color = QColor(c); self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(checker_pixmap(4, "#3a3a42", "#55555f")))
        p.drawRoundedRect(r, 7, 7)
        p.setBrush(self._color)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1.5))
        p.drawRoundedRect(r, 7, 7)


class LabeledSlider(QWidget):
    valueChanged = Signal(int)

    def __init__(self, text, lo, hi, value, suffix="", parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(3)
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(text); self.label.setObjectName("Muted")
        self.spin = QSpinBox()
        self.spin.setRange(lo, hi); self.spin.setSuffix(suffix)
        self.spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.spin.setAlignment(Qt.AlignRight)
        self.spin.setFixedWidth(64)
        top.addWidget(self.label); top.addStretch(); top.addWidget(self.spin)
        v.addLayout(top)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(lo, hi)
        v.addWidget(self.slider)
        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        self.slider.valueChanged.connect(self.valueChanged)
        self.setValue(value)

    def value(self):
        return self.slider.value()

    def setValue(self, v):
        self.slider.setValue(v)


class CollapsibleSection(QWidget):
    def __init__(self, title: str, expanded=True, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)
        self._title = title
        self._header = QPushButton()
        self._header.setObjectName("SectionHeader")
        self._header.setCheckable(True)
        self._header.setChecked(expanded)
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.clicked.connect(self._toggle)
        lay.addWidget(self._header)
        self._content = QWidget()
        self._cl = QVBoxLayout(self._content)
        self._cl.setContentsMargins(6, 10, 4, 6); self._cl.setSpacing(9)
        lay.addWidget(self._content)
        self._content.setVisible(expanded)
        self._update_title()

    def content_layout(self):
        return self._cl

    def _toggle(self):
        self._content.setVisible(self._header.isChecked())
        self._update_title()

    def _update_title(self):
        self._header.setText(f"{'▾' if self._header.isChecked() else '▸'}   {self._title}")


# ============================================================
#  PANEL DE COLOR
# ============================================================
DEFAULT_PALETTE = [
    "#000000", "#3b3b3b", "#7a7a7a", "#bdbdbd", "#ffffff",
    "#e53935", "#fb8c00", "#fdd835", "#43a047", "#00acc1",
    "#1e88e5", "#5e35b1", "#d81b60", "#6d4c41", "#8d6e63",
    "#a5d6a7", "#90caf9", "#ce93d8", "#ffcc80", "#80cbc4",
]


class ColorPanel(QWidget):
    colorChanged = Signal(QColor)

    def __init__(self, color: QColor, parent=None):
        super().__init__(parent)
        from collections import deque
        self._updating = False
        self._recent = deque(maxlen=10)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(9)

        self.chroma = ChromaRect(color)
        self.chroma.setFixedHeight(196)
        self.chroma.colorChanged.connect(self._on_chroma)
        v.addWidget(self.chroma)

        row = QHBoxLayout(); row.setSpacing(6)
        lab = QLabel("Hex"); lab.setObjectName("Muted")
        self.hex_edit = QLineEdit()
        self.hex_edit.setMaxLength(7)
        self.hex_edit.setValidator(QRegularExpressionValidator(
            QRegularExpression("#?[0-9a-fA-F]{0,6}")))
        self.hex_edit.editingFinished.connect(self._on_hex)
        row.addWidget(lab); row.addWidget(self.hex_edit, 1)
        v.addLayout(row)

        grid = QGridLayout(); grid.setHorizontalSpacing(6); grid.setVerticalSpacing(5)
        self.spins = {}
        for i, key in enumerate("RGBA"):
            l = QLabel(key); l.setObjectName("Muted"); l.setFixedWidth(12)
            sp = QSpinBox(); sp.setRange(0, 255)
            sp.setButtonSymbols(QAbstractSpinBox.NoButtons)
            sp.valueChanged.connect(self._on_spin)
            self.spins[key] = sp
            grid.addWidget(l, i // 2, (i % 2) * 2)
            grid.addWidget(sp, i // 2, (i % 2) * 2 + 1)
        v.addLayout(grid)

        pal_lab = QLabel("Paleta"); pal_lab.setObjectName("Muted")
        v.addWidget(pal_lab)
        pal = QGridLayout(); pal.setSpacing(4)
        for i, hexc in enumerate(DEFAULT_PALETTE):
            sw = Swatch(QColor(hexc))
            sw.clicked.connect(self._on_swatch)
            pal.addWidget(sw, i // 10, i % 10)
        v.addLayout(pal)

        rec_lab = QLabel("Recientes"); rec_lab.setObjectName("Muted")
        v.addWidget(rec_lab)
        self._recent_layout = QHBoxLayout(); self._recent_layout.setSpacing(4)
        self._recent_layout.addStretch()
        v.addLayout(self._recent_layout)

        self._sync_widgets(color)

    def color(self):
        return self.chroma.color()

    def set_color(self, c: QColor, emit=True):
        self.chroma.set_color(c)
        self._sync_widgets(c)
        if emit:
            self.colorChanged.emit(QColor(c))

    def add_recent(self, c: QColor):
        c = QColor(c)
        key = c.name(QColor.HexArgb)
        if self._recent and self._recent[0].name(QColor.HexArgb) == key:
            return
        self._recent = type(self._recent)(
            (x for x in self._recent if x.name(QColor.HexArgb) != key), maxlen=10)
        self._recent.appendleft(c)
        while self._recent_layout.count():
            it = self._recent_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        for col in self._recent:
            sw = Swatch(col)
            sw.clicked.connect(self._on_swatch)
            self._recent_layout.addWidget(sw)
        self._recent_layout.addStretch()

    def _sync_widgets(self, c: QColor):
        self._updating = True
        self.hex_edit.setText(c.name(QColor.HexRgb))
        for key, val in zip("RGBA", (c.red(), c.green(), c.blue(), c.alpha())):
            self.spins[key].setValue(val)
        self._updating = False

    def _on_chroma(self, c):
        self._sync_widgets(c)
        self.colorChanged.emit(QColor(c))

    def _on_spin(self):
        if self._updating:
            return
        c = QColor(self.spins["R"].value(), self.spins["G"].value(),
                   self.spins["B"].value(), self.spins["A"].value())
        self.chroma.set_color(c)
        self._updating = True
        self.hex_edit.setText(c.name(QColor.HexRgb))
        self._updating = False
        self.colorChanged.emit(c)

    def _on_hex(self):
        if self._updating:
            return
        txt = self.hex_edit.text().strip()
        if not txt.startswith("#"):
            txt = "#" + txt
        c = QColor(txt)
        if not c.isValid():
            self._sync_widgets(self.chroma.color())
            return
        c.setAlpha(self.spins["A"].value())
        self.set_color(c)

    def _on_swatch(self, c):
        self.set_color(c)


# ============================================================
#  PANEL DE CAPAS
# ============================================================
def make_thumb(image: QImage, size=36) -> QPixmap:
    pm = QPixmap(size, size)
    p = QPainter(pm)
    p.fillRect(pm.rect(), QBrush(checker_pixmap(4, "#3a3a42", "#55555f")))
    mode = Qt.SmoothTransformation if max(image.width(), image.height()) <= 1536 \
        else Qt.FastTransformation
    small = image.scaled(size, size, Qt.KeepAspectRatio, mode)
    p.drawImage((size - small.width()) // 2, (size - small.height()) // 2, small)
    p.setPen(QPen(QColor(255, 255, 255, 30), 1)); p.setBrush(Qt.NoBrush)
    p.drawRect(0, 0, size - 1, size - 1)
    p.end()
    return pm


class LayerRow(QWidget):
    visibility_clicked = Signal(object)
    lock_clicked = Signal(object)
    rename_requested = Signal(object)

    def __init__(self, layer, active, thumb, parent=None):
        super().__init__(parent)
        self.layer = layer
        self.setObjectName("LayerRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setProperty("active", "true" if active else "false")
        self.setFixedHeight(48)
        h = QHBoxLayout(self)
        h.setContentsMargins(5, 4, 8, 4); h.setSpacing(4)

        self.btn_vis = QToolButton()
        self.btn_vis.setIcon(make_icon("eye" if layer.visible else "eye_off"))
        self.btn_vis.setIconSize(QSize(18, 18)); self.btn_vis.setFixedSize(24, 24)
        self.btn_vis.clicked.connect(lambda: self.visibility_clicked.emit(self.layer))
        h.addWidget(self.btn_vis)

        self.btn_lock = QToolButton()
        self.btn_lock.setIcon(make_icon("lock" if layer.locked else "unlock"))
        self.btn_lock.setIconSize(QSize(18, 18)); self.btn_lock.setFixedSize(24, 24)
        self.btn_lock.clicked.connect(lambda: self.lock_clicked.emit(self.layer))
        h.addWidget(self.btn_lock)

        self.thumb = QLabel(); self.thumb.setFixedSize(36, 36)
        self.thumb.setPixmap(thumb)
        h.addWidget(self.thumb)

        self.name_label = QLabel(layer.name)
        self.name_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        if not layer.visible:
            self.name_label.setStyleSheet(f"color:{MUTED};")
        h.addWidget(self.name_label, 1)

    def set_active(self, active: bool):
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_thumb(self, pm: QPixmap):
        self.thumb.setPixmap(pm)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.rename_requested.emit(self.layer)


class LayerPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = None
        self._syncing = False
        self._rows = []
        self._thumbs = {}
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(220)
        self._thumb_timer.timeout.connect(self._refresh_thumbs)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)

        self.list = QListWidget()
        self.list.setObjectName("LayerList")
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        v.addWidget(self.list, 1)

        btns = QHBoxLayout(); btns.setSpacing(4)
        self.btn_add = self._btn("plus", "Nueva capa")
        self.btn_dup = self._btn("copy", "Duplicar capa")
        self.btn_up = self._btn("up", "Subir capa")
        self.btn_down = self._btn("down", "Bajar capa")
        self.btn_merge = self._btn("merge", "Combinar hacia abajo")
        self.btn_del = self._btn("trash", "Eliminar capa")
        for b in (self.btn_add, self.btn_dup, self.btn_up,
                  self.btn_down, self.btn_merge, self.btn_del):
            btns.addWidget(b)
        v.addLayout(btns)

        self.opacity = LabeledSlider("Opacidad de la capa", 0, 100, 100, " %")
        v.addWidget(self.opacity)
        brow = QHBoxLayout()
        bl = QLabel("Fusión"); bl.setObjectName("Muted")
        self.blend = QComboBox()
        for key, (label, _) in BLEND_MODES.items():
            self.blend.addItem(label, key)
        brow.addWidget(bl); brow.addWidget(self.blend, 1)
        v.addLayout(brow)

        self.btn_add.clicked.connect(lambda: self.doc and self.doc.add_layer())
        self.btn_dup.clicked.connect(lambda: self.doc and self.doc.duplicate_active())
        self.btn_up.clicked.connect(lambda: self.doc and self.doc.move_active(+1))
        self.btn_down.clicked.connect(lambda: self.doc and self.doc.move_active(-1))
        self.btn_merge.clicked.connect(self._merge)
        self.btn_del.clicked.connect(lambda: self.doc and self.doc.remove_active())
        self.opacity.valueChanged.connect(self._on_opacity)
        self.blend.currentIndexChanged.connect(self._on_blend)
        self.list.currentRowChanged.connect(self._on_row_selected)
        self.list.model().rowsMoved.connect(self._on_rows_moved)

    @staticmethod
    def _btn(icon, tip):
        b = QToolButton()
        b.setIcon(make_icon(icon))
        b.setIconSize(QSize(18, 18))
        b.setFixedHeight(28)
        b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        b.setToolTip(tip)
        return b

    def set_document(self, doc: Document):
        if self.doc is not None:
            for sig, slot in ((self.doc.structure_changed, self._rebuild),
                              (self.doc.active_changed, self._on_active_changed),
                              (self.doc.content_changed, self._on_content)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.doc = doc
        self._thumbs.clear()
        doc.structure_changed.connect(self._rebuild)
        doc.active_changed.connect(self._on_active_changed)
        doc.content_changed.connect(self._on_content)
        self._rebuild()

    def _thumb_for(self, layer):
        pm = self._thumbs.get(id(layer))
        if pm is None:
            pm = make_thumb(layer.image)
            self._thumbs[id(layer)] = pm
        return pm

    def _rebuild(self):
        if self.doc is None:
            return
        self._syncing = True
        scroll = self.list.verticalScrollBar().value()
        self.list.blockSignals(True)
        self.list.clear()
        self._rows = []
        layers, active = self.doc.layers, self.doc.active_index
        n = len(layers)
        for vis in range(n):
            idx = n - 1 - vis
            L = layers[idx]
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, idx)
            item.setSizeHint(QSize(0, 52))
            row = LayerRow(L, idx == active, self._thumb_for(L))
            row.visibility_clicked.connect(self._toggle_visible)
            row.lock_clicked.connect(self._toggle_lock)
            row.rename_requested.connect(self._rename)
            self.list.setItemWidget(item, row)
            self._rows.append(row)
        if 0 <= active < n:
            self.list.setCurrentRow(n - 1 - active)
        self.list.blockSignals(False)
        self.list.verticalScrollBar().setValue(scroll)
        self._sync_controls()
        self._syncing = False

    def _on_active_changed(self):
        if self.doc is None:
            return
        self._syncing = True
        n = len(self.doc.layers)
        active = self.doc.active_index
        self.list.blockSignals(True)
        if 0 <= active < n:
            self.list.setCurrentRow(n - 1 - active)
        self.list.blockSignals(False)
        for row in self._rows:
            row.set_active(row.layer is self.doc.layers[active])
        self._sync_controls()
        self._syncing = False

    def _sync_controls(self):
        doc = self.doc
        L = doc.active_layer
        n = len(doc.layers)
        i = doc.active_index
        self.opacity.setEnabled(L is not None)
        self.blend.setEnabled(L is not None)
        if L is not None:
            self.opacity.setValue(int(round(L.opacity * 100)))
            self.blend.setCurrentIndex(max(0, self.blend.findData(L.blend)))
        self.btn_del.setEnabled(n > 1)
        self.btn_up.setEnabled(i < n - 1)
        self.btn_down.setEnabled(i > 0)
        self.btn_merge.setEnabled(i > 0)

    def _on_content(self, _rect):
        self._thumb_timer.start()

    def _refresh_thumbs(self):
        self._thumbs.clear()
        for row in self._rows:
            row.set_thumb(self._thumb_for(row.layer))

    def _on_row_selected(self, vis_row):
        if self._syncing or self.doc is None:
            return
        n = len(self.doc.layers)
        if 0 <= vis_row < n:
            self.doc.set_active(n - 1 - vis_row)

    def _on_rows_moved(self, *_):
        QTimer.singleShot(0, self._apply_drag_order)

    def _apply_drag_order(self):
        if self.doc is None:
            return
        n = len(self.doc.layers)
        order_idx = []
        for r in range(self.list.count()):
            v = self.list.item(r).data(Qt.UserRole)
            if v is not None:
                order_idx.append(int(v))
        if len(order_idx) == n and sorted(order_idx) == list(range(n)):
            ordered = [self.doc.layers[i] for i in reversed(order_idx)]
            self.doc.reorder(ordered)
        self._rebuild()

    def _toggle_visible(self, layer):
        self.doc.set_layer_prop(layer, "visible", not layer.visible, "Visibilidad de capa")

    def _toggle_lock(self, layer):
        self.doc.set_layer_prop(layer, "locked", not layer.locked, "Bloqueo de capa")

    def _rename(self, layer):
        name, ok = QInputDialog.getText(self, "Renombrar capa", "Nuevo nombre:",
                                        text=layer.name)
        if ok and name.strip():
            self.doc.set_layer_prop(layer, "name", name.strip(), "Renombrar capa")

    def _on_opacity(self, value):
        if self._syncing or self.doc is None or self.doc.active_layer is None:
            return
        self.doc.set_layer_prop(self.doc.active_layer, "opacity",
                                value / 100.0, "Opacidad de capa")

    def _on_blend(self, _):
        if self._syncing or self.doc is None or self.doc.active_layer is None:
            return
        self.doc.set_layer_prop(self.doc.active_layer, "blend",
                                self.blend.currentData(), "Modo de fusión")

    def _merge(self):
        if self.doc is None:
            return
        ok, why = self.doc.merge_down()
        if not ok and why:
            QMessageBox.information(self, "Combinar", why)


# ============================================================
#  BARRA DE TÍTULO Y BORDES
# ============================================================
class TitleBar(QWidget):
    def __init__(self, window: QMainWindow):
        super().__init__(window)
        self._win = window
        self._drag_pos = None
        self.setObjectName("TitleBar")
        self.setFixedHeight(36)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 0, 0); lay.setSpacing(8)
        logo = QLabel(); logo.setPixmap(make_logo(20)); logo.setFixedSize(20, 20)
        lay.addWidget(logo)
        self.menu_bar = QMenuBar()
        self.menu_bar.setNativeMenuBar(False)
        lay.addWidget(self.menu_bar)
        lay.addStretch()
        self.title = QLabel(APP_NAME); self.title.setObjectName("AppTitle")
        lay.addWidget(self.title)
        lay.addStretch()
        self.btn_min = self._wbtn("min", window.showMinimized)
        self.btn_max = self._wbtn("max", self._toggle_max)
        self.btn_close = self._wbtn("close", window.close, close=True)
        for b in (self.btn_min, self.btn_max, self.btn_close):
            lay.addWidget(b)

    def _wbtn(self, icon, slot, close=False):
        b = QToolButton()
        b.setObjectName("CloseBtn" if close else "WinBtn")
        b.setIcon(make_icon(icon, color="#d6d6e2"))
        b.setIconSize(QSize(18, 18)); b.setFixedSize(46, 36)
        b.clicked.connect(slot)
        return b

    def set_title(self, text):
        self.title.setText(text)

    def update_state(self, maximized: bool):
        self.btn_max.setIcon(make_icon("restore" if maximized else "max",
                                       color="#d6d6e2"))

    def _toggle_max(self):
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self._win.isMaximized():
            handle = self._win.windowHandle()
            if handle is not None and handle.startSystemMove():
                return
            self._drag_pos = e.globalPosition().toPoint()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            delta = e.globalPosition().toPoint() - self._drag_pos
            self._win.move(self._win.pos() + delta)
            self._drag_pos = e.globalPosition().toPoint()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._toggle_max()


class ResizeGrip(QWidget):
    def __init__(self, window, edges, cursor):
        super().__init__(window)
        self._win = window
        self._edges = edges
        self.setCursor(cursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            h = self._win.windowHandle()
            if h is not None:
                h.startSystemResize(self._edges)


class StatusBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StatusBar")
        self.setFixedHeight(26)
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 0, 12, 0); h.setSpacing(18)
        self.msg = QLabel("Listo")
        self.cursor = QLabel("—")
        self.layer = QLabel("")
        self.size = QLabel("")
        self.zoom = QLabel("100 %")
        h.addWidget(self.msg, 1)
        for w in (self.cursor, self.layer, self.size, self.zoom):
            w.setMinimumWidth(70)
            h.addWidget(w)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.msg.setText("Listo"))

    def show_message(self, text, ms=3500):
        self.msg.setText(text)
        self._timer.start(ms)

    def set_cursor(self, x, y):
        self.cursor.setText("—" if x < 0 else f"{x}, {y} px")

    def set_zoom(self, z):
        self.zoom.setText(f"{z * 100:.0f} %" if z >= 0.1 else f"{z * 100:.1f} %")


# ============================================================
#  DIÁLOGO NUEVO DOCUMENTO
# ============================================================
class NewDocDialog(QDialog):
    PRESETS = [("256 × 256", 256, 256), ("512 × 512", 512, 512),
               ("1024 × 1024", 1024, 1024), ("2048 × 2048", 2048, 2048),
               ("4096 × 4096", 4096, 4096), ("Personalizado", 0, 0)]

    def __init__(self, parent=None, default=(1024, 1024)):
        super().__init__(parent)
        self.setWindowTitle("Nuevo documento")
        self.setModal(True)
        form = QFormLayout(self)
        form.setContentsMargins(18, 18, 18, 14); form.setSpacing(10)
        self.preset = QComboBox()
        for label, *_ in self.PRESETS:
            self.preset.addItem(label)
        self.w = QSpinBox(); self.w.setRange(1, MAX_DOC_SIZE); self.w.setSuffix(" px")
        self.h = QSpinBox(); self.h.setRange(1, MAX_DOC_SIZE); self.h.setSuffix(" px")
        self.w.setValue(default[0]); self.h.setValue(default[1])
        self.bg = QComboBox(); self.bg.addItems(["Blanco", "Transparente"])
        form.addRow("Preajuste", self.preset)
        form.addRow("Ancho", self.w)
        form.addRow("Alto", self.h)
        form.addRow("Fondo", self.bg)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Crear")
        bb.button(QDialogButtonBox.Cancel).setText("Cancelar")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.preset.setCurrentIndex(2)
        self.preset.currentIndexChanged.connect(self._on_preset)
        self.w.valueChanged.connect(self._to_custom)
        self.h.valueChanged.connect(self._to_custom)
        self.setMinimumWidth(320)

    def _on_preset(self, i):
        _, w, h = self.PRESETS[i]
        if w:
            for sp, v in ((self.w, w), (self.h, h)):
                sp.blockSignals(True); sp.setValue(v); sp.blockSignals(False)

    def _to_custom(self):
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(len(self.PRESETS) - 1)
        for i, (_, w, h) in enumerate(self.PRESETS[:-1]):
            if w == self.w.value() and h == self.h.value():
                self.preset.setCurrentIndex(i)
        self.preset.blockSignals(False)

    def values(self):
        return self.w.value(), self.h.value(), self.bg.currentIndex() == 0


# ============================================================
#  VENTANA PRINCIPAL
# ============================================================
TOOLS = [
    ("brush", "Pincel", "B"), ("eraser", "Borrador", "E"),
    ("fill", "Relleno", "G"), ("picker", "Cuentagotas", "I"),
    ("line", "Línea", "L"), ("rect", "Rectángulo", "R"),
    ("ellipse", "Elipse", "O"), ("hand", "Mano", "H"),
]


class MainWindow(QMainWindow):
    GRIP = 5

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(make_logo(32)))
        self.resize(1360, 860)
        self.setMinimumSize(1020, 640)

        self.settings = ToolSettings()
        self.doc = None

        root = QWidget(); root.setObjectName("Root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        self.title_bar = TitleBar(self)
        v.addWidget(self.title_bar)

        body = QHBoxLayout(); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(0)
        v.addLayout(body, 1)
        self.status = StatusBar()
        v.addWidget(self.status)

        self.canvas = Canvas(self.settings)
        self.canvas.zoomChanged.connect(self.status.set_zoom)
        self.canvas.cursorMoved.connect(self.status.set_cursor)
        self.canvas.message.connect(self.status.show_message)
        self.canvas.colorPicked.connect(lambda c: self.color_panel.set_color(c))
        self.canvas.colorUsed.connect(lambda c: self.color_panel.add_recent(c))

        self._create_actions()
        body.addWidget(self._build_toolbar())
        body.addWidget(self._build_left_panel())
        body.addWidget(self.canvas, 1)
        body.addWidget(self._build_right_panel())
        self._build_menus()
        self._build_grips()

        self.addActions(self._all_actions)
        self.new_document(1024, 1024, True, confirm=False)
        self._update_tool_options()

    # ---------- Acciones ----------
    def _act(self, text, slot=None, shortcut=None, checkable=False,
             checked=False, icon=None, tip=None):
        a = QAction(text, self)
        if shortcut:
            if isinstance(shortcut, (list, tuple)):
                a.setShortcuts([QKeySequence(s) for s in shortcut])
            else:
                a.setShortcut(QKeySequence(shortcut))
        if icon:
            a.setIcon(make_icon(icon))
        a.setCheckable(checkable)
        if checkable:
            a.setChecked(checked)
            if slot:
                a.toggled.connect(slot)
        elif slot:
            a.triggered.connect(lambda *_: slot())
        if tip:
            a.setToolTip(tip)
        self._all_actions.append(a)
        return a

    def _create_actions(self):
        self._all_actions = []
        self.a_new = self._act("Nuevo…", self._cmd_new, "Ctrl+N")
        self.a_open = self._act("Abrir…", self._cmd_open, "Ctrl+O")
        self.a_save = self._act("Guardar proyecto", self._cmd_save, "Ctrl+S")
        self.a_save_as = self._act("Guardar como…", self._cmd_save_as, "Ctrl+Shift+S")
        self.a_export = self._act("Exportar imagen…", self._cmd_export, "Ctrl+E")
        self.a_quit = self._act("Salir", self.close, "Ctrl+Q")
        self.a_undo = self._act("Deshacer", self._cmd_undo, "Ctrl+Z")
        self.a_redo = self._act("Rehacer", self._cmd_redo, ["Ctrl+Shift+Z", "Ctrl+Y"])
        self.a_copy = self._act("Copiar imagen visible", self._cmd_copy, "Ctrl+Shift+C")
        self.a_paste = self._act("Pegar como capa nueva", self._cmd_paste, "Ctrl+V")
        self.a_clear = self._act("Limpiar capa",
                                 lambda: self._layer_op(lambda d: d.clear_active()),
                                 "Delete")
        self.a_fill_layer = self._act("Rellenar capa con el color",
                                      self._cmd_fill_layer, "Ctrl+Backspace")
        self.a_layer_new = self._act("Nueva capa",
                                     lambda: self.doc.add_layer(), "Ctrl+Shift+N")
        self.a_layer_dup = self._act("Duplicar capa",
                                     lambda: self.doc.duplicate_active(), "Ctrl+J")
        self.a_layer_del = self._act("Eliminar capa",
                                     lambda: self.doc.remove_active())
        self.a_layer_merge = self._act("Combinar hacia abajo", self._cmd_merge, "Ctrl+M")
        self.a_layer_up = self._act("Subir capa",
                                    lambda: self.doc.move_active(+1), "Ctrl+]")
        self.a_layer_down = self._act("Bajar capa",
                                      lambda: self.doc.move_active(-1), "Ctrl+[")
        self.a_flip_h = self._act("Voltear horizontal",
                                  lambda: self._filter("Voltear horizontal",
                                                       lambda im: im.mirrored(True, False)))
        self.a_flip_v = self._act("Voltear vertical",
                                  lambda: self._filter("Voltear vertical",
                                                       lambda im: im.mirrored(False, True)))
        self.a_invert = self._act("Invertir colores",
                                  lambda: self._filter("Invertir colores", invert_image),
                                  "Ctrl+I")
        self.a_gray = self._act("Escala de grises",
                                lambda: self._filter("Escala de grises", grayscale_image))
        self.a_offset = self._act(
            "Desplazar la mitad (comprobar costuras)",
            lambda: self._filter(
                "Desplazar mitad",
                lambda im: offset_image(im, im.width() // 2, im.height() // 2)),
            "Ctrl+Shift+O")
        self.a_zoom_in = self._act("Acercar", self.canvas.zoom_in,
                                   [QKeySequence.ZoomIn, "Ctrl+="])
        self.a_zoom_out = self._act("Alejar", self.canvas.zoom_out, QKeySequence.ZoomOut)
        self.a_fit = self._act("Ajustar a la ventana", self.canvas.fit_view, "Ctrl+0")
        self.a_actual = self._act("Tamaño real (100 %)", self.canvas.zoom_actual, "Ctrl+1")
        self.a_tile_preview = self._act("Vista en mosaico 3×3",
                                        self.canvas.set_tile_preview, "T",
                                        checkable=True)
        self.a_wrap_x = self._act("Dibujo continuo en X",
                                  lambda on: setattr(self.settings, "tile_x", on),
                                  checkable=True)
        self.a_wrap_y = self._act("Dibujo continuo en Y",
                                  lambda on: setattr(self.settings, "tile_y", on),
                                  checkable=True)
        self.a_grid = self._act("Mostrar rejilla", self.canvas.set_show_grid,
                                "Ctrl+'", checkable=True)
        self.tool_group = QActionGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_actions = {}
        for key, name, sc in TOOLS:
            a = self._act(name, None, sc, checkable=True, checked=(key == "brush"),
                          icon=key, tip=f"{name} ({sc})")
            a.setData(key)
            a.toggled.connect(lambda on, k=key: on and self._set_tool(k))
            self.tool_group.addAction(a)
            self.tool_actions[key] = a
        self.a_size_down = self._act("Reducir tamaño", lambda: self._bump_size(-1), "[")
        self.a_size_up = self._act("Aumentar tamaño", lambda: self._bump_size(+1), "]")
        self.a_shortcuts = self._act("Atajos de teclado", self._show_shortcuts, "F1")
        self.a_about = self._act("Acerca de", self._show_about)

    # ---------- Interfaz ----------
    def _build_menus(self):
        mb = self.title_bar.menu_bar
        m = mb.addMenu("Archivo")
        for a in (self.a_new, self.a_open, None, self.a_save, self.a_save_as,
                  self.a_export, None, self.a_quit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("Edición")
        for a in (self.a_undo, self.a_redo, None, self.a_copy, self.a_paste, None,
                  self.a_clear, self.a_fill_layer):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("Capa")
        for a in (self.a_layer_new, self.a_layer_dup, self.a_layer_del,
                  self.a_layer_merge, None, self.a_layer_up, self.a_layer_down, None,
                  self.a_flip_h, self.a_flip_v, self.a_invert, self.a_gray,
                  None, self.a_offset):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("Ver")
        for a in (self.a_zoom_in, self.a_zoom_out, self.a_fit, self.a_actual, None,
                  self.a_tile_preview, self.a_wrap_x, self.a_wrap_y, self.a_grid):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("Ayuda")
        m.addAction(self.a_shortcuts); m.addAction(self.a_about)

    def _build_toolbar(self):
        bar = QWidget(); bar.setObjectName("ToolBar"); bar.setFixedWidth(54)
        v = QVBoxLayout(bar)
        v.setContentsMargins(7, 10, 7, 10); v.setSpacing(4)
        for key, _, _ in TOOLS:
            b = QToolButton()
            b.setDefaultAction(self.tool_actions[key])
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            b.setIconSize(QSize(22, 22)); b.setFixedSize(40, 40)
            v.addWidget(b)
        v.addStretch()
        self.color_chip = ColorChip(self.settings.color)
        v.addWidget(self.color_chip, 0, Qt.AlignHCenter)
        return bar

    def _toggle_btn(self, action):
        b = QToolButton()
        b.setObjectName("ToggleBtn")
        b.setDefaultAction(action)
        b.setToolButtonStyle(Qt.ToolButtonTextOnly)
        b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return b

    def _build_left_panel(self):
        scroll = QScrollArea()
        scroll.setObjectName("Sidebar")
        scroll.setFixedWidth(272)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget(); inner.setObjectName("Sidebar")
        side = QVBoxLayout(inner)
        side.setContentsMargins(10, 10, 6, 10); side.setSpacing(8)

        sec = CollapsibleSection("Color", True)
        self.color_panel = ColorPanel(self.settings.color)
        self.color_panel.colorChanged.connect(self._on_color_changed)
        sec.content_layout().addWidget(self.color_panel)
        side.addWidget(sec)

        sec = CollapsibleSection("Opciones de herramienta", True)
        cl = sec.content_layout()
        self.opt_size = LabeledSlider("Tamaño", 1, 400, self.settings.size, " px")
        self.opt_size.valueChanged.connect(self._on_size)
        self.opt_opacity = LabeledSlider("Opacidad", 1, 100, 100, " %")
        self.opt_opacity.valueChanged.connect(self._on_opacity)
        self.opt_hardness = LabeledSlider("Dureza", 0, 100, 100, " %")
        self.opt_hardness.valueChanged.connect(
            lambda v: setattr(self.settings, "hardness", v / 100.0))
        self.opt_smooth = LabeledSlider("Suavizado del trazo", 0, 100, 0, " %")
        self.opt_smooth.valueChanged.connect(
            lambda v: setattr(self.settings, "smoothing", v))
        self.opt_tol = LabeledSlider("Tolerancia", 0, 100,
                                     self.settings.tolerance, " %")
        self.opt_tol.valueChanged.connect(
            lambda v: setattr(self.settings, "tolerance", v))

        self.opt_type_label = QLabel("Tipo de trazo"); self.opt_type_label.setObjectName("Muted")
        self.opt_type = QComboBox()
        self.opt_type.addItem("Libre (con dureza y presión)", "free")
        self.opt_type.addItem("Determinista (vectorial)", "deterministic")
        self.opt_type.setCurrentIndex(1)
        self.opt_type.currentIndexChanged.connect(self._on_stroke_type)

        self.opt_press_label = QLabel("Presión"); self.opt_press_label.setObjectName("Muted")
        self.opt_press = QComboBox()
        self.opt_press.addItem("Velocidad", "velocity")
        self.opt_press.addItem("Tableta (presión)", "tablet")
        self.opt_press.addItem("Ninguna", "none")
        self.opt_press.currentIndexChanged.connect(
            lambda i: setattr(self.settings, "pressure_mode",
                              self.opt_press.itemData(i)))

        self.opt_shape_label = QLabel("Forma"); self.opt_shape_label.setObjectName("Muted")
        self.opt_shape = QComboBox()
        self.opt_shape.addItem("Contorno", "outline")
        self.opt_shape.addItem("Relleno", "fill")
        self.opt_shape.currentIndexChanged.connect(
            lambda i: setattr(self.settings, "shape_mode",
                              self.opt_shape.itemData(i)))

        self.opt_none = QLabel("Esta herramienta no tiene opciones.")
        self.opt_none.setObjectName("Muted"); self.opt_none.setWordWrap(True)

        for w in (self.opt_size, self.opt_opacity, self.opt_hardness, self.opt_smooth,
                  self.opt_tol, self.opt_type_label, self.opt_type,
                  self.opt_press_label, self.opt_press, self.opt_shape_label,
                  self.opt_shape, self.opt_none):
            cl.addWidget(w)
        side.addWidget(sec)

        sec = CollapsibleSection("Textura (repetición)", True)
        cl = sec.content_layout()
        row = QHBoxLayout(); row.setSpacing(6)
        row.addWidget(self._toggle_btn(self.a_wrap_x))
        row.addWidget(self._toggle_btn(self.a_wrap_y))
        cl.addLayout(row)
        cl.addWidget(self._toggle_btn(self.a_tile_preview))
        cl.addWidget(self._toggle_btn(self._offset_proxy()))
        grow = QHBoxLayout(); grow.setSpacing(6)
        grow.addWidget(self._toggle_btn(self.a_grid))
        self.grid_spin = QSpinBox(); self.grid_spin.setRange(4, 1024)
        self.grid_spin.setValue(64); self.grid_spin.setSuffix(" px")
        self.grid_spin.setFixedWidth(78)
        self.grid_spin.valueChanged.connect(self._on_grid_size)
        grow.addWidget(self.grid_spin)
        cl.addLayout(grow)
        hint = QLabel("El dibujo continuo envuelve los trazos por los bordes "
                      "para que la textura sea repetible.")
        hint.setObjectName("Muted"); hint.setWordWrap(True)
        cl.addWidget(hint)
        side.addWidget(sec)

        sec = CollapsibleSection("Vista", False)
        cl = sec.content_layout()
        row = QHBoxLayout(); row.setSpacing(6)
        for text, slot in (("−", self.canvas.zoom_out), ("+", self.canvas.zoom_in),
                           ("Ajustar", self.canvas.fit_view),
                           ("100 %", self.canvas.zoom_actual)):
            b = QPushButton(text)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda *_, s=slot: s())
            row.addWidget(b)
        cl.addLayout(row)
        side.addWidget(sec)

        side.addStretch()
        scroll.setWidget(inner)
        return scroll

    def _offset_proxy(self):
        a = QAction("Desplazar mitad (ver costuras)", self)
        a.triggered.connect(lambda *_: self.a_offset.trigger())
        self._all_actions.append(a)
        return a

    def _build_right_panel(self):
        panel = QWidget(); panel.setObjectName("RightPanel"); panel.setFixedWidth(272)
        v = QVBoxLayout(panel)
        v.setContentsMargins(10, 10, 10, 10); v.setSpacing(8)
        t = QLabel("Capas"); t.setObjectName("PanelTitle")
        v.addWidget(t)
        self.layer_panel = LayerPanel()
        v.addWidget(self.layer_panel, 1)
        sec = CollapsibleSection("Historial", False)
        self.history_view = QUndoView()
        self.history_view.setFixedHeight(170)
        sec.content_layout().addWidget(self.history_view)
        v.addWidget(sec)
        return panel

    def _build_grips(self):
        g = self.GRIP
        E = Qt
        spec = [
            (E.LeftEdge, Qt.SizeHorCursor), (E.RightEdge, Qt.SizeHorCursor),
            (E.TopEdge, Qt.SizeVerCursor), (E.BottomEdge, Qt.SizeVerCursor),
            (E.LeftEdge | E.TopEdge, Qt.SizeFDiagCursor),
            (E.RightEdge | E.BottomEdge, Qt.SizeFDiagCursor),
            (E.RightEdge | E.TopEdge, Qt.SizeBDiagCursor),
            (E.LeftEdge | E.BottomEdge, Qt.SizeBDiagCursor),
        ]
        self._grips = []
        for edges, cur in spec:
            grip = ResizeGrip(self, edges, cur)
            self._grips.append((edges, grip))
        self._layout_grips()

    def _layout_grips(self):
        g, c = self.GRIP, 12
        w, h = self.width(), self.height()
        for edges, grip in self._grips:
            left = bool(edges & Qt.LeftEdge); right = bool(edges & Qt.RightEdge)
            top = bool(edges & Qt.TopEdge); bottom = bool(edges & Qt.BottomEdge)
            if (left or right) and (top or bottom):
                x = 0 if left else w - c
                y = 0 if top else h - c
                grip.setGeometry(x, y, c, c)
            elif left or right:
                grip.setGeometry(0 if left else w - g, c, g, h - 2 * c)
            else:
                grip.setGeometry(c, 0 if top else h - g, w - 2 * c, g)
            grip.setVisible(not self.isMaximized())
            grip.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_grips"):
            self._layout_grips()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.WindowStateChange and hasattr(self, "title_bar"):
            self.title_bar.update_state(self.isMaximized())
            if hasattr(self, "_grips"):
                self._layout_grips()

    # ---------- Documento ----------
    def _set_document(self, doc: Document):
        old = self.doc
        if old is not None:
            for sig, slot in ((old.undo_stack.cleanChanged, self._update_title),
                              (old.undo_stack.canUndoChanged, self._sync_undo),
                              (old.undo_stack.canRedoChanged, self._sync_undo),
                              (old.undo_stack.undoTextChanged, self._sync_undo),
                              (old.undo_stack.redoTextChanged, self._sync_undo)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.doc = doc
        self.canvas.set_document(doc)
        self.layer_panel.set_document(doc)
        self.history_view.setStack(doc.undo_stack)
        doc.undo_stack.cleanChanged.connect(self._update_title)
        doc.undo_stack.canUndoChanged.connect(self._sync_undo)
        doc.undo_stack.canRedoChanged.connect(self._sync_undo)
        doc.undo_stack.undoTextChanged.connect(self._sync_undo)
        doc.undo_stack.redoTextChanged.connect(self._sync_undo)
        doc.structure_changed.connect(self._update_status_doc)
        doc.active_changed.connect(self._update_status_doc)
        self._update_title()
        self._sync_undo()
        self._update_status_doc()
        if old is not None:
            old.deleteLater()

    def new_document(self, w, h, white, confirm=True):
        if confirm and self.doc is not None and not self._confirm_discard():
            return
        self._set_document(Document(w, h, white_background=white))

    def _update_title(self, *_):
        d = self.doc
        mark = "" if d.undo_stack.isClean() else "*"
        self.title_bar.set_title(f"{d.name}{mark} — {APP_NAME}")
        self.setWindowTitle(f"{d.name}{mark} — {APP_NAME}")

    def _update_status_doc(self, *_):
        d = self.doc
        self.status.size.setText(f"{d.width_px} × {d.height_px}")
        L = d.active_layer
        self.status.layer.setText(L.name if L else "")

    def _sync_undo(self, *_):
        s = self.doc.undo_stack
        self.a_undo.setEnabled(s.canUndo())
        self.a_redo.setEnabled(s.canRedo())
        self.a_undo.setText("Deshacer" + (f" «{s.undoText()}»" if s.canUndo() else ""))
        self.a_redo.setText("Rehacer" + (f" «{s.redoText()}»" if s.canRedo() else ""))

    # ---------- Comandos ----------
    def _confirm_discard(self) -> bool:
        if self.doc.undo_stack.isClean():
            return True
        r = QMessageBox.question(
            self, "Cambios sin guardar",
            f"¿Guardar los cambios de «{self.doc.name}» antes de continuar?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if r == QMessageBox.Save:
            return self._cmd_save()
        return r == QMessageBox.Discard

    def _cmd_new(self):
        dlg = NewDocDialog(self, (self.doc.width_px, self.doc.height_px))
        if dlg.exec() == QDialog.Accepted:
            w, h, white = dlg.values()
            self.new_document(w, h, white)

    def _cmd_open(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir", self._start_dir(),
            f"Proyectos e imágenes (*{PROJECT_EXT} *.png *.jpg *.jpeg *.bmp *.webp *.tga);;"
            f"Todos los archivos (*)")
        if not path:
            return
        try:
            if path.lower().endswith(PROJECT_EXT):
                doc = load_project(path)
            else:
                img = QImage(path)
                if img.isNull():
                    raise ValueError("No se pudo leer la imagen")
                if img.width() > MAX_DOC_SIZE or img.height() > MAX_DOC_SIZE:
                    raise ValueError(f"La imagen supera el máximo de {MAX_DOC_SIZE} px")
                doc = document_from_image(img, os.path.splitext(os.path.basename(path))[0])
        except Exception as ex:
            QMessageBox.critical(self, "Error al abrir", str(ex))
            return
        self._set_document(doc)
        self.status.show_message(f"Abierto: {os.path.basename(path)}")

    def _start_dir(self):
        if self.doc is not None and self.doc.path:
            return os.path.dirname(self.doc.path)
        return os.path.expanduser("~")

    def _cmd_save(self) -> bool:
        if self.doc.path and self.doc.path.lower().endswith(PROJECT_EXT):
            return self._write_project(self.doc.path)
        return self._cmd_save_as()

    def _cmd_save_as(self) -> bool:
        default = os.path.join(self._start_dir(), self.doc.name + PROJECT_EXT)
        path, _ = QFileDialog.getSaveFileName(
            self, "Guardar proyecto", default,
            f"Proyecto de texturas (*{PROJECT_EXT})")
        if not path:
            return False
        if not path.lower().endswith(PROJECT_EXT):
            path += PROJECT_EXT
        return self._write_project(path)

    def _write_project(self, path) -> bool:
        try:
            save_project(self.doc, path)
        except Exception as ex:
            QMessageBox.critical(self, "Error al guardar", str(ex))
            return False
        self.doc.path = path
        self.doc.name = os.path.splitext(os.path.basename(path))[0]
        self.doc.undo_stack.setClean()
        self._update_title()
        self.status.show_message(f"Guardado: {os.path.basename(path)}")
        return True

    def _cmd_export(self):
        default = os.path.join(self._start_dir(), self.doc.name + ".png")
        path, flt = QFileDialog.getSaveFileName(
            self, "Exportar imagen", default,
            "PNG (*.png);;JPEG (*.jpg *.jpeg);;WebP (*.webp);;BMP (*.bmp)")
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if not ext:
            ext = ".jpg" if "JPEG" in flt else ".webp" if "WebP" in flt \
                else ".bmp" if "BMP" in flt else ".png"
            path += ext
        if ext in (".jpg", ".jpeg", ".bmp"):
            img = self.doc.flatten(QColor(255, 255, 255)) \
                .convertToFormat(QImage.Format_RGB32)
        else:
            img = self.doc.flatten()
        fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
               ".webp": "WEBP", ".bmp": "BMP"}.get(ext, "PNG")
        ok = img.save(path, fmt, 95) if fmt in ("JPEG", "WEBP") else img.save(path, fmt)
        if ok:
            self.status.show_message(f"Exportado: {os.path.basename(path)}")
        else:
            QMessageBox.critical(self, "Error al exportar",
                                 "No se pudo escribir el archivo.")

    def _cmd_undo(self):
        if not self.canvas.is_stroking():
            self.doc.undo_stack.undo()

    def _cmd_redo(self):
        if not self.canvas.is_stroking():
            self.doc.undo_stack.redo()

    def _cmd_copy(self):
        QApplication.clipboard().setImage(self.doc.flatten())
        self.status.show_message("Imagen copiada al portapapeles")

    def _cmd_paste(self):
        img = QApplication.clipboard().image()
        if img.isNull():
            self.status.show_message("No hay ninguna imagen en el portapapeles")
            return
        img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        full = QImage(self.doc.width_px, self.doc.height_px,
                      QImage.Format_ARGB32_Premultiplied)
        full.fill(QColor(0, 0, 0, 0))
        p = QPainter(full)
        p.drawImage((full.width() - img.width()) // 2,
                    (full.height() - img.height()) // 2, img)
        p.end()
        self.doc.add_layer("Pegado", image=full)

    def _cmd_fill_layer(self):
        self._layer_op(lambda d: d.fill_active(self.settings.color))

    def _cmd_merge(self):
        ok, why = self.doc.merge_down()
        if not ok:
            self.status.show_message(why)

    def _layer_op(self, fn):
        ok, why = fn(self.doc)
        if not ok:
            self.status.show_message(why)

    def _filter(self, text, fn):
        if self.canvas.is_stroking():
            return
        self._layer_op(lambda d: d.apply_to_active(text, fn))

    # ---------- Herramientas ----------
    def _set_tool(self, key):
        self.settings.tool = key
        self.canvas.refresh_cursor()
        self._update_tool_options()

    def _update_tool_options(self):
        t = self.settings.tool
        free = self.settings.stroke_type == "free"
        draw = t in ("brush", "eraser")
        shape = t in ("line", "rect", "ellipse")
        self.opt_size.setVisible(draw or shape)
        self.opt_opacity.setVisible(draw or shape or t == "fill")
        self.opt_type_label.setVisible(draw)
        self.opt_type.setVisible(draw)
        self.opt_hardness.setVisible(draw and free)
        self.opt_smooth.setVisible(draw and free)
        self.opt_press_label.setVisible(draw and free)
        self.opt_press.setVisible(draw and free)
        self.opt_shape_label.setVisible(t in ("rect", "ellipse"))
        self.opt_shape.setVisible(t in ("rect", "ellipse"))
        self.opt_tol.setVisible(t == "fill")
        self.opt_none.setVisible(t in ("picker", "hand"))

    def _on_color_changed(self, c: QColor):
        self.settings.color = QColor(c)
        self.color_chip.set_color(c)

    def _on_size(self, v):
        self.settings.size = v
        self.canvas.update()

    def _on_opacity(self, v):
        self.settings.opacity = v / 100.0

    def _on_stroke_type(self, i):
        self.settings.stroke_type = self.opt_type.itemData(i)
        self._update_tool_options()

    def _on_grid_size(self, v):
        self.canvas.grid_size = v
        self.canvas.update()

    def _bump_size(self, direction):
        s = self.settings.size
        step = max(1, s // 8)
        self.opt_size.setValue(clamp(s + direction * step, 1, 400))

    # ---------- Ayuda ----------
    def _show_shortcuts(self):
        rows = [
            ("B / E / G / I", "Pincel / Borrador / Relleno / Cuentagotas"),
            ("L / R / O / H", "Línea / Rectángulo / Elipse / Mano"),
            ("[  ]", "Reducir / aumentar tamaño"),
            ("Espacio + arrastrar", "Mover la vista"),
            ("Rueda del ratón", "Zoom hacia el cursor"),
            ("Alt + clic", "Cuentagotas temporal"),
            ("Mayús (formas)", "Línea a 45° / cuadrado / círculo"),
            ("Esc", "Cancelar el trazo en curso"),
            ("Ctrl+Z / Ctrl+Shift+Z", "Deshacer / Rehacer"),
            ("Ctrl+N / O / S", "Nuevo / Abrir / Guardar"),
            ("Ctrl+E", "Exportar imagen"),
            ("Ctrl+Shift+N / J", "Nueva capa / Duplicar capa"),
            ("Ctrl+M", "Combinar hacia abajo"),
            ("Ctrl+0 / Ctrl+1", "Ajustar / Tamaño real"),
            ("T", "Vista en mosaico 3×3"),
            ("Supr", "Limpiar capa"),
        ]
        html = "<table cellspacing='6'>" + "".join(
            f"<tr><td><b>{k}</b></td><td style='color:{MUTED}'>&nbsp;&nbsp;{v}</td></tr>"
            for k, v in rows) + "</table>"
        QMessageBox.information(self, "Atajos de teclado", html)

    def _show_about(self):
        QMessageBox.about(self, f"Acerca de {APP_NAME}",
                          f"<h3>{APP_NAME}</h3><p>Editor de texturas por capas "
                          f"con dibujo continuo, vista en mosaico y proyectos .texproj.</p>")

    # ---------- Cierre ----------
    def closeEvent(self, e):
        if self.canvas.is_stroking():
            self.canvas.cancel_stroke()
        if self._confirm_discard():
            e.accept()
        else:
            e.ignore()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    apply_dark_palette(app)
    app.setStyleSheet(build_stylesheet())
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()