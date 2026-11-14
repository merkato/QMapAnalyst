"""
Zasięg mapy: ramka mapy na skanie (4 rogi wskazane przez użytkownika albo cały skan) i jej obraz
na mapie współczesnej – ramka przeliczona bieżącą transformacją (liczony na żądanie, nie zapisywany).

Domieszka (mixin) panelu QMapAnalystDockWidget – korzysta z jego pól (old_canvas, main_canvas,
old_raster_layer, analysis, library_panel…) i metod (_ref_to_canvas, set_status…).
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import Qgis, QgsCsException, QgsGeometry, QgsPointXY, QgsSettings
from qgis.gui import QgsRubberBand

from . import coverage as cv
from . import library as lb

SETTINGS_SHOW_FRAME = "QMapAnalyst/show_frame"
FRAME_COLOR = QColor(0, 140, 200)


class MapFrameMixin:
    """Wymaga w __init__: self.map_frame = None, self._frame_capture = None, self._frame_bands = {}."""

    # --- ramka na skanie ---------------------------------------------------------------
    def scan_frame(self):
        """Rogi całego skanu (współrzędne skanu) albo None, gdy skan nie jest wczytany."""
        if self.old_raster_layer is None:
            return None
        e = self.old_raster_layer.extent()
        return [(e.xMinimum(), e.yMaximum()), (e.xMaximum(), e.yMaximum()),
                (e.xMaximum(), e.yMinimum()), (e.xMinimum(), e.yMinimum())]

    def effective_frame(self):
        return self.map_frame or self.scan_frame()

    def start_frame_capture(self):
        if self.old_raster_layer is None:
            self.set_status(self.tr("Najpierw wczytaj skan mapy dawnej."), "warn")
            return
        self._clear_lib_target()
        self._frame_capture = []
        self._frame_was_capturing = self.btn_capture.isChecked()
        if not self.btn_capture.isChecked():
            self.btn_capture.setChecked(True)
        self.set_status(self.tr("Zasięg mapy: kliknij na skanie 4 rogi ramki mapy (1/4)…"))
        self.update_frame_bands()

    def frame_click(self, point):
        """Kliknięcie na mapie dawnej w trybie wskazywania ramki; True – obsłużone."""
        if self._frame_capture is None:
            return False
        self._frame_capture.append((point.x(), point.y()))
        n = len(self._frame_capture)
        if n < 4:
            self.set_status(self.tr("Zasięg mapy: kliknij kolejny róg ramki ({n}/4)…").format(n=n + 1))
        else:
            self.map_frame = self._frame_capture
            self._frame_capture = None
            if not self._frame_was_capturing:
                self.btn_capture.setChecked(False)
            self.set_status(self.tr("Zapisano zasięg mapy (4 rogi ramki). Zapisz analizę, aby zachować go w bibliotece."), "ok")
            self.update_analysis_label()
        self.update_frame_bands()
        return True

    def reset_frame(self):
        self._frame_capture = None
        self.map_frame = None
        self.update_frame_bands()
        self.update_analysis_label()
        self.set_status(self.tr("Zasięg mapy: cały skan."), "ok")

    def frame_text(self):
        return lb.frame_text(self.effective_frame())

    # --- zasięg na mapie współczesnej ---------------------------------------------------
    def footprint_ref(self):
        """Zasięg mapy w układzie odniesienia analizy (wielokąt) albo None – brak transformacji lub skanu."""
        if self.analysis is None:
            return None
        return cv.footprint(self.analysis.fit, self.effective_frame())

    def footprint_contains(self, p, area=None):
        """Czy punkt biblioteki leży w zasięgu mapy: True / False; None – zasięg nieznany."""
        area = area if area is not None else self.footprint_ref()
        if area is None:
            return None
        try:
            q = self.library_panel.to_crs(p, self._ensure_ref_crs())
        except QgsCsException:
            return None
        return lb.point_in_polygon(q.x(), q.y(), area)

    def on_show_frame_toggled(self, checked):
        QgsSettings().setValue(SETTINGS_SHOW_FRAME, bool(checked))
        self.update_frame_bands()

    def _band(self, key, canvas):
        band = self._frame_bands.get(key)
        if band is None:
            band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
            band.setStrokeColor(FRAME_COLOR)
            band.setFillColor(QColor(0, 0, 0, 0))
            band.setWidth(2)
            band.setLineStyle(Qt.PenStyle.DashLine)
            self._frame_bands[key] = band
        return band

    def _drop_band(self, key):
        band = self._frame_bands.pop(key, None)
        if band is not None:
            try:
                band.reset(Qgis.GeometryType.Polygon)
                if band.scene() is not None:
                    band.scene().removeItem(band)
            except RuntimeError:
                pass

    def update_frame_bands(self):
        """Ramka na mapie dawnej (wskazana lub wskazywana) i zasięg na mapie głównej."""
        frame = self._frame_capture if self._frame_capture is not None else self.map_frame
        if frame and len(frame) >= 2:
            pts = [QgsPointXY(x, y) for x, y in frame]
            self._band("old", self.old_canvas).setToGeometry(
                QgsGeometry.fromPolygonXY([pts]) if len(pts) >= 3 else QgsGeometry.fromPolylineXY(pts), None)
        else:
            self._drop_band("old")
        area = self.footprint_ref() if self.act_frame_show.isChecked() else None
        if area:
            try:
                pts = [self._ref_to_canvas(x, y) for x, y in area]
            except QgsCsException:
                pts = []
            if pts:
                self._band("main", self.main_canvas).setToGeometry(QgsGeometry.fromPolygonXY([pts]), None)
                return
        self._drop_band("main")

    def clear_frame_bands(self):
        for key in list(self._frame_bands):
            self._drop_band(key)
