"""
Frontal Android del Creador de Texturas.
- Toolbar superior (menú, undo/redo, herramientas)
- Canvas a pantalla completa
- Toolbar inferior contextual (color + tamaño / tolerancia / forma)
- Drawers animados: izquierdo (color + opciones), derecho (capas)
- Sin cursores custom, sin atajos de teclado, sin dialogs nativos no-SAF
"""
from __future__ import annotations

import math
import os
import sys

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation,
    QRectF, QRegularExpression, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QIcon, QImage, QPainter, QPen, QPixmap,
    QRegularExpressionValidator, QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QButtonGroup,
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QToolButton, QVBoxLayout, QWidget,
)

from core import (
    APP_NAME, PROJECT_EXT, MAX_DOC_SIZE, IS_ANDROID,
    BG0, BG1, BG2, BG3, BORDER, TEXT, MUTED, ACCENT, CANVAS_BG,
    build_stylesheet, clamp, mid, quad_point, premultiplied_argb,
    image_pixels_u32, flood_fill_u32, offset_image, invert_image,
    grayscale_image, checker_pixmap, make_icon, make_logo,
    BLEND_MODES, Layer, PatchCommand, StackCommand, PropertyCommand,
    Document, save_project, load_project, document_from_image,
    ToolSettings,
)


# ============================================================
#  CANVAS (móvil)
# ============================================================
class MobileCanvas(QWidget):
    """Canvas con gestos multitáctiles limpios y sin cursor custom."""
    zoomChanged = Signal(float)
    message = Signal(str)
    colorPicked = Signal(QColor)
    colorUsed = Signal(QColor)

    DRAW_TOOLS = ("brush", "eraser")
    SHAPE_TOOLS = ("line", "rect", "ellipse")

    def __init__(self, settings: ToolSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.doc = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMinimumSize(200, 200)

        # vista
        self.zoom = 1.0
        self.min_zoom, self.max_zoom = 0.05, 40.0
        self.offset = QPointF(0, 0)
        self._fitted = True
        self.tile_preview = False

        # trazo
        self._stroking = False
        self._layer = None
        self._base = None
        self._buf = None
        self._dirty = QRectF().toRect()
        self._buf_dirty = QRectF().toRect()
        self._pending = {}
        self._pts = []
        self._sp = QPointF()
        self._speed = 0.0
        self._last_screen = QPointF()
        self._remain = 0.0
        self._path = None
        self._eraser = False
        self._opacity = 1.0
        self._pen_color = QColor(0, 0, 0)
        self._pressure = 1.0

        # gestos
        self._touch_points = {}
        self._gesture_active = False
        self._pinch_start = None

        # pan (1 dedo con tool=hand)
        self._panning = False
        self._pan_start = QPointF()
        self._pan_off0 = QPointF()

        # cuentagotas con long-press
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(500)
        self._long_press_timer.timeout.connect(self._on_long_press)
        self._long_press_pos = None
        self._picking = False

    # ---------- Documento ----------
    def set_document(self, doc: Document):
        self.doc = doc
        self._buf = QImage(doc.width_px, doc.height_px, QImage.Format_ARGB32_Premultiplied)
        self._buf.fill(QColor(0, 0, 0, 0))
        doc.content_changed.connect(self._on_content)
        doc.structure_changed.connect(lambda: self.update())
        self._fitted = True
        self.fit_view()

    def _on_content(self, rect):
        if self.tile_preview:
            # Repintar solo las 9 copias intersectadas con `rect`
            w, h, z = self.doc.width_px, self.doc.height_px, self.zoom
            for j in (-1, 0, 1):
                for i in (-1, 0, 1):
                    self.update(self._image_rect_to_widget(rect)
                                .translated(int(i * w * z), int(j * h * z)))
        else:
            self.update(self._image_rect_to_widget(rect))

    def _image_rect_to_widget(self, r):
        x = self.offset.x() + r.x() * self.zoom
        y = self.offset.y() + r.y() * self.zoom
        return QRectF(x, y, r.width() * self.zoom, r.height() * self.zoom) \
            .toAlignedRect().adjusted(-2, -2, 2, 2)

    # ---------- Vista ----------
    def screen_to_image(self, pos: QPointF) -> QPointF:
        return QPointF((pos.x() - self.offset.x()) / self.zoom,
                       (pos.y() - self.offset.y()) / self.zoom)

    def set_zoom(self, new_zoom, anchor=None):
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

    def zoom_in(self): self.set_zoom(self.zoom * 1.3)
    def zoom_out(self): self.set_zoom(self.zoom / 1.3)
    def zoom_actual(self): self.set_zoom(1.0)

    def fit_view(self, margin=24):
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

    def set_tile_preview(self, on):
        self.tile_preview = on
        self.fit_view()
        self.update()

    def resizeEvent(self, e):
        if self._fitted:
            self.fit_view()
        super().resizeEvent(e)

    # ---------- Pintado ----------
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

        checker = QBrush = None
        from PySide6.QtGui import QBrush as QB
        checker = QB(checker_pixmap())
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

        p.setRenderHint(QPainter.Antialiasing, False)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(ACCENT) if self.tile_preview else QColor(90, 90, 105), 1))
        p.drawRect(center)

    # ---------- Eventos ----------
    def event(self, e):
        t = e.type()
        if t in (QEvent.TouchBegin, QEvent.TouchUpdate,
                 QEvent.TouchEnd, QEvent.TouchCancel):
            return self._handle_touch(e)
        return super().event(e)

    def _handle_touch(self, e) -> bool:
        for tp in e.points():
            pid = tp.id()
            if e.type() in (QEvent.TouchEnd, QEvent.TouchCancel):
                self._touch_points.pop(pid, None)
            else:
                self._touch_points[pid] = QPointF(tp.position())
        n = len(self._touch_points)
        if n >= 2:
            # Gestión de gesto: cancelar trazo si lo había
            if self._stroking:
                self.cancel_stroke()
            self._long_press_timer.stop()
            self._picking = False
            self._panning = False
            self._gesture_active = True
            self._update_pinch()
            e.accept()
            return True
        if n == 1 and not self._gesture_active:
            # 1 dedo → emulación ratón para dibujar
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
        center = (pts[0] + pts[1]) / 2.0
        dist = math.hypot(pts[1].x() - pts[0].x(), pts[1].y() - pts[0].y())
        if self._pinch_start is None:
            self._pinch_start = (dist, self.zoom, center, QPointF(self.offset))
            return
        d0, z0, c0, o0 = self._pinch_start
        new_zoom = clamp(z0 * (dist / d0), self.min_zoom, self.max_zoom) if d0 > 5 else z0
        self.offset = o0 + (center - c0)
        self.set_zoom(new_zoom, anchor=center)

    def tabletEvent(self, e):
        if e.type() in (QEvent.TabletPress, QEvent.TabletMove):
            self._pressure = float(e.pressure())
        else:
            self._pressure = 1.0
        e.ignore()

    def mousePressEvent(self, e):
        if self._gesture_active or self.doc is None:
            return
        self.setFocus()
        pos = e.position()

        # Modo mano o herramienta "hand"
        if self.settings.tool == "hand" or self.settings.hand_mode:
            self._panning = True
            self._pan_start = pos
            self._pan_off0 = QPointF(self.offset)
            return

        img_pos = self.screen_to_image(pos)

        # Long-press → cuentagotas temporal
        if self.settings.tool in self.DRAW_TOOLS:
            self._long_press_pos = img_pos
            self._long_press_timer.start()

        # Cuentagotas / Alt (no hay Alt en móvil, pero conservamos)
        if self.settings.tool == "picker":
            self._do_pick(img_pos)
            return
        if self.settings.tool == "fill":
            self._do_fill(img_pos)
            return
        if self.settings.tool in self.DRAW_TOOLS + self.SHAPE_TOOLS:
            self._begin_stroke(img_pos)

    def mouseMoveEvent(self, e):
        if self._gesture_active or self.doc is None:
            return
        if self._panning:
            self.offset = self._pan_off0 + (e.position() - self._pan_start)
            self._fitted = False
            self.update()
            return
        # Cancelar long-press si el dedo se movió mucho
        if self._long_press_timer.isActive() and self._long_press_pos is not None:
            cur = self.screen_to_image(e.position())
            d = (cur - self._long_press_pos).manhattanLength()
            if d * self.zoom > 12:
                self._long_press_timer.stop()
        if self._stroking:
            self._stroke_move(e)
        elif self._picking:
            self._do_pick(self.screen_to_image(e.position()))

    def mouseReleaseEvent(self, e):
        if self._panning:
            self._panning = False
            return
        if self._long_press_timer.isActive():
            self._long_press_timer.stop()
        if self._picking:
            self._picking = False
            return
        if self._stroking:
            self._finish_stroke()

    def _on_long_press(self):
        if self._long_press_pos is not None and self._stroking:
            self.cancel_stroke()
            self._do_pick(self._long_press_pos)
            self._long_press_pos = None

    # ---------- Cuentagotas / relleno ----------
    def _do_pick(self, pos):
        if self.doc is None:
            return
        x, y = int(math.floor(pos.x())), int(math.floor(pos.y()))
        if not (0 <= x < self.doc.width_px and 0 <= y < self.doc.height_px):
            return
        c = self.doc.flat.pixelColor(x, y)
        if c.alpha() == 0:
            return
        c.setAlpha(255)
        self.colorPicked.emit(c)

    def _do_fill(self, pos):
        ok, why = self.doc.can_paint()
        if not ok:
            self.message.emit(why); return
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
        rect = QRectF(x0, y0, x1 - x0 + 1, y1 - y0 + 1).toRect()
        self.doc.undo_stack.push(PatchCommand(
            self.doc, layer, rect, before_full.copy(rect),
            layer.image.copy(rect), "Relleno"))
        self.doc.notify_content(rect)
        self.colorUsed.emit(self.settings.color)

    # ---------- Trazo ----------
    def _wrap_offsets(self, bounds):
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

    def _add_pending(self, key, rect_f):
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
        self._buf_dirty = QRectF().toRect()

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

    def _begin_stroke(self, pos):
        ok, why = self.doc.can_paint()
        if not ok:
            self.message.emit(why); return
        s = self.settings
        self._layer = self.doc.active_layer
        self._base = self._layer.image.copy()
        self._dirty = QRectF().toRect()
        self._buf_dirty = QRectF().toRect()
        self._pending = {}
        self._stroking = True
        self._eraser = s.tool == "eraser"
        self._opacity = s.opacity * (1.0 if self._eraser else s.color.alphaF())
        self._pen_color = (QColor(0, 0, 0) if self._eraser
                           else QColor(s.color.red(), s.color.green(), s.color.blue()))
        self._pts = [QPointF(pos)]
        self._sp = QPointF(pos)
        self._speed = 0.0
        self._last_screen = pos

        if s.tool in self.DRAW_TOOLS:
            if s.stroke_type == "free":
                self._prepare_dab()
                self._remain = self._spacing()
                p = QPainter(self._buf)
                p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
                self._stamp(p, pos, self._dab_diameter())
                p.end()
            else:
                from PySide6.QtGui import QPainterPath as QPP
                self._path = QPP()
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

    # ---------- Pincel libre ----------
    def _spacing(self):
        return max(0.7, self.settings.size * 0.08)

    def _dab_diameter(self):
        s = self.settings
        size = float(s.size)
        if s.pressure_mode == "velocity":
            v = min(1.0, self._speed / 40.0)
            return max(0.5, size * (1.0 - 0.55 * v))
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
        if getattr(self, "_dab_key", None) == key and getattr(self, "_dab", None) is not None:
            return
        S = size + 2
        from PySide6.QtGui import QImage as QI, QRadialGradient, QBrush
        dab = QI(S, S, QI.Format_ARGB32_Premultiplied)
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
            g.setColorAt(0.0, c0); g.setColorAt(hard, c0); g.setColorAt(1.0, c1)
            p.setBrush(QBrush(g))
        p.drawEllipse(circle)
        p.end()
        self._dab, self._dab_key = dab, key

    def _stamp(self, p, c, diameter):
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

    def _add_free_point(self, pt):
        pts = self._pts
        pts.append(pt)
        n = len(pts)
        if n == 2:
            poly = [pts[0], mid(pts[0], pts[1])]
        else:
            a = mid(pts[-3], pts[-2])
            c = pts[-2]
            b = mid(pts[-2], pts[-1])
            length = math.hypot(c.x() - a.x(), c.y() - a.y()) + math.hypot(b.x() - c.x(), b.y() - c.y())
            steps = max(2, min(48, int(length / 2.0) + 1))
            poly = [quad_point(a, c, b, i / steps) for i in range(steps + 1)]
        self._stamp_polyline(poly)

    # ---------- Determinista ----------
    def _add_det_point(self, pt):
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
            from PySide6.QtGui import QPainterPath as QPP
            path = QPP(self._path)
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
#  COLOR WIDGETS
# ============================================================
class Swatch(QWidget):
    clicked = Signal(QColor)

    def __init__(self, color, size=32, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._color.alpha() < 255:
            p.setBrush(checker_pixmap(6))
            p.setPen(Qt.NoPen); p.drawRoundedRect(r, 6, 6)
        p.setBrush(self._color)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawRoundedRect(r, 6, 6)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(QColor(self._color))


class ColorChip(QWidget):
    def __init__(self, color, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(44, 44)

    def set_color(self, c):
        self._color = QColor(c); self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(checker_pixmap(6))
        p.drawRoundedRect(r, 10, 10)
        p.setBrush(self._color)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1.5))
        p.drawRoundedRect(r, 10, 10)

    def mousePressEvent(self, e):
        self.clicked.emit() if hasattr(self, "clicked") else None


class ColorPanelMobile(QWidget):
    """Panel de color para drawer. Reutiliza ChromaRect estilizado para móvil."""
    colorChanged = Signal(QColor)

    def __init__(self, color, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(12)

        self.chroma = ChromaRectMobile(color)
        self.chroma.setFixedHeight(220)
        self.chroma.colorChanged.connect(self._on_chroma)
        v.addWidget(self.chroma)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8); grid.setVerticalSpacing(6)
        self.spins = {}
        for i, key in enumerate("RGBA"):
            l = QLabel(key); l.setObjectName("Muted")
            sp = QSpinBox(); sp.setRange(0, 255)
            sp.setButtonSymbols(QAbstractSpinBox.NoButtons)
            sp.valueChanged.connect(self._on_spin)
            self.spins[key] = sp
            grid.addWidget(l, i // 2, (i % 2) * 2)
            grid.addWidget(sp, i // 2, (i % 2) * 2 + 1)
        v.addLayout(grid)

        self._sync(color)

    def color(self):
        return self.chroma.color()

    def set_color(self, c, emit=True):
        self.chroma.set_color(c)
        self._sync(c)
        if emit:
            self.colorChanged.emit(QColor(c))

    def _sync(self, c):
        for k, val in zip("RGBA", (c.red(), c.green(), c.blue(), c.alpha())):
            self.spins[k].blockSignals(True)
            self.spins[k].setValue(val)
            self.spins[k].blockSignals(False)

    def _on_chroma(self, c):
        self._sync(c); self.colorChanged.emit(QColor(c))

    def _on_spin(self):
        c = QColor(self.spins["R"].value(), self.spins["G"].value(),
                   self.spins["B"].value(), self.spins["A"].value())
        self.chroma.set_color(c)
        self.colorChanged.emit(c)


class ChromaRectMobile(QWidget):
    """Versión simplificada y táctil: cuadrado S/V grande + hue + alpha."""
    colorChanged = Signal(QColor)

    def __init__(self, color=QColor(255, 0, 0), parent=None):
        super().__init__(parent)
        self._h = 0.0; self._s = 1.0; self._v = 1.0; self._a = 255
        self._sq = QRectF(); self._hue = QRectF(); self._alpha = QRectF()
        self._pix = None
        self._drag = None
        self.setMinimumSize(180, 200)
        self.set_color(color)

    def color(self):
        return QColor.fromHsvF(self._h, self._s, self._v, self._a / 255.0)

    def set_color(self, c):
        h, s, v, a = c.getHsvF()
        if h >= 0:
            self._h = min(h, 0.9999)
        self._s, self._v, self._a = s, v, int(round(a * 255))
        self._rebuild()
        self.update()

    def _layout(self):
        m, bar, gap = 4.0, 22.0, 14.0
        w = self.width() - 2 * m
        h = self.height() - 2 * m - 2 * (bar + gap)
        self._sq = QRectF(m, m, max(10, w), max(10, h))
        self._hue = QRectF(m, self._sq.bottom() + gap, w, bar)
        self._alpha = QRectF(m, self._hue.bottom() + gap, w, bar)

    def resizeEvent(self, e):
        self._layout(); self._rebuild()
        super().resizeEvent(e)

    def _rebuild(self):
        w, h = max(1, int(self._sq.width())), max(1, int(self._sq.height()))
        pm = QPixmap(w, h)
        p = QPainter(pm)
        from PySide6.QtGui import QLinearGradient, QBrush
        gh = QLinearGradient(0, 0, w, 0)
        gh.setColorAt(0, QColor(255, 255, 255))
        gh.setColorAt(1, QColor.fromHsvF(self._h, 1, 1))
        p.fillRect(pm.rect(), QBrush(gh))
        gv = QLinearGradient(0, 0, 0, h)
        gv.setColorAt(0, QColor(0, 0, 0, 0))
        gv.setColorAt(1, QColor(0, 0, 0, 255))
        p.fillRect(pm.rect(), QBrush(gv))
        p.end()
        self._pix = pm

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._pix:
            p.drawPixmap(self._sq.toRect(), self._pix)
        p.setPen(QPen(QColor(255, 255, 255, 35), 1))
        p.setBrush(Qt.NoBrush); p.drawRoundedRect(self._sq, 6, 6)
        x = self._sq.x() + self._s * self._sq.width()
        y = self._sq.y() + (1 - self._v) * self._sq.height()
        p.setPen(QPen(QColor(0, 0, 0, 200), 3)); p.drawEllipse(QPointF(x, y), 9, 9)
        p.setPen(QPen(QColor(255, 255, 255), 2)); p.drawEllipse(QPointF(x, y), 9, 9)

        from PySide6.QtGui import QLinearGradient, QBrush
        g = QLinearGradient(self._hue.topLeft(), self._hue.topRight())
        for i in range(7):
            g.setColorAt(i / 6.0, QColor.fromHsvF(min(i / 6.0, 0.9999), 1, 1))
        p.setPen(Qt.NoPen); p.setBrush(QBrush(g))
        p.drawRoundedRect(self._hue, 6, 6)
        self._handle(p, self._hue, self._h, QColor.fromHsvF(self._h, 1, 1))

        p.setBrush(checker_pixmap(6, "#3a3a42", "#55555f"))
        p.setBrushOrigin(self._alpha.topLeft())
        p.drawRoundedRect(self._alpha, 6, 6)
        base = QColor.fromHsvF(self._h, self._s, self._v)
        ga = QLinearGradient(self._alpha.topLeft(), self._alpha.topRight())
        c0 = QColor(base); c0.setAlpha(0)
        ga.setColorAt(0, c0); ga.setColorAt(1, QColor(base))
        p.setBrush(QBrush(ga)); p.drawRoundedRect(self._alpha, 6, 6)
        self._handle(p, self._alpha, self._a / 255.0, QColor(base))

    @staticmethod
    def _handle(p, bar, t, fill):
        x = bar.x() + t * bar.width()
        r = bar.height() / 2.0 + 3
        p.setPen(QPen(QColor(0, 0, 0, 160), 3)); p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(x, bar.center().y()), r, r)
        p.setPen(QPen(QColor(255, 255, 255), 2)); p.setBrush(fill)
        p.drawEllipse(QPointF(x, bar.center().y()), r, r)

    def _update(self, pos):
        if self._drag == "square":
            r = self._sq
            self._s = clamp((pos.x() - r.x()) / r.width(), 0, 1)
            self._v = 1 - clamp((pos.y() - r.y()) / r.height(), 0, 1)
        elif self._drag == "hue":
            r = self._hue
            self._h = clamp((pos.x() - r.x()) / r.width(), 0, 0.9999)
            self._rebuild()
        elif self._drag == "alpha":
            r = self._alpha
            self._a = int(round(clamp((pos.x() - r.x()) / r.width(), 0, 1) * 255))
        self.update(); self.colorChanged.emit(self.color())

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        if self._hue.adjusted(-6, -8, 6, 8).contains(pos):
            self._drag = "hue"
        elif self._alpha.adjusted(-6, -8, 6, 8).contains(pos):
            self._drag = "alpha"
        elif self._sq.adjusted(-6, -6, 6, 6).contains(pos):
            self._drag = "square"
        else:
            return
        self._update(pos)

    def mouseMoveEvent(self, e):
        if self._drag:
            self._update(e.position())

    def mouseReleaseEvent(self, e):
        self._drag = None


# ============================================================
#  PANEL DE CAPAS (móvil)
# ============================================================
class LayerRowMobile(QWidget):
    visibility_clicked = Signal(object)
    lock_clicked = Signal(object)
    rename_requested = Signal(object)

    def __init__(self, layer: Layer, active: bool, parent=None):
        super().__init__(parent)
        self.layer = layer
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("LayerRow")
        self.setProperty("active", "true" if active else "false")
        self.setFixedHeight(64)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 10, 6); h.setSpacing(6)

        self.btn_vis = QToolButton()
        self.btn_vis.setIcon(make_icon("eye" if layer.visible else "eye_off", size=24))
        self.btn_vis.setIconSize(QSize(24, 24)); self.btn_vis.setFixedSize(44, 44)
        self.btn_vis.clicked.connect(lambda: self.visibility_clicked.emit(self.layer))
        h.addWidget(self.btn_vis)

        self.btn_lock = QToolButton()
        self.btn_lock.setIcon(make_icon("lock" if layer.locked else "unlock", size=24))
        self.btn_lock.setIconSize(QSize(24, 24)); self.btn_lock.setFixedSize(44, 44)
        self.btn_lock.clicked.connect(lambda: self.lock_clicked.emit(self.layer))
        h.addWidget(self.btn_lock)

        pm = QPixmap(48, 48)
        p = QPainter(pm)
        p.fillRect(pm.rect(), checker_pixmap(6, "#3a3a42", "#55555f"))
        small = layer.image.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawImage((48 - small.width()) // 2, (48 - small.height()) // 2, small)
        p.setPen(QPen(QColor(255, 255, 255, 30), 1)); p.setBrush(Qt.NoBrush)
        p.drawRect(0, 0, 47, 47)
        p.end()
        self.thumb = QLabel(); self.thumb.setPixmap(pm); self.thumb.setFixedSize(48, 48)
        h.addWidget(self.thumb)

        self.name_label = QLabel(layer.name)
        self.name_label.setStyleSheet("font-size: 15px;")
        self.name_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        if not layer.visible:
            self.name_label.setStyleSheet(f"font-size: 15px; color:{MUTED};")
        h.addWidget(self.name_label, 1)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.rename_requested.emit(self.layer)


class LayerPanelMobile(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = None
        self._syncing = False
        self._rows = []

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)

        self.list = QListWidget()
        self.list.setObjectName("LayerList")
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        v.addWidget(self.list, 1)

        btns = QHBoxLayout(); btns.setSpacing(4)
        self.btn_add = self._mk_btn("plus", "Nueva capa")
        self.btn_dup = self._mk_btn("copy", "Duplicar")
        self.btn_up = self._mk_btn("up", "Subir")
        self.btn_down = self._mk_btn("down", "Bajar")
        self.btn_merge = self._mk_btn("merge", "Combinar")
        self.btn_del = self._mk_btn("trash", "Eliminar")
        for b in (self.btn_add, self.btn_dup, self.btn_up,
                  self.btn_down, self.btn_merge, self.btn_del):
            btns.addWidget(b)
        v.addLayout(btns)

        brow = QHBoxLayout()
        bl = QLabel("Fusión"); bl.setObjectName("Muted")
        self.blend = QComboBox()
        for key, (label, _) in BLEND_MODES.items():
            self.blend.addItem(label, key)
        brow.addWidget(bl); brow.addWidget(self.blend, 1)
        v.addLayout(brow)

        opr = QHBoxLayout()
        ol = QLabel("Opacidad"); ol.setObjectName("Muted")
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100); self.opacity.setValue(100)
        opr.addWidget(ol); opr.addWidget(self.opacity, 1)
        v.addLayout(opr)

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
    def _mk_btn(icon, tip):
        b = QToolButton()
        b.setIcon(make_icon(icon, size=24))
        b.setIconSize(QSize(24, 24))
        b.setFixedHeight(48)
        b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        b.setToolTip(tip)
        return b

    def set_document(self, doc):
        self.doc = doc
        if self.doc is not None:
            for sig, slot in ((doc.structure_changed, self._rebuild),
                              (doc.active_changed, self._on_active_changed)):
                try: sig.disconnect(slot)
                except (RuntimeError, TypeError): pass
        doc.structure_changed.connect(self._rebuild)
        doc.active_changed.connect(self._on_active_changed)
        self._rebuild()

    def _rebuild(self):
        if self.doc is None: return
        self._syncing = True
        scroll = self.list.verticalScrollBar().value()
        self.list.blockSignals(True); self.list.clear()
        self._rows = []
        layers, active = self.doc.layers, self.doc.active_index
        n = len(layers)
        for vis in range(n):
            idx = n - 1 - vis
            L = layers[idx]
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, idx)
            item.setSizeHint(QSize(0, 68))
            row = LayerRowMobile(L, idx == active)
            row.visibility_clicked.connect(self._toggle_vis)
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
        self._rebuild()

    def _sync_controls(self):
        if self.doc is None: return
        L = self.doc.active_layer
        n = len(self.doc.layers); i = self.doc.active_index
        if L:
            self.opacity.blockSignals(True)
            self.opacity.setValue(int(round(L.opacity * 100)))
            self.opacity.blockSignals(False)
            self.blend.blockSignals(True)
            self.blend.setCurrentIndex(max(0, self.blend.findData(L.blend)))
            self.blend.blockSignals(False)
        self.btn_del.setEnabled(n > 1)
        self.btn_up.setEnabled(i < n - 1)
        self.btn_down.setEnabled(i > 0)
        self.btn_merge.setEnabled(i > 0)

    def _on_row_selected(self, vis_row):
        if self._syncing or self.doc is None: return
        n = len(self.doc.layers)
        if 0 <= vis_row < n:
            self.doc.set_active(n - 1 - vis_row)

    def _on_rows_moved(self, *_):
        QTimer.singleShot(0, self._apply_drag_order)

    def _apply_drag_order(self):
        if self.doc is None: return
        n = len(self.doc.layers)
        order_idx = []
        for r in range(self.list.count()):
            v = self.list.item(r).data(Qt.UserRole)
            if v is not None: order_idx.append(int(v))
        if len(order_idx) == n and sorted(order_idx) == list(range(n)):
            ordered = [self.doc.layers[i] for i in reversed(order_idx)]
            self.doc.reorder(ordered)
        self._rebuild()

    def _toggle_vis(self, layer):
        self.doc.set_layer_prop(layer, "visible", not layer.visible, "Visibilidad")
    def _toggle_lock(self, layer):
        self.doc.set_layer_prop(layer, "locked", not layer.locked, "Bloqueo")
    def _rename(self, layer):
        name, ok = QInputDialog.getText(self, "Renombrar", "Nombre:", text=layer.name)
        if ok and name.strip():
            self.doc.set_layer_prop(layer, "name", name.strip(), "Renombrar")
    def _on_opacity(self, v):
        if self._syncing or self.doc is None or self.doc.active_layer is None: return
        self.doc.set_layer_prop(self.doc.active_layer, "opacity", v / 100.0, "Opacidad")
    def _on_blend(self, _):
        if self._syncing or self.doc is None or self.doc.active_layer is None: return
        self.doc.set_layer_prop(self.doc.active_layer, "blend",
                                self.blend.currentData(), "Fusión")
    def _merge(self):
        if self.doc is None: return
        ok, why = self.doc.merge_down()
        if not ok and why:
            QMessageBox.information(self, "Combinar", why)


# ============================================================
#  DIÁLOGO NUEVO DOCUMENTO
# ============================================================
class NewDocDialogMobile(QDialog):
    PRESETS = [("512 × 512", 512, 512), ("1024 × 1024", 1024, 1024),
               ("2048 × 2048", 2048, 2048), ("Personalizado", 0, 0)]

    def __init__(self, parent=None, default=(1024, 1024)):
        super().__init__(parent)
        self.setWindowTitle("Nuevo documento")
        self.setModal(True)
        form = QFormLayout(self)
        form.setContentsMargins(16, 16, 16, 12); form.setSpacing(10)
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
        self.preset.setCurrentIndex(1)
        self.preset.currentIndexChanged.connect(self._on_preset)
        self.w.valueChanged.connect(self._to_custom)
        self.h.valueChanged.connect(self._to_custom)

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
#  VENTANA MÓVIL
# ============================================================
TOOLS_MOBILE = [
    ("brush", "brush", "Pincel"),
    ("eraser", "eraser", "Borrador"),
    ("fill", "fill", "Relleno"),
    ("picker", "picker", "Cuentagotas"),
    ("line", "line", "Línea"),
    ("rect", "rect", "Rectángulo"),
    ("ellipse", "ellipse", "Elipse"),
    ("hand", "hand", "Mano"),
]


class MobileWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = ToolSettings()
        self.doc = None
        self._drawer_left_open = False
        self._drawer_right_open = False

        root = QWidget(); root.setObjectName("MobileRoot")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        # 1) Top bar
        self.top_bar = self._build_top_bar()
        v.addWidget(self.top_bar)

        # 2) Canvas
        self.canvas = MobileCanvas(self.settings)
        self.canvas.zoomChanged.connect(self._on_zoom_changed)
        self.canvas.message.connect(self._toast)
        self.canvas.colorPicked.connect(self._on_color_picked)
        self.canvas.colorUsed.connect(lambda c: self.color_panel.set_color(c) if self.color_panel else None)
        v.addWidget(self.canvas, 1)

        # 3) Bottom bar
        self.bottom_bar = self._build_bottom_bar()
        v.addWidget(self.bottom_bar)

        # 4) Drawers (creados ocultos)
        self.drawer_left = self._build_drawer_color()
        self.drawer_right = self._build_drawer_layers()
        self.drawer_left.hide()
        self.drawer_right.hide()

        # Animaciones
        self._anim_left = QPropertyAnimation(self.drawer_left, b"pos", self)
        self._anim_left.setDuration(220)
        self._anim_left.setEasingCurve(QEasingCurve.OutCubic)
        self._anim_left.finished.connect(self._on_anim_left_done)
        self._anim_right = QPropertyAnimation(self.drawer_right, b"pos", self)
        self._anim_right.setDuration(220)
        self._anim_right.setEasingCurve(QEasingCurve.OutCubic)
        self._anim_right.finished.connect(self._on_anim_right_done)

        # Toast
        self._toast_label = QLabel("", self)
        self._toast_label.setStyleSheet(
            f"background: {BG2}; color: {TEXT}; border-radius: 8px; padding: 8px 14px;")
        self._toast_label.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._toast_label.hide)

        self.new_document(1024, 1024, True, confirm=False)

    # ---------- Top bar ----------
    def _build_top_bar(self):
        bar = QFrame(); bar.setObjectName("TopBar"); bar.setFixedHeight(56)
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 6, 6, 6); h.setSpacing(2)

        b_menu = self._tool_btn("menu", self._open_menu, 44)
        h.addWidget(b_menu)

        self.b_undo = self._tool_btn("undo", self._cmd_undo, 44)
        self.b_redo = self._tool_btn("redo", self._cmd_redo, 44)
        h.addWidget(self.b_undo); h.addWidget(self.b_redo)

        h.addStretch()

        self._tool_btns = {}
        self._tool_group = QButtonGroup(self); self._tool_group.setExclusive(True)
        for key, icon, _ in TOOLS_MOBILE[:4]:  # 4 principales en top
            b = QToolButton(); b.setObjectName("ToolBtnMobile")
            b.setIcon(make_icon(icon, size=24)); b.setIconSize(QSize(24, 24))
            b.setFixedSize(44, 44); b.setCheckable(True)
            b.setProperty("tool", key)
            b.clicked.connect(lambda *_, k=key: self._set_tool(k))
            self._tool_group.addButton(b)
            self._tool_btns[key] = b
            h.addWidget(b)
        self._tool_btns["brush"].setChecked(True)

        h.addStretch()

        b_layers = self._tool_btn("layers", self._toggle_right_drawer, 44)
        h.addWidget(b_layers)
        return bar

    def _tool_btn(self, icon, slot, size=44):
        b = QToolButton()
        b.setIcon(make_icon(icon, size=int(size * 0.55)))
        b.setIconSize(QSize(int(size * 0.55), int(size * 0.55)))
        b.setFixedSize(size, size)
        b.clicked.connect(lambda *_: slot())
        return b

    # ---------- Bottom bar contextual ----------
    def _build_bottom_bar(self):
        bar = QFrame(); bar.setObjectName("BottomBar"); bar.setFixedHeight(64)
        self._bottom_layout = QHBoxLayout(bar)
        self._bottom_layout.setContentsMargins(8, 8, 8, 8)
        self._bottom_layout.setSpacing(8)
        self._rebuild_bottom_bar()
        return bar

    def _rebuild_bottom_bar(self):
        while self._bottom_layout.count():
            it = self._bottom_layout.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        tool = self.settings.tool

        if tool in ("brush", "eraser", "line", "rect", "ellipse"):
            self.color_chip = ColorChip(self.settings.color)
            self.color_chip.clicked = self._toggle_left_drawer
            self.color_chip.mousePressEvent = lambda e: self._toggle_left_drawer()
            self._bottom_layout.addWidget(self.color_chip)

            self._bottom_layout.addWidget(QLabel("Grosor"))
            s = QSlider(Qt.Horizontal)
            s.setRange(1, 200); s.setValue(self.settings.size)
            self._size_slider = s
            self._bottom_layout.addWidget(s, 1)
            self._size_lbl = QLabel(f"{self.settings.size}")
            self._size_lbl.setFixedWidth(40)
            self._bottom_layout.addWidget(self._size_lbl)
            s.valueChanged.connect(self._on_size)
            s.valueChanged.connect(lambda v: self._size_lbl.setText(str(v)))

        elif tool == "fill":
            self.color_chip = ColorChip(self.settings.color)
            self.color_chip.mousePressEvent = lambda e: self._toggle_left_drawer()
            self._bottom_layout.addWidget(self.color_chip)
            self._bottom_layout.addWidget(QLabel("Tolerancia"))
            t = QSlider(Qt.Horizontal); t.setRange(0, 100); t.setValue(self.settings.tolerance)
            self._bottom_layout.addWidget(t, 1)
            t.valueChanged.connect(lambda v: setattr(self.settings, "tolerance", v))

        elif tool == "picker":
            self.color_chip = ColorChip(self.settings.color)
            self._bottom_layout.addWidget(self.color_chip)
            l = QLabel("Toca el canvas para tomar un color")
            l.setObjectName("Muted")
            self._bottom_layout.addWidget(l, 1)

        elif tool == "hand":
            l = QLabel("Arrastra para mover. Pinch para zoom.")
            l.setObjectName("Muted")
            self._bottom_layout.addStretch(); self._bottom_layout.addWidget(l)
            self._bottom_layout.addStretch()

    # ---------- Drawers ----------
    def _build_drawer_color(self):
        w = QFrame(); w.setObjectName("Drawer")
        w.setFixedWidth(min(340, int(self.width() * 0.9) if self.width() > 100 else 320))
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 12); v.setSpacing(10)

        hdr = QHBoxLayout()
        t = QLabel("Color y opciones"); t.setStyleSheet("font-size:15px; font-weight:600;")
        hdr.addWidget(t); hdr.addStretch()
        btn_close = QToolButton(); btn_close.setIcon(make_icon("close", size=24))
        btn_close.setIconSize(QSize(24, 24)); btn_close.setFixedSize(44, 44)
        btn_close.clicked.connect(self._toggle_left_drawer)
        hdr.addWidget(btn_close)
        v.addLayout(hdr)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 4, 0); iv.setSpacing(12)

        self.color_panel = ColorPanelMobile(self.settings.color)
        self.color_panel.colorChanged.connect(self._on_color_changed)
        iv.addWidget(self.color_panel)

        # Opacidad de trazo
        iv.addWidget(QLabel("Opacidad"))
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(1, 100); self.opacity_slider.setValue(100)
        self.opacity_slider.valueChanged.connect(
            lambda v: setattr(self.settings, "opacity", v / 100.0))
        iv.addWidget(self.opacity_slider)

        # Presión
        iv.addWidget(QLabel("Presión"))
        self.pressure_combo = QComboBox()
        self.pressure_combo.addItem("Velocidad", "velocity")
        self.pressure_combo.addItem("Tableta (presión real)", "tablet")
        self.pressure_combo.addItem("Ninguna", "none")
        self.pressure_combo.currentIndexChanged.connect(
            lambda i: setattr(self.settings, "pressure_mode", self.pressure_combo.itemData(i)))
        iv.addWidget(self.pressure_combo)

        # Tipo de trazo
        iv.addWidget(QLabel("Tipo de trazo"))
        self.type_combo = QComboBox()
        self.type_combo.addItem("Libre (con dureza)", "free")
        self.type_combo.addItem("Determinista (vectorial)", "deterministic")
        self.type_combo.setCurrentIndex(1)
        self.type_combo.currentIndexChanged.connect(
            lambda i: setattr(self.settings, "stroke_type", self.type_combo.itemData(i)))
        iv.addWidget(self.type_combo)

        # Dureza
        iv.addWidget(QLabel("Dureza"))
        self.hardness_slider = QSlider(Qt.Horizontal)
        self.hardness_slider.setRange(0, 100); self.hardness_slider.setValue(100)
        self.hardness_slider.valueChanged.connect(
            lambda v: setattr(self.settings, "hardness", v / 100.0))
        iv.addWidget(self.hardness_slider)

        # Tileado
        iv.addWidget(QLabel("Textura repetible"))
        row = QHBoxLayout(); row.setSpacing(6)
        b_wrap_x = QPushButton("Wrap X"); b_wrap_x.setCheckable(True)
        b_wrap_x.setChecked(self.settings.tile_x)
        b_wrap_x.toggled.connect(lambda on: setattr(self.settings, "tile_x", on))
        row.addWidget(b_wrap_x)
        b_wrap_y = QPushButton("Wrap Y"); b_wrap_y.setCheckable(True)
        b_wrap_y.setChecked(self.settings.tile_y)
        b_wrap_y.toggled.connect(lambda on: setattr(self.settings, "tile_y", on))
        row.addWidget(b_wrap_y)
        iv.addLayout(row)

        b_preview = QPushButton("Vista 3×3")
        b_preview.setCheckable(True)
        b_preview.toggled.connect(lambda on: self.canvas.set_tile_preview(on))
        iv.addWidget(b_preview)

        iv.addStretch()
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        return w

    def _build_drawer_layers(self):
        w = QFrame(); w.setObjectName("Drawer")
        w.setFixedWidth(min(360, int(self.width() * 0.92) if self.width() > 100 else 340))
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 12); v.setSpacing(10)

        hdr = QHBoxLayout()
        t = QLabel("Capas"); t.setStyleSheet("font-size:15px; font-weight:600;")
        hdr.addWidget(t); hdr.addStretch()
        btn_close = QToolButton(); btn_close.setIcon(make_icon("close", size=24))
        btn_close.setIconSize(QSize(24, 24)); btn_close.setFixedSize(44, 44)
        btn_close.clicked.connect(self._toggle_right_drawer)
        hdr.addWidget(btn_close)
        v.addLayout(hdr)

        self.layer_panel = LayerPanelMobile()
        v.addWidget(self.layer_panel, 1)
        return w

    # ---------- Drawer animación ----------
    def _drawer_geometry(self):
        top = self.top_bar.height()
        bottom = self.bottom_bar.height()
        return top, self.height() - top - bottom

    def _toggle_left_drawer(self):
        if self._drawer_left_open:
            self._close_drawer(self.drawer_left, self._anim_left, from_left=True)
        else:
            self._open_drawer(self.drawer_left, self._anim_left, from_left=True)
        self._drawer_left_open = not self._drawer_left_open

    def _toggle_right_drawer(self):
        if self._drawer_right_open:
            self._close_drawer(self.drawer_right, self._anim_right, from_left=False)
        else:
            self._open_drawer(self.drawer_right, self._anim_right, from_left=False)
        self._drawer_right_open = not self._drawer_right_open

    def _open_drawer(self, drawer, anim, from_left):
        top, h = self._drawer_geometry()
        drawer.setFixedHeight(h)
        dw = drawer.width()
        start_x = -dw if from_left else self.width()
        end_x = 0 if from_left else self.width() - dw
        drawer.move(start_x, top)
        drawer.show(); drawer.raise_()
        anim.stop()
        anim.setStartValue(QPoint(start_x, top))
        anim.setEndValue(QPoint(end_x, top))
        anim.start()

    def _close_drawer(self, drawer, anim, from_left):
        top, h = self._drawer_geometry()
        dw = drawer.width()
        end_x = -dw if from_left else self.width()
        anim.stop()
        anim.setStartValue(drawer.pos())
        anim.setEndValue(QPoint(end_x, top))
        anim.start()

    def _on_anim_left_done(self):
        if not self._drawer_left_open:
            self.drawer_left.hide()

    def _on_anim_right_done(self):
        if not self._drawer_right_open:
            self.drawer_right.hide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        top, h = self._drawer_geometry()
        self.drawer_left.setFixedHeight(h)
        self.drawer_right.setFixedHeight(h)
        self.drawer_left.move(-self.drawer_left.width() if not self._drawer_left_open
                              else 0, top)
        self.drawer_right.move(self.width() if not self._drawer_right_open
                               else self.width() - self.drawer_right.width(), top)

    # ---------- Menú ----------
    def _open_menu(self):
        m = QMenu(self)
        m.addAction("Nuevo…", self._cmd_new)
        m.addAction("Abrir…", self._cmd_open)
        m.addSeparator()
        m.addAction("Guardar", self._cmd_save)
        m.addAction("Guardar como…", self._cmd_save_as)
        m.addAction("Exportar imagen…", self._cmd_export)
        m.addSeparator()
        m.addAction("Ajustar a pantalla", self.canvas.fit_view)
        m.addAction("100 %", self.canvas.zoom_actual)
        m.addSeparator()
        m.addAction("Acerca de", self._show_about)
        m.exec(self.mapToGlobal(QPoint(8, self.top_bar.height())))

    # ---------- Documento ----------
    def _set_document(self, doc):
        self.doc = doc
        self.canvas.set_document(doc)
        self.layer_panel.set_document(doc)
        doc.undo_stack.canUndoChanged.connect(self._sync_undo)
        doc.undo_stack.canRedoChanged.connect(self._sync_undo)
        doc.undo_stack.cleanChanged.connect(self._on_clean_changed)
        self._sync_undo()
        self._update_title()

    def new_document(self, w, h, white, confirm=True):
        if confirm and self.doc is not None:
            if self.doc.undo_stack.isClean():
                pass
            else:
                r = QMessageBox.question(
                    self, "Cambios sin guardar",
                    "¿Descartar los cambios actuales?",
                    QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Discard)
                if r != QMessageBox.Discard:
                    return
        self._set_document(Document(w, h, white_background=white))

    def _update_title(self):
        if self.doc is None: return
        mark = "" if self.doc.undo_stack.isClean() else "*"
        self.setWindowTitle(f"{self.doc.name}{mark} — {APP_NAME}")

    def _on_clean_changed(self, *_):
        self._update_title()

    def _sync_undo(self, *_):
        if self.doc is None: return
        self.b_undo.setEnabled(self.doc.undo_stack.canUndo())
        self.b_redo.setEnabled(self.doc.undo_stack.canRedo())

    def _cmd_undo(self):
        if self.doc is not None and not self.canvas._stroking:
            self.doc.undo_stack.undo()

    def _cmd_redo(self):
        if self.doc is not None and not self.canvas._stroking:
            self.doc.undo_stack.redo()

    def _cmd_new(self):
        dlg = NewDocDialogMobile(self, (self.doc.width_px, self.doc.height_px))
        if dlg.exec() == QDialog.Accepted:
            w, h, white = dlg.values()
            self.new_document(w, h, white)

    def _cmd_open(self):
        if self.doc is not None and not self.doc.undo_stack.isClean():
            r = QMessageBox.question(self, "Cambios sin guardar",
                                     "¿Descartar los cambios actuales?",
                                     QMessageBox.Discard | QMessageBox.Cancel,
                                     QMessageBox.Discard)
            if r != QMessageBox.Discard:
                return
        try:
            path, _ = QFileDialog.getOpenFileName(self, "Abrir")
        except Exception:
            path = ""
        if not path:
            return
        try:
            if path.lower().endswith(PROJECT_EXT):
                doc = load_project(path)
            else:
                img = QImage(path)
                if img.isNull():
                    raise ValueError("No se pudo leer la imagen")
                doc = document_from_image(img, os.path.splitext(os.path.basename(path))[0])
        except Exception as ex:
            QMessageBox.critical(self, "Error al abrir", str(ex))
            return
        self._set_document(doc)

    def _cmd_save(self):
        if self.doc.path and self.doc.path.lower().endswith(PROJECT_EXT):
            return self._write_project(self.doc.path)
        return self._cmd_save_as()

    def _cmd_save_as(self):
        default = self.doc.name + PROJECT_EXT
        path, _ = QFileDialog.getSaveFileName(self, "Guardar proyecto", default)
        if not path:
            return False
        if not path.lower().endswith(PROJECT_EXT):
            path += PROJECT_EXT
        return self._write_project(path)

    def _write_project(self, path):
        try:
            save_project(self.doc, path)
        except Exception as ex:
            QMessageBox.critical(self, "Error al guardar", str(ex))
            return False
        self.doc.path = path
        self.doc.name = os.path.splitext(os.path.basename(path))[0]
        self.doc.undo_stack.setClean()
        self._update_title()
        self._toast("Guardado")
        return True

    def _cmd_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Exportar imagen",
                                              self.doc.name + ".png")
        if not path: return
        if not os.path.splitext(path)[1]:
            path += ".png"
        img = self.doc.flatten()
        fmt = "PNG"
        if path.lower().endswith((".jpg", ".jpeg")):
            fmt = "JPEG"
            img = self.doc.flatten(QColor(255, 255, 255)).convertToFormat(QImage.Format_RGB32)
        elif path.lower().endswith(".webp"):
            fmt = "WEBP"
        elif path.lower().endswith(".bmp"):
            fmt = "BMP"
            img = self.doc.flatten(QColor(255, 255, 255)).convertToFormat(QImage.Format_RGB32)
        ok = img.save(path, fmt, 95) if fmt in ("JPEG", "WEBP") else img.save(path, fmt)
        if ok: self._toast("Exportado")
        else: QMessageBox.critical(self, "Error", "No se pudo exportar.")

    def _show_about(self):
        QMessageBox.about(self, f"Acerca de {APP_NAME}",
                          f"<h3>{APP_NAME}</h3>"
                          "<p>Editor de texturas por capas para Android.</p>"
                          "<p>Gestos:</p>"
                          "<ul><li>1 dedo: dibujar</li>"
                          "<li>2 dedos: zoom + pan</li>"
                          "<li>Long-press: cuentagotas</li></ul>")

    # ---------- Herramientas ----------
    def _set_tool(self, key):
        self.settings.tool = key
        if key in self._tool_btns:
            self._tool_btns[key].setChecked(True)
        self._rebuild_bottom_bar()

    def _on_color_changed(self, c):
        self.settings.color = QColor(c)
        if hasattr(self, "color_chip") and self.color_chip:
            self.color_chip.set_color(c)

    def _on_color_picked(self, c):
        self.settings.color = QColor(c)
        if hasattr(self, "color_chip") and self.color_chip:
            self.color_chip.set_color(c)
        if hasattr(self, "color_panel") and self.color_panel:
            self.color_panel.set_color(c, emit=False)

    def _on_size(self, v):
        self.settings.size = v

    def _on_zoom_changed(self, z):
        pass  # opcional: mostrar en algún lado

    # ---------- Toast ----------
    def _toast(self, text, ms=2200):
        self._toast_label.setText(text)
        self._toast_label.adjustSize()
        x = (self.width() - self._toast_label.width()) // 2
        y = self.height() - self.bottom_bar.height() - self._toast_label.height() - 16
        self._toast_label.move(x, y)
        self._toast_label.show(); self._toast_label.raise_()
        self._toast_timer.start(ms)

    # ---------- Cierre ----------
    def closeEvent(self, e):
        if self.canvas._stroking:
            self.canvas.cancel_stroke()
        if self.doc is not None and not self.doc.undo_stack.isClean():
            r = QMessageBox.question(self, "Cambios sin guardar",
                                     "¿Salir sin guardar?",
                                     QMessageBox.Discard | QMessageBox.Cancel,
                                     QMessageBox.Cancel)
            if r != QMessageBox.Discard:
                e.ignore(); return
        e.accept()