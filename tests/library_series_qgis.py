"""
Okno „Seria czasowa” (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_series_qgis.py
"""
import faulthandler; faulthandler.enable()  # noqa: E702
import importlib.util
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsProject, QgsSettings  # noqa: E402

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QMainWindow, QMessageBox  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
sd = __import__(f"{PKG}.series_dialog", fromlist=["*"])
spec = importlib.util.spec_from_file_location("test_series", os.path.join(HERE, "test_series.py"))
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)


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
PATH_BEFORE = QgsSettings().value(lp.SETTINGS_PATH, "")
NAMES_BEFORE = QgsSettings().value(dock_mod.SETTINGS_NAMES, False, type=bool)
TMP = tempfile.mkdtemp(prefix="qma_serui_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
project = QgsProject.instance()
project.setCrs(crs)
iface = Iface()
iface.canvas.setDestinationCrs(crs)
dock = dock_mod.QMapAnalystDockWidget(iface)
panel = dock.library_panel

path = os.path.join(TMP, "lib.gpkg")
lib, sid, uid, (a, b, c), _e = ts.make_library(path)
lib.close()
assert panel.use_library(path)
assert not panel.btn_series.isEnabled()
panel.cmb_set.setCurrentIndex(panel.cmb_set.findData(sid))
assert panel.btn_series.isEnabled()

dlg = sd.SeriesDialog(panel)
t = dlg.tbl_sheets
assert t.rowCount() == 3 and t.item(0, 0).text() == "Mapa A (1770)" and t.item(0, 2).text() == "15"
assert t.item(0, 6).text().startswith("1 : 47") and t.item(1, 6).text() == "brak DPI", t.item(0, 6).text()
p = dlg.tbl_pairs
assert p.rowCount() == 3 and p.item(0, 0).text() == "Mapa A (1770)" and p.item(0, 1).text() == "Mapa B (1800)"
assert p.item(0, 0).font().bold() and not p.item(1, 0).font().bold()
assert "Podobne błędy" in dlg.lbl_summary.text() and "Mapa A (1770) → Mapa B (1800)" in dlg.lbl_summary.text()
pts = dlg.tbl_points
assert pts.rowCount() == 15 and pts.columnCount() == 5 and pts.item(0, 3).text() == "3"
assert "Zestaw „S”: punkty 15, mapy 3" == dlg.lbl_title.text()

# warstwy na mapie głównej
dlg.sp_scale.setValue(5)
vec = dlg.add_vectors_layer()
assert vec.featureCount() == 45 and vec.crs().authid() == "EPSG:2180" and "× 5" in vec.name()
f = next(vec.getFeatures())
assert f["error_m"] > 0 and f["map"] == "Mapa A (1770)"
line = f.geometry().asPolyline()
assert abs(line[1].distance(line[0]) - 5 * f["error_m"]) < 0.01 * 5 * f["error_m"] + 0.01
traj = dlg.add_trajectories_layer()
assert traj.featureCount() == 15 and next(traj.getFeatures())["maps"] == 3
assert vec.id() in project.mapLayers() and traj.id() in project.mapLayers()

# eksport
out = os.path.join(TMP, "seria")
QFileDialog.getSaveFileName = staticmethod(lambda *a_, **k: (out, "JSON (*.json)"))
dlg.export()
assert json.load(open(out + ".json", encoding="utf-8"))["pairs"][0]["similar"] is True
QFileDialog.getSaveFileName = staticmethod(lambda *a_, **k: (out, "Mapy (CSV) (*.csv)"))
dlg.export()
assert open(out + ".csv", encoding="utf-8-sig").readline().startswith("key,map,sheets")

# stan bieżący: niezapisane punkty w panelu GCP jako dodatkowy arkusz
with ts.lb.Library.open(path) as lib:
    links = lib.links(c)
for ln in links[:8]:
    dock.add_gcp(ln.x_old, ln.y_old, ln.ref_x, ln.ref_y, name=ln.name, uid=ln.uid)
dock.gcps_changed()
dlg.refresh()
assert dlg.tbl_sheets.rowCount() == 4 and dlg.tbl_sheets.item(3, 0).text().endswith("*"), \
    [dlg.tbl_sheets.item(r, 0).text() for r in range(dlg.tbl_sheets.rowCount())]
assert dlg.tbl_sheets.item(3, 2).text() == "8"

# przycisk w zakładce Biblioteka otwiera okno
sd.SeriesDialog.exec = lambda self: QDialog.DialogCode.Rejected
panel.series_dialog()

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().setValue(lp.SETTINGS_PATH, PATH_BEFORE)
QgsSettings().setValue(dock_mod.SETTINGS_NAMES, NAMES_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY SERIES OK")
