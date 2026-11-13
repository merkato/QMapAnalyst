"""
Analiza mapy w bibliotece (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_analysis_qgis.py
Status i uwagi punktów, „brak na mapie”, zapis i odczyt analizy, opis mapy, wykrywanie punktów
poprawionych lub usuniętych w bibliotece.
"""
import faulthandler; faulthandler.enable()  # noqa: E702
import math
import os

import numpy as np
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsPointXY, QgsProject, QgsRasterLayer, QgsSettings,
)

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog, QInputDialog, QMainWindow, QMessageBox  # noqa: E402
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


questions = []


def question(*a, **k):
    questions.append(a[2])
    return QMessageBox.StandardButton.Yes


QMessageBox.question = staticmethod(question)
QInputDialog.getText = staticmethod(lambda *a, **k: ("pominięty przez kartografa", True))


def dialog_exec(self):
    if not self.ed_title.text().startswith("Zannoni"):
        self.ed_title.setText("Zannoni ark. 14")
    self.sp_from.setValue(1772)
    self.sp_scale.setValue(280000)
    self.ed_sheet.setText("14")
    self.accept()
    return QDialog.DialogCode.Accepted


lp.AnalysisDialog.exec = dialog_exec

QgsSettings().remove(lp.SETTINGS_PATH)
NAMES_BEFORE = QgsSettings().value(dock_mod.SETTINGS_NAMES, False, type=bool)  # przywracane na końcu
TMP = tempfile.mkdtemp(prefix="qma_an_")
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


raster = os.path.join(TMP, "zannoni_14.tif")
ds = gdal.GetDriverByName("GTiff").Create(raster, 2000, 1600, 1, gdal.GDT_Byte)
ds.GetRasterBand(1).Fill(200)
ds = None
PIX = {f"P{i}": (200 + 200 * i, -(300 + 250 * (i % 4))) for i in range(6)}
PIX["Łagów"] = (1500, -1400)
dock = dock_mod.QMapAnalystDockWidget(iface)
dock.set_old_raster(QgsRasterLayer(raster, "zannoni_14"))
panel = dock.library_panel
path = os.path.join(TMP, "lib.gpkg")
assert panel.create_library(path, crs)
with lb.Library.open(path) as lib:
    pts = [lb.RefPoint(n, *to_ref(*xy), type="village") for n, xy in PIX.items()]
    lib.add_points(pts)
    uid = {p.name: p.uid for p in pts}
    sid = lib.add_set("S", uids=list(uid.values()))
panel.refresh_layer()
panel.reload_sets(select=sid)
assert "nie zapisana" in panel.lbl_analysis.text()


def pick(name):
    dock.choose_library_point(next(p for p in panel.set_points() if p.name == name))
    dock.on_old_canvas_clicked(QgsPointXY(*PIX[name]), LEFT)


for n in ("P0", "P1", "P2", "P3"):
    pick(n)
dock.btn_capture.setChecked(False)
dock.add_gcp(1900, -1500, *to_ref(1900, -1500), name="kopiec")          # spoza biblioteki
dock.gcps_changed()
assert [g.uid is not None for g in dock.gcps] == [True] * 4 + [False]

# --- brak na mapie
dock.choose_library_point(next(p for p in panel.set_points() if p.name == "Łagów"))
assert dock.btn_lib_absent.isEnabled()
dock.mark_target_absent()
assert dock.absent == {uid["Łagów"]: ("Łagów", "pominięty przez kartografa")}
assert "(1)" in dock.btn_lib_absent.text() and "Łagów" in dock.btn_lib_absent.toolTip()
t = dock._lib_target                       # „Następny” pomija punkty nieobecne
assert t is not None and t.name in ("P4", "P5"), t
dock.btn_capture.setChecked(False)

# --- status i uwagi w tabeli
it = dock.tbl_gcp.item(2, dock_mod.COL_STATUS)
assert it.text() == "pewny"
it.setData(Qt.ItemDataRole.UserRole, "illegible")
it.setText("nieczytelny")
qgs.processEvents()
assert dock.gcps[2].status == "illegible" and not dock.gcps[2].active
dock.tbl_gcp.item(1, dock_mod.COL_STATUS).setData(Qt.ItemDataRole.UserRole, "uncertain")
qgs.processEvents()
dock.tbl_gcp.item(1, dock_mod.COL_NOTE).setText("sygnatura zatarta")
assert dock.gcps[1].status == "uncertain" and dock.gcps[1].active and dock.gcps[1].comment == "sygnatura zatarta"
_, _, idx = dock.active_arrays()
assert idx == [0, 1, 3, 4], idx
assert dock.tbl_gcp.item(2, dock_mod.COL_ERR).text() == "nieczytelny"

# --- wagi a priori: status „niepewny” i niepewność współrzędnych z biblioteki
dock.chk_auto.setChecked(False)
dock.run_analysis()
assert dock.analysis.fit.weights is None
dock.chk_weights.setChecked(True)
dock.run_analysis()
w = dock.analysis.fit.weights
assert w is not None and abs(w[1] - 0.25) < 1e-9 and np.allclose(w[[0, 2, 3]], 1.0), w   # aktywne: P0, P1, P3, kopiec
assert "0,25" in dock.tbl_gcp.item(1, dock_mod.COL_W).toolTip() or "0.25" in dock.tbl_gcp.item(1, dock_mod.COL_W).toolTip()
assert "wagi a priori" in dock.lbl_status.text()
with lb.Library.open(path) as lib:
    p0 = lib.get_point(uid["P0"])
    p0.sigma_m = 500.0
    lib.update_point(p0)
panel.invalidate_cache()
dock.run_analysis()
w = dock.analysis.fit.weights
assert w[0] < 0.01 and abs(w[1] - 0.25) < 1e-9, w
rows = dict(dock.report_meta_rows())
assert rows["Wagi a priori"].startswith("tak"), rows
dock.chk_weights.setChecked(False)
dock.run_analysis()
assert dock.analysis.fit.weights is None and dict(dock.report_meta_rows())["Wagi a priori"].startswith("nie")
with lb.Library.open(path) as lib:
    p0 = lib.get_point(uid["P0"])
    p0.sigma_m = None
    lib.update_point(p0)
panel.invalidate_cache()

# --- zapis analizy
assert dock.save_library_analysis()
a = dock.library_analysis
assert a.analysis_id is not None and a.title == "Zannoni ark. 14" and a.year_from == 1772 and a.scan_sha256
assert "Zannoni ark. 14" in panel.lbl_analysis.text() and "1772" in panel.lbl_analysis.text()
with lb.Library.open(path) as lib:
    links = lib.links(a.analysis_id)
    assert [ln.status for ln in links] == ["sure", "uncertain", "illegible", "sure", "sure", "absent"], links
    assert links[4].uid is None and links[4].name == "kopiec"
    assert links[5].comment == "pominięty przez kartografa" and links[5].x_old is None
    assert lib.get_analysis(a.analysis_id).ref_crs
n_q = len(questions)
assert dock.save_library_analysis() and len(questions) == n_q        # ponowny zapis – bez pytań
with lb.Library.open(path) as lib:
    assert len(lib.analyses()) == 1

# --- zmiana punktu w bibliotece i usunięcie innego
with lb.Library.open(path) as lib:
    p1 = lib.get_point(uid["P1"])
    p1.x += 25.0
    lib.update_point(p1)
    lib.delete_point(uid["P2"], force=True)
panel.invalidate_cache()

# --- odczyt w „nowej sesji”
dock.clear_all_gcp()
dock.library_analysis = None
dock.gcps_changed()
questions.clear()
assert dock.open_library_analysis(a.analysis_id)
assert len(dock.gcps) == 5 and dock.absent == {uid["Łagów"]: ("Łagów", "pominięty przez kartografa")}
g1 = dock.gcps[1]
assert g1.status == "uncertain" and g1.comment == "sygnatura zatarta" and g1.uid == uid["P1"]
assert math.isclose(g1.ref_x, to_ref(*PIX["P1"])[0] + 25.0), "współrzędne nie zaktualizowane"
assert any("zmieniły położenie" in q for q in questions), questions
assert dock.gcps[2].uid is None and dock.gcps[2].name == "P2"       # usunięty z biblioteki
assert dock.gcps[2].status == "illegible"
assert dock.library_analysis.analysis_id == a.analysis_id
assert dock.chk_names.isChecked()

# --- zapis „od zera” tego samego skanu – pytanie o zastąpienie (Tak)
dock.library_analysis = None
questions.clear()
assert dock.save_library_analysis()
assert any("już analiza tego skanu" in q for q in questions), questions
assert dock.library_analysis.analysis_id == a.analysis_id
with lb.Library.open(path) as lib:
    assert len(lib.analyses()) == 1
    assert lib.links(a.analysis_id)[2].uid is None

# --- wybór analizy z listy
QInputDialog.getItem = staticmethod(lambda parent, title, label, items, *a, **k: (items[0], True))
dock.clear_all_gcp()
dock.gcps_changed()
assert dock.open_library_analysis()
assert len(dock.gcps) == 5

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().remove(lp.SETTINGS_PATH)
QgsSettings().setValue(dock_mod.SETTINGS_NAMES, NAMES_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY ANALYSIS OK")
