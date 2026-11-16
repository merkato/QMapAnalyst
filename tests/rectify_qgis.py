"""
Rektyfikacja mapy (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/rectify_qgis.py
Skan bez georeferencji z jasnym znacznikiem, GCP z gładkim zniekształceniem, okno „Rektyfikacja”:
pole korekcji (TPS auto) zmniejsza reszty, raster COG z kanałem alfa przycięty do ramki, znacznik
w miejscu przewidzianym przez odwzorowanie, plik ustawień i warstwa wektorów korekcji.
"""
import faulthandler; faulthandler.enable()  # noqa: E702
import json
import os
import shutil
import sys
import tempfile
import time

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox  # noqa: E402
from osgeo import gdal  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
rd = __import__(f"{PKG}.rectify_dialog", fromlist=["*"])


class Iface:
    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas(self.win)
        self.win.setCentralWidget(self.canvas)

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.win


QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
NAMES_BEFORE = QgsSettings().value(dock_mod.SETTINGS_NAMES, False, type=bool)
TMP = tempfile.mkdtemp(prefix="qma_rect_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface = Iface()
iface.canvas.setDestinationCrs(crs)
project = QgsProject.instance()
project.setCrs(crs)

raster = os.path.join(TMP, "scan.tif")
ds = gdal.GetDriverByName("GTiff").Create(raster, 1000, 800, 1, gdal.GDT_Byte)
arr = np.full((800, 1000), 120, np.uint8)
arr[395:406, 595:606] = 255                    # znacznik: piksel (600, 400) → skan (600, −400)
ds.GetRasterBand(1).WriteArray(arr)
ds = None


def truth(px, py):
    """Skan → teren: afiniczna + gładkie zniekształcenie (do ~150 m)."""
    x, y = 600000 + 10 * px, 300000 + 10 * py
    return x + 150 * np.sin(px / 180.0), y + 120 * np.cos(py / 150.0)


dock = dock_mod.QMapAnalystDockWidget(iface)
dock.chk_names.setChecked(False)
dock.set_old_raster(QgsRasterLayer(raster, "scan"))
rng = np.random.default_rng(4)
for px, py in zip(rng.uniform(30, 970, 40), rng.uniform(-770, -30, 40)):
    dock.add_gcp(px, py, *truth(px, py))
dock.cmb_transform_type.setCurrentIndex(dock.cmb_transform_type.findData("affine"))
dock.gcps_changed()
dock._recalc_timer.stop()
dock.run_analysis()
assert dock.analysis is not None and dock.btn_rectify.isEnabled()
dock.map_frame = [(20, -20), (980, -20), (980, -780), (20, -780)]

dlg = rd.RectifyDialog(dock)
c = dlg.correction
assert c.method == "tps" and c.auto and c.rmse_after < 0.3 * c.rmse_before, (c.rmse_before, c.rmse_after)
assert dlg.chk_frame.isChecked() and dlg.folds == 0
dlg.cmb_method.setCurrentIndex(dlg.cmb_method.findData("triangles"))
assert dlg.correction.rmse_after < 1e-3
dlg.cmb_method.setCurrentIndex(dlg.cmb_method.findData("tps"))
dlg.chk_auto.setChecked(False)
dlg.sld_smooth.setValue(90)
dlg.recompute()
assert dlg.correction.rmse_after > c.rmse_after
dlg.chk_auto.setChecked(True)

out = os.path.join(TMP, "wynik.tif")
dlg.ed_out.setText(out)
dlg.spin_res.setValue(5.0)
n_layers = len(project.mapLayers())
assert dlg.run() is None and dlg.task is not None and not dlg.progress.isHidden() and not dlg.btn_run.isEnabled()   # zadanie w tle
deadline = time.time() + 180
while dlg.output is None and time.time() < deadline:
    qgs.processEvents()
    time.sleep(0.05)
assert dlg.output == out and os.path.exists(out) and dlg.task is None and dlg.progress.isHidden() and dlg.btn_run.isEnabled()
info = gdal.Info(out, format="json")
assert info["bands"][-1]["colorInterpretation"] == "Alpha" and len(info["bands"]) == 4, [b["colorInterpretation"] for b in info["bands"]]
assert "2180" in info["coordinateSystem"]["wkt"] and abs(info["geoTransform"][1] - 5.0) < 1e-9
assert len(project.mapLayers()) == n_layers + 2                       # raster + wektory korekcji
settings = json.load(open(os.path.splitext(out)[0] + ".rectify.json", encoding="utf-8"))
assert settings["method"] == "tps" and settings["auto"] and len(settings["points"]) == 40

# znacznik w miejscu przewidzianym przez odwzorowanie (i blisko prawdy terenowej)
ods = gdal.Open(out)
gt = ods.GetGeoTransform()
a = ods.GetRasterBand(1).ReadAsArray()
yy, xx = np.nonzero(a > 200)
mx, my = gt[0] + (xx.mean() + 0.5) * gt[1], gt[3] + (yy.mean() + 0.5) * gt[5]
ex, ey = dlg.rectifier()(np.array([[600.0, -400.0]]))[0]
tx, ty = truth(600.0, -400.0)
assert np.hypot(mx - ex, my - ey) < 2 * gt[1], (mx - ex, my - ey)
assert np.hypot(mx - tx, my - ty) < 30, (mx - tx, my - ty)
alpha = ods.GetRasterBand(4).ReadAsArray()
assert alpha[0, 0] == 0 and alpha[alpha.shape[0] // 2, alpha.shape[1] // 2] == 255   # poza ramką przezroczyste
ods = None

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().setValue(dock_mod.SETTINGS_NAMES, NAMES_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("RECTIFY OK")
