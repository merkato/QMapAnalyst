"""
Test synchronizacji widoków, dodawania GCP i plików Georeferencera w QGIS (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/sync_qgis.py
"""
import faulthandler
import math
import os
import sys
import tempfile

faulthandler.enable()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

import numpy as np
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsPointXY, QgsProject,
                       QgsRasterLayer, QgsRectangle)

qgs = QgsApplication([], True)
qgs.initQgis()

from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtWidgets import QFileDialog, QMainWindow, QMessageBox

dm = __import__(f"{PKG}.dockwidget", fromlist=["*"])
gcp_io = __import__(f"{PKG}.gcp_io", fromlist=["*"])


def run():
    win = QMainWindow()
    canvas = QgsMapCanvas(win)
    win.setCentralWidget(canvas)
    win.resize(900, 700)
    win.show()

    class Iface:
        def mapCanvas(self):
            return canvas

        def mainWindow(self):
            return win

    crs = QgsCoordinateReferenceSystem("EPSG:2180")
    canvas.setDestinationCrs(crs)
    canvas.setExtent(QgsRectangle(400000, 400000, 600000, 600000))
    dock = dm.QMapAnalystDockWidget(Iface())
    errors = []
    dock.report_exception = lambda where, exc: errors.append((where, repr(exc)))
    dock.resize(450, 900)
    dock.show()

    tmp = tempfile.mkdtemp(prefix="qma_sync_")
    from osgeo import gdal
    gdal.UseExceptions()
    path = os.path.join(tmp, "scan.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 4000, 3000, 1, gdal.GDT_Byte)
    ds = None
    raster = QgsRasterLayer(path, "scan")
    dock.set_old_raster(raster)

    # Mapa: Helmert (skala 25 m/px, obrót 2°) + szum
    ang = math.radians(2.0)
    A = 25.0 * np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
    t = np.array([450000.0, 560000.0])
    fwd = lambda p: np.asarray(p) @ A.T + t
    rng = np.random.default_rng(4)
    old_pts = rng.uniform([200, -2800], [3800, -200], (8, 2))

    # --- 1. dodawanie par trybem przechwytywania; po 4 punktach transformacja liczy się sama
    dock.btn_capture.setChecked(True)
    assert canvas.mapTool() is not dock.tool_click_ref
    for k, (ox, oy) in enumerate(old_pts):
        dock.on_old_canvas_clicked(QgsPointXY(ox, oy), Qt.MouseButton.LeftButton)
        assert canvas.mapTool() is dock.tool_click_ref, "krok 2 powinien aktywować narzędzie mapy głównej"
        if k >= 4:
            assert dock._pred_marker is not None, "brak przewidywanego położenia"
            pc = dock._pred_marker.center()
            exp = fwd([ox, oy])
            assert math.hypot(pc.x() - exp[0], pc.y() - exp[1]) < 50, (pc.x(), pc.y(), exp)
            if dock.btn_sync_views.isChecked():
                c = canvas.center()
                assert math.hypot(c.x() - pc.x(), c.y() - pc.y()) < 1.0, "mapa główna nie wycentrowana na przewidywanym punkcie"
        rx, ry = fwd([ox, oy]) + rng.normal(0, 3, 2)
        dock.on_ref_canvas_clicked(QgsPointXY(rx, ry), Qt.MouseButton.LeftButton)
        assert dock._pred_marker is None
        if dock._recalc_timer.isActive():   # symulacja upływu czasu timera
            dock._recalc_timer.stop()
            dock.run_analysis(auto=True)
        if k == 3:
            assert dock.analysis is not None, "po 4 punktach transformacja powinna policzyć się automatycznie"
            assert "KROK 1/2" in dock.lbl_status.text(), dock.lbl_status.text()
            dock.btn_sync_views.setChecked(True)
    print("capture OK:", len(dock.gcps), "pairs;", dock.lbl_status.text()[:90])
    dock.btn_capture.setChecked(False)
    assert canvas.mapTool() is not dock.tool_click_ref

    # --- 2. synchronizacja dawna -> główna
    counter = {"main": 0, "old": 0}
    canvas.extentsChanged.connect(lambda: counter.__setitem__("main", counter["main"] + 1))
    dock.old_canvas.extentsChanged.connect(lambda: counter.__setitem__("old", counter["old"] + 1))
    dock.old_canvas.setExtent(QgsRectangle(1000, -2000, 1400, -1700))
    oc = dock.old_canvas.extent().center()
    exp = fwd([oc.x(), oc.y()])
    mc = canvas.extent().center()
    print("old->main center error [m]:", round(math.hypot(mc.x() - exp[0], mc.y() - exp[1]), 2),
          "main width [m]:", round(canvas.extent().width()), "events:", counter)
    assert math.hypot(mc.x() - exp[0], mc.y() - exp[1]) < 30
    assert counter["old"] <= 2 and counter["main"] <= 2, counter

    # --- 3. synchronizacja główna -> dawna
    counter.update(main=0, old=0)
    target = fwd([3000, -500])
    canvas.setExtent(QgsRectangle(target[0] - 3000, target[1] - 2000, target[0] + 3000, target[1] + 2000))
    oc = dock.old_canvas.extent().center()
    print("main->old center error [px]:", round(math.hypot(oc.x() - 3000, oc.y() + 500), 2), "events:", counter)
    assert math.hypot(oc.x() - 3000, oc.y() + 500) < 2
    assert counter["old"] <= 2 and counter["main"] <= 2, counter

    # --- 4. inny CRS mapy głównej – synchronizacja dalej działa
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    dock.old_canvas.setExtent(QgsRectangle(2000, -1500, 2300, -1300))
    c = canvas.extent().center()
    back = dock._canvas_to_ref(c)
    exp = fwd([2150, -1400])
    print("old->main (EPSG:3857) center error [m]:", round(math.hypot(back.x() - exp[0], back.y() - exp[1]), 2))
    assert math.hypot(back.x() - exp[0], back.y() - exp[1]) < 30

    # --- 5. zapis .points z WKT i odczyt
    pts_path = os.path.join(tmp, "scan.tif.points")
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (pts_path, "*.points"))
    dock.gcps[2].enabled = False
    dock.save_gcp_to_file()
    first = open(pts_path, encoding="utf-8").readline()
    assert first.startswith("#CRS: ") and ("PROJCRS" in first or "PROJCS" in first), first[:60]
    parsed = QgsCoordinateReferenceSystem(first[6:].strip())
    assert parsed.isValid() and parsed.authid() == "EPSG:2180", parsed.authid()
    saved = [(g.old_x, g.old_y, g.ref_x, g.ref_y, g.enabled) for g in dock.gcps]
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (pts_path, ""))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    dock.load_gcp_from_file()
    loaded = [(g.old_x, g.old_y, g.ref_x, g.ref_y, g.enabled) for g in dock.gcps]
    assert loaded == saved, "round-trip .points zmienił dane"
    assert dock.ref_crs.authid() == "EPSG:2180"
    print("points round-trip OK:", dock.lbl_status.text())

    # --- 6. dopisanie punktów z pliku w innym układzie (EPSG:3857) – przeliczenie
    from qgis.core import QgsCoordinateTransform
    ct = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance())
    extra = []
    for ox, oy in [(500, -500), (3500, -2500)]:
        r = fwd([ox, oy])
        p = ct.transform(QgsPointXY(*r))
        extra.append((ox, oy, p.x(), p.y(), True))
    other = os.path.join(tmp, "other.points")
    gcp_io.write_file(other, gcp_io.format_points(extra, "EPSG:3857"))
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (other, ""))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    dock.load_gcp_from_file()
    g = dock.gcps[-1]
    exp = fwd([3500, -2500])
    assert math.hypot(g.ref_x - exp[0], g.ref_y - exp[1]) < 0.01, (g.ref_x, g.ref_y, exp)
    print("append from EPSG:3857 OK:", dock.lbl_status.text())

    # --- 7. test hipotezy (preset TKKP) – estymacja uruchamia się sama, siatka hipotezy na skanie
    dock.cmb_hyp_preset.setCurrentIndex(dock.cmb_hyp_preset.findData("tkkp"))
    assert dock.cmb_hyp_proj.currentData() == "bonne"
    assert dock.hyp_edits["lon0"].text() == "38°42′30″" and dock.cmb_hyp_meridian.currentData() == "Ferro"
    key, params = dock.hypothesis_values()
    assert abs(params["lon0"] - (21 + 2.5 / 60)) < 1e-9 and params["lat1"] == 52.0
    dock.run_hypothesis()
    hyp = [c for c in dock.projection.candidates if c.is_hypothesis]
    assert len(hyp) == 1, dock.lbl_status.text()
    assert dock.registry.has("old.graticule.bonne.hyp"), "brak siatki hipotezy"
    assert "(hipoteza)" in dock.txt_proj.toPlainText() or "hipoteza" in dock.txt_proj.toPlainText().lower()
    print("hypothesis:", dock.lbl_status.text())
    # niepoprawny kąt – komunikat, bez wyjątku
    dock.hyp_edits["lat1"].setText("52°75′")
    dock.run_hypothesis()
    assert "52°75′" in dock.lbl_status.text(), dock.lbl_status.text()
    # siatka swobodnie dopasowanego kandydata obok siatki hipotezy
    dock.tbl_proj.selectRow(0 if not dock.projection.candidates[0].is_reference else 1)
    dock.generate_graticule()
    grats = [k for k in dock.registry.keys("old.graticule")]
    assert len(grats) >= 2, grats
    print("graticules:", grats)

    assert not errors, errors
    dock.close()
    dock.cleanup()
    dock.deleteLater()
    QgsApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QgsProject.instance().clear()


run()
import gc
gc.collect()
print("SYNC OK", flush=True)
qgs.exitQgis()
print("EXIT OK", flush=True)
