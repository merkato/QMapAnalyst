"""
Dodawanie par GCP z biblioteki punktów (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_pick_qgis.py
Wybór punktu z wyszukiwarki, przyciąganie w kroku 2/2, „Następny z zestawu” z położeniem
przewidywanym na skanie, pomijanie i ochrona przed dublowaniem punktu w analizie.
"""
import faulthandler; faulthandler.enable()  # noqa: E702
import math
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings,
)

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox  # noqa: E402
from osgeo import gdal  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lb = __import__(f"{PKG}.library", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])


class Iface:
    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas(self.win)
        self.win.setCentralWidget(self.canvas)

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.win


QgsSettings().remove(lp.SETTINGS_PATH)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
TMP = tempfile.mkdtemp(prefix="qma_pick_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface = Iface()
iface.canvas.resize(800, 600)
iface.canvas.setDestinationCrs(crs)
project = QgsProject.instance()
project.setCrs(crs)

LEFT = Qt.MouseButton.LeftButton
A = math.radians(3)


def to_ref(px, py):
    return (600000 + 50 * (px * math.cos(A) - py * math.sin(A)),
            300000 + 50 * (px * math.sin(A) + py * math.cos(A)))


# --- skan i biblioteka: 9 punktów, zestaw z 7
raster = os.path.join(TMP, "scan.tif")
ds = gdal.GetDriverByName("GTiff").Create(raster, 2000, 1600, 1, gdal.GDT_Byte)
ds.GetRasterBand(1).Fill(200)
ds = None
PIX = {f"P{i}": (200 + 200 * i, -(300 + 250 * (i % 4))) for i in range(8)}
PIX["Łagów"] = (1500, -1400)
dock = dock_mod.QMapAnalystDockWidget(iface)
dock.set_old_raster(QgsRasterLayer(raster, "scan"))
panel = dock.library_panel
path = os.path.join(TMP, "lib.gpkg")
assert panel.create_library(path, crs)
with lb.Library.open(path) as lib:
    pts = [lb.RefPoint(n, *to_ref(*xy), type="village", alt_names="Lagow|Lagau" if n == "Łagów" else "")
           for n, xy in PIX.items()]
    lib.add_points(pts)
    uid = {p.name: p.uid for p in pts}
    lib.add_set("S", uids=[uid[n] for n in ("P0", "P1", "P2", "P3", "P4", "P5", "Łagów")])
panel.refresh_layer()
panel.reload_sets()
assert dock.ed_lib_search.isEnabled() and dock.btn_lib_next.isEnabled()
xs = [to_ref(*xy) for xy in PIX.values()]
iface.canvas.setExtent(QgsRectangle(min(x for x, _ in xs) - 2000, min(y for _, y in xs) - 2000,
                                    max(x for x, _ in xs) + 2000, max(y for _, y in xs) + 2000))
iface.canvas.refresh()


def gcp_by_name(name):
    return next(g for g in dock.gcps if g.name == name)


# --- 1. wyszukiwarka: nazwa dawna, bez znaków diakrytycznych, potem klik na skanie
dock.update_lib_search("lagau")
assert dock._lib_model.rowCount() == 1, dock._lib_model.rowCount()
assert dock._lib_model.item(0).text().startswith("Łagów · wieś"), dock._lib_model.item(0).text()
dock.on_lib_search_activated(dock._lib_model.index(0, 0))
assert dock._lib_target is not None and dock._lib_target.name == "Łagów"
assert dock.btn_capture.isChecked() and len(dock._lib_markers) == 1   # bez analizy – brak przewidywania
dock.on_old_canvas_clicked(QgsPointXY(*PIX["Łagów"]), LEFT)
g = gcp_by_name("Łagów")
assert g.uid == uid["Łagów"] and (g.ref_x, g.ref_y) == to_ref(*PIX["Łagów"]) and dock._lib_target is None
print("search:", dock.lbl_status.text())

# --- 2. przyciąganie w kroku 2/2: klik ~5 px od punktu P0
rx, ry = to_ref(*PIX["P0"])
iface.canvas.setExtent(QgsRectangle(rx - 3000, ry - 3000, rx + 3000, ry + 3000))   # tolerancja 10 px ≪ 10 km
dock.on_old_canvas_clicked(QgsPointXY(*PIX["P0"]), LEFT)
upp = iface.canvas.mapUnitsPerPixel()
dock.on_ref_canvas_clicked(QgsPointXY(rx + 4 * upp, ry - 3 * upp), LEFT)
g = gcp_by_name("P0")
assert g.uid == uid["P0"] and (g.ref_x, g.ref_y) == (rx, ry), (g.ref_x - rx, g.ref_y - ry)

# --- 3. klik daleko od punktów biblioteki – zwykła para bez uid
rx6, ry6 = to_ref(*PIX["P6"])
iface.canvas.setExtent(QgsRectangle(rx6 - 3000, ry6 - 3000, rx6 + 3000, ry6 + 3000))
dock.on_old_canvas_clicked(QgsPointXY(*PIX["P6"]), LEFT)
dock.on_ref_canvas_clicked(QgsPointXY(rx6 + 60 * upp, ry6), LEFT)
assert dock.gcps[-1].uid is None and dock.gcps[-1].name == ""

# --- 4. ochrona przed dublowaniem
dock.choose_library_point(lb.RefPoint("P0", rx, ry, uid=uid["P0"]))
assert dock._lib_target is None and "#2" in dock.lbl_status.text(), dock.lbl_status.text()
iface.canvas.setExtent(QgsRectangle(rx - 3000, ry - 3000, rx + 3000, ry + 3000))
dock.on_old_canvas_clicked(QgsPointXY(*PIX["P1"]), LEFT)
dock.on_ref_canvas_clicked(QgsPointXY(rx, ry), LEFT)            # przyciąga do P0, który już jest
assert len(dock.gcps) == 3 and "#2" in dock.lbl_status.text()

# --- 5. „Następny z zestawu” po obliczeniu transformacji
panel.cmb_set.setCurrentIndex(panel.cmb_set.findData(next(s[0] for s in lb.Library.open(path).sets())))
dock.btn_capture.setChecked(False)
dock.gcps_changed()
dock._recalc_timer.stop()
dock.run_analysis()
assert dock.analysis is not None, dock.lbl_status.text()
dock.next_library_point()
t = dock._lib_target
assert t is not None and t.name in ("P1", "P2", "P3", "P4", "P5"), t
assert len(dock._lib_markers) == 2, "brak przewidywanego położenia na skanie"
pred = dock._lib_markers[1][1].center()
assert math.dist((pred.x(), pred.y()), PIX[t.name]) < 30, (pred.x(), pred.y(), PIX[t.name])
assert "Brakuje jeszcze 5" in dock.lbl_status.text(), dock.lbl_status.text()
print("next:", dock.lbl_status.text())
first = t.name
dock.next_library_point()                                         # pominięcie
assert dock._lib_target.name != first
done = []
while dock._lib_target is not None:
    name = dock._lib_target.name
    dock.on_old_canvas_clicked(QgsPointXY(*PIX[name]), LEFT)
    done.append(name)
    dock.next_library_point()
assert sorted(done) == ["P1", "P2", "P3", "P4", "P5"], done
assert "Wszystkie punkty zestawu" in dock.lbl_status.text(), dock.lbl_status.text()
used = lp.QgsExpressionContextUtils.layerScope(panel.layer).variable(lp.USED_VAR).split("|")
assert len(used) == 7, used

# --- 6. bez zestawu „Następny” prosi o wybór
panel.cmb_set.setCurrentIndex(0)
dock.next_library_point()
assert "zestaw" in dock.lbl_status.text().lower()

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().remove(lp.SETTINGS_PATH)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY PICK OK")
