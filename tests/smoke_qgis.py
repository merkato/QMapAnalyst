"""
Test dymny panelu w QGIS bez okna (offscreen). Uruchomienie:
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/smoke_qgis.py
"""
import faulthandler; faulthandler.enable()
import math
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

import numpy as np
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsPointXY, QgsProject, QgsRasterLayer)

qgs = QgsApplication([], True)
qgs.initQgis()

from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtWidgets import QMainWindow, QFileDialog, QMessageBox

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
pj = __import__(f"{PKG}.projections", fromlist=["*"])


class Iface:
    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas(self.win)
        self.win.setCentralWidget(self.canvas)

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.win


errors = []
iface = Iface()
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface.canvas.setDestinationCrs(crs)
QgsProject.instance().setCrs(crs)

dock = dock_mod.QMapAnalystDockWidget(iface)
orig_report = dock.report_exception


def report_exception(where, exc):
    errors.append((where, exc))
    orig_report(where, exc)


dock.report_exception = report_exception
dock.show()

tmp = tempfile.mkdtemp(prefix="qma_")

# --- syntetyczny skan
from osgeo import gdal
W, H = 3000, 2400
raster = os.path.join(tmp, "scan.tif")
ds = gdal.GetDriverByName("GTiff").Create(raster, W, H, 1, gdal.GDT_Byte)
ds.GetRasterBand(1).Fill(200)
ds = None
lyr = QgsRasterLayer(raster, "scan")
assert lyr.isValid()
dock.set_old_raster(lyr)

# --- GCP: mapa w odwzorowaniu Bonne'a
rng = np.random.default_rng(1)
n = 30
ll = np.column_stack([rng.uniform(15.0, 23.5, n), rng.uniform(49.5, 54.5, n)])
bonne = pj.PROJECTIONS_BY_KEY["bonne"]
x, y = bonne.func(np.radians(ll[:, 0]), np.radians(ll[:, 1]), {"lon0": math.radians(19.0), "lat1": math.radians(52.0)})
P = np.column_stack([x, y])
P = (P - P.min(0)) / np.ptp(P, axis=0).max() * 2800 + [100, -2700]
P += rng.normal(0, 1.0, P.shape)
P[5] += [40, -35]  # punkt odstający
ct = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, QgsProject.instance())
for (lo, la), (px, py) in zip(ll, P):
    r = ct.transform(QgsPointXY(lo, la))
    dock.add_gcp(px, py, r.x(), r.y())
dock.gcps_changed()

for i in range(dock.cmb_transform_type.count()):
    dock.cmb_transform_type.setCurrentIndex(i)
    dock.run_analysis()
    assert dock.analysis is not None, dock.lbl_status.text()
    print("model", dock.analysis.fit.model, "sigma0", round(dock.analysis.fit.sigma0, 1),
          "outliers", int(np.sum(np.nan_to_num(dock.analysis.fit.std_residuals) > 3.29)))
dock.cmb_transform_type.setCurrentIndex(1)
dock.run_analysis()
assert "Punkty odstające" in dock.txt_report.toPlainText()
assert int(np.nanargmax(dock.analysis.fit.std_residuals)) == 5

dock.spin_grid_step.setValue(20000)
for fn in (dock.generate_gcp_points, dock.generate_result_layers, dock.generate_error_circles,
           dock.generate_distortion_grid, dock.generate_old_vectors, dock.generate_old_circles,
           dock.generate_historic_grid):
    fn()
    print(fn.__name__, "->", dock.lbl_status.text())
for k in ("scale", "rot", "omega"):
    dock.generate_isolines(k)
    print("isolines", k, "->", dock.lbl_status.text())

dock.estimate_projection()
best = dock.projection.best
print("best projection:", best.key, round(best.sigma0_px, 2), {k: round(v, 2) for k, v in best.params_deg.items()})
assert best.key == "bonne"
assert any("#6" in str(m) for m in dock.projection.messages), dock.projection.messages
print("projection warnings:", dock.projection.messages)
dock.generate_graticule()
print("graticule ->", dock.lbl_status.text())

# --- przełączenie aktywności i auto-przeliczenie
dock.tbl_gcp.item(5, dock_mod.COL_ACT).setCheckState(dock_mod.Qt.CheckState.Unchecked)
dock._recalc_timer.stop()
dock.run_analysis(auto=True)
assert dock.analysis.fit.stats["n"] == n - 1
print("after disabling #6: sigma0", round(dock.analysis.fit.sigma0, 2))
dock.refresh_existing_layers()

# --- edycja współrzędnej w tabeli (wartość niepoprawna i poprawna)
dock.tbl_gcp.item(0, dock_mod.COL_OX).setText("abc")
dock.tbl_gcp.item(0, dock_mod.COL_OX).setText(f"{dock.gcps[0].old_x + 1:.2f}")

# --- zapis i odczyt GCP
pts_path = os.path.join(tmp, "gcp.points")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (pts_path, "*.points"))
dock.save_gcp_to_file()
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (pts_path, ""))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
dock.load_gcp_from_file()
assert len(dock.gcps) == n, len(dock.gcps)
assert not dock.gcps[5].enabled
print("reload ->", dock.lbl_status.text())

# --- usuwanie
dock.tbl_gcp.selectRow(3)
dock.remove_selected_gcp()
assert len(dock.gcps) == n - 1
dock._recalc_timer.stop()
dock.run_analysis()

# --- zmiana CRS mapy głównej (markery muszą się przeliczyć)
iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
dock.run_analysis()

# --- eksport
dock.estimate_projection()
b2 = dock.projection.best
print("re-estimate without outlier:", b2.key, round(b2.sigma0_px, 2), {k: round(v, 2) for k, v in b2.params_deg.items()}, {k: round(v, 2) for k, v in b2.params_std_deg.items()})
dock.file_export_dir.setFilePath(tmp)
dock.execute_export()
print("export ->", dock.lbl_status.text())
for f in sorted(os.listdir(tmp)):
    print("   ", f, os.path.getsize(os.path.join(tmp, f)))

# --- usunięcie warstwy przez użytkownika i sprzątanie
lid = dock.registry.layer("old.iso.scale").id()
QgsProject.instance().removeMapLayer(lid)
assert not dock.registry.has("old.iso.scale")
dock.refresh_existing_layers()
dock.clear_main_layers()

# --- ścieżka jak w QMapAnalystPlugin.unload()
dock.close()
dock.cleanup()
dock.deleteLater()
QgsApplication.processEvents()
del dock, lyr, orig_report, report_exception, fn
from qgis.PyQt.QtCore import QEvent
QgsApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
assert not any(type(w).__name__ == "QMapAnalystDockWidget" for w in QgsApplication.topLevelWidgets()), "panel nie zostal usuniety"
QgsProject.instance().clear()
QgsApplication.processEvents()

if errors:
    print("ERRORS:")
    for w, e in errors:
        print("  ", w, repr(e))
    sys.exit(1)
print("SMOKE OK", flush=True)
# obiekty QGIS (CRS, transformacje, kanwy) muszą zniknąć przed exitQgis()
del iface, ct, crs
import gc
gc.collect()
qgs.exitQgis()
print("EXIT OK", flush=True)
