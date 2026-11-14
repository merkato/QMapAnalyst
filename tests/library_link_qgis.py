"""
Jednoznaczne powiązanie punktów analizy z biblioteką (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_link_qgis.py
Ikony powiązania w tabeli GCP, propozycja punktu biblioteki przy zwykłym kliknięciu, „Powiąż”,
wiersze „brak na mapie”, niezapisane zmiany i stan bieżący w macierzy pokrycia,
„Następny z zestawu” nie łączy się z wcześniejszym kliknięciem na skanie.
"""
import faulthandler; faulthandler.enable()  # noqa: E702
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
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QInputDialog, QMainWindow, QMessageBox  # noqa: E402
from osgeo import gdal  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lb = __import__(f"{PKG}.library", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
cd = __import__(f"{PKG}.coverage_dialog", fromlist=["*"])
cv = __import__(f"{PKG}.coverage", fromlist=["*"])


class Iface:
    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas(self.win)
        self.win.setCentralWidget(self.canvas)

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.win


ANSWER = [QMessageBox.StandardButton.Yes]
QUESTIONS = []


def question(*a, **k):
    QUESTIONS.append(a[2])
    return ANSWER[0]


def dialog_exec(self):
    self.ed_title.setText("Mapa testowa")
    self.accept()
    return QDialog.DialogCode.Accepted


QMessageBox.question = staticmethod(question)
QInputDialog.getText = staticmethod(lambda *a, **k: ("pominięty", True))
lp.AnalysisDialog.exec = dialog_exec
PATH_BEFORE = QgsSettings().value(lp.SETTINGS_PATH, "")
NAMES_BEFORE = QgsSettings().value(dock_mod.SETTINGS_NAMES, False, type=bool)
TMP = tempfile.mkdtemp(prefix="qma_link_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface = Iface()
iface.canvas.resize(800, 600)
iface.canvas.setDestinationCrs(crs)
project = QgsProject.instance()
project.setCrs(crs)
LEFT = Qt.MouseButton.LeftButton


def to_ref(px, py):
    return 600000 + 50 * px, 300000 + 50 * py


raster = os.path.join(TMP, "scan.tif")
ds = gdal.GetDriverByName("GTiff").Create(raster, 2000, 1600, 1, gdal.GDT_Byte)
ds.GetRasterBand(1).Fill(200)
ds = None
PIX = {f"P{i}": (200 + 200 * i, -(300 + 250 * (i % 4))) for i in range(8)}
dock = dock_mod.QMapAnalystDockWidget(iface)
dock.chk_names.setChecked(False)
dock.set_old_raster(QgsRasterLayer(raster, "scan"))
panel = dock.library_panel
path = os.path.join(TMP, "lib.gpkg")
assert panel.create_library(path, crs)
with lb.Library.open(path) as lib:
    pts = [lb.RefPoint(n, *to_ref(*xy)) for n, xy in PIX.items()]
    lib.add_points(pts)
    uid = {p.name: p.uid for p in pts}
    sid = lib.add_set("S", uids=list(uid.values()))
panel.refresh_layer()
panel.reload_sets(select=sid)
assert panel.current_set() == sid


def zoom_to(name, half=3000):
    x, y = to_ref(*PIX[name])
    iface.canvas.setExtent(QgsRectangle(x - half, y - half, x + half, y + half))
    iface.canvas.refresh()
    return x, y, iface.canvas.mapUnitsPerPixel()


def header(r):
    return dock.tbl_gcp.verticalHeaderItem(r)


# --- 1. para z biblioteki („Następny z zestawu”) – ikona powiązania
dock.choose_library_point(lb.RefPoint("P0", *to_ref(*PIX["P0"]), uid=uid["P0"]))
dock.on_old_canvas_clicked(QgsPointXY(*PIX["P0"]), LEFT)
assert dock.gcps[0].uid == uid["P0"]
assert "Punkt biblioteki: P0" in header(0).toolTip() and not header(0).icon().isNull()

# --- 2. zwykłe kliknięcie ~25 px od P1 – propozycja punktu biblioteki (Tak)
x1, y1, upp = zoom_to("P1")
dock.on_old_canvas_clicked(QgsPointXY(*PIX["P1"]), LEFT)
dock.on_ref_canvas_clicked(QgsPointXY(x1 + 20 * upp, y1 + 15 * upp), LEFT)
g = dock.gcps[1]
assert g.uid == uid["P1"] and (g.ref_x, g.ref_y) == (x1, y1) and g.name == "P1", (g.uid, g.ref_x - x1)
assert "25 px" in QUESTIONS[-1] and "P1" in QUESTIONS[-1], QUESTIONS[-1]

# … i odmowa (Nie) – punkt spoza biblioteki w miejscu kliknięcia
x2, y2, upp = zoom_to("P2", 300)
ANSWER[0] = QMessageBox.StandardButton.No
dock.on_old_canvas_clicked(QgsPointXY(*PIX["P2"]), LEFT)
assert 15 * upp < 500, upp
dock.on_ref_canvas_clicked(QgsPointXY(x2 + 15 * upp, y2), LEFT)
ANSWER[0] = QMessageBox.StandardButton.Yes
assert dock.gcps[2].uid is None
assert "spoza biblioteki" in header(2).toolTip()
assert dock.btn_gcp_link.isEnabled()

# --- 3. „Powiąż”: nazwa do 5 km, dowolny do 500 m; reszta bez dopasowania
x3, y3 = to_ref(*PIX["P3"])
x4, y4 = to_ref(*PIX["P4"])
x5, y5 = to_ref(*PIX["P5"])
dock.add_gcp(*PIX["P3"], x3 + 1200, y3 - 900, name="p3")          # ta sama nazwa (wielkość liter bez znaczenia), 1,5 km
dock.add_gcp(*PIX["P4"], x4 + 150, y4 + 200)                      # bez nazwy, 250 m
dock.add_gcp(*PIX["P5"], x5 + 3000, y5)                           # bez nazwy, 3 km – bez dopasowania
dock.gcps_changed()
dock.tbl_gcp.clearSelection()
dock.link_selected_to_library()
q = QUESTIONS[-1]
assert "(liczba: 3)" in q and "→ P3 (1500 m, ta sama nazwa)" in q and "→ P4 (250 m)" in q and "→ P2" in q, q
assert "Bez dopasowania: 1" in q, q
g3, g4, g5 = dock.gcps[3:6]
assert g3.uid == uid["P3"] and (g3.ref_x, g3.ref_y) == (x3, y3) and g3.name == "p3"
assert g4.uid == uid["P4"] and g4.name == "P4" and dock.gcps[2].uid == uid["P2"] and g5.uid is None
assert "różni się" not in header(3).toolTip()                      # p3 = P3 po normalizacji

# --- 4. „brak na mapie” jako wiersz tabeli
dock.choose_library_point(lb.RefPoint("P6", *to_ref(*PIX["P6"]), uid=uid["P6"]))
dock.mark_target_absent()                                         # potem od razu „Następny z zestawu”
assert uid["P6"] in dock.absent
n = len(dock.gcps)
t = dock.tbl_gcp
assert t.rowCount() == n + 1 and header(n).text() == "○"
assert t.item(n, dock_mod.COL_STATUS).text() == "brak na mapie" and t.columnSpan(n, dock_mod.COL_OX) > 1
assert t.item(n, dock_mod.COL_NOTE).text() == "pominięty"
t.item(n, dock_mod.COL_NOTE).setText("nie ma na arkuszu")
assert dock.absent[uid["P6"]] == ("P6", "nie ma na arkuszu")
assert dock.selected_rows() == [] or all(r < n for r in dock.selected_rows())
# dwuklik w numer wiersza – wskazanie punktu mimo wszystko
dock._clear_lib_target()
dock.zoom_to_gcp(n)
assert dock._lib_target is not None and dock._lib_target.uid == uid["P6"]
dock.on_old_canvas_clicked(QgsPointXY(*PIX["P6"]), LEFT)
assert uid["P6"] not in dock.absent and dock.gcps[-1].uid == uid["P6"] and t.rowCount() == len(dock.gcps)
# usunięcie wiersza „brak na mapie”
dock.choose_library_point(lb.RefPoint("P7", *to_ref(*PIX["P7"]), uid=uid["P7"]))
dock.mark_target_absent()
t.selectRow(len(dock.gcps))
assert dock.selected_rows() == [] and dock.selected_absent() == [uid["P7"]]
n_before = len(dock.gcps)
dock.remove_selected_gcp()
assert not dock.absent and len(dock.gcps) == n_before

# --- 5. „Następny z zestawu” po kliknięciu na skanie: kliknięcie odrzucone, nie powstaje para
dock._clear_lib_target()
dock.on_old_canvas_clicked(QgsPointXY(10, -10), LEFT)              # brak celu – zwykły krok 1/2
assert dock._temp_old_point is not None
n_before = len(dock.gcps)
dock.next_library_point()
assert dock._temp_old_point is None and len(dock.gcps) == n_before and dock._lib_target is not None

# --- 6. niezapisane zmiany i stan bieżący w macierzy
dock._clear_lib_target()
assert dock.analysis_dirty() and dock.btn_lib_save.text().endswith("*")
cov_dlg = cd.CoverageDialog(panel)
cur = cov_dlg.coverage.current_id
assert cur == cv.CURRENT and cov_dlg.table.horizontalHeaderItem(0).text().endswith("*")
assert dock.save_library_analysis()
assert not dock.analysis_dirty() and not dock.btn_lib_save.text().endswith("*")
assert "niezapisane" not in panel.lbl_analysis.text()
aid = dock.library_analysis.analysis_id
dock.choose_library_point(lb.RefPoint("P7", *to_ref(*PIX["P7"]), uid=uid["P7"]))
dock.mark_target_absent()
assert dock.analysis_dirty() and "niezapisane" in panel.lbl_analysis.text()
cov_dlg = cd.CoverageDialog(panel)
cov = cov_dlg.coverage
assert cov.current_id == aid and [a.analysis_id for a in cov.analyses] == [aid]
assert cov.cell(uid["P7"], aid) == "absent" and cov.cell(uid["P0"], aid) == "sure"
assert cov_dlg.table.horizontalHeaderItem(0).text() == "Mapa testowa *"
with lb.Library.open(path) as lib:
    assert all(ln.uid != uid["P7"] for ln in lib.links(aid))       # plik ma ostatni zapis
cov_dlg.on_cell_double_clicked(0, 0)                                # otwarta analiza – bez ponownego wczytania
assert cov_dlg.chosen_analysis is None and "otwarta" in cov_dlg.lbl_legend.text()

# --- 7. wczytanie analizy przy niezapisanych zmianach: Anuluj / Nie (porzuć)
n_gcps = len(dock.gcps)
ANSWER[0] = QMessageBox.StandardButton.Cancel
assert not dock.open_library_analysis(aid)
assert uid["P7"] in dock.absent
ANSWER[0] = QMessageBox.StandardButton.No
assert dock.open_library_analysis(aid)
assert "niezapisane zmiany" in QUESTIONS[-1] or "Zapisać" in QUESTIONS[-2], QUESTIONS[-2:]
ANSWER[0] = QMessageBox.StandardButton.Yes
assert not dock.absent and len(dock.gcps) == n_gcps and not dock.analysis_dirty()

# --- 8. plik .points: powiązania z biblioteką, statusy, uwagi i „brak na mapie” w pliku towarzyszącym
dock.gcps[1].status, dock.gcps[1].comment = "uncertain", "zatarte"
dock.choose_library_point(lb.RefPoint("P7", *to_ref(*PIX["P7"]), uid=uid["P7"]))
dock.mark_target_absent()
dock._clear_lib_target()
before = [(g.name, g.uid, g.status, g.comment) for g in dock.gcps]
pts_path = os.path.join(TMP, "scan.points")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (pts_path, ""))
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (pts_path, ""))
dock.save_gcp_to_file()
assert "uid" in open(pts_path + ".names.csv", encoding="utf-8").readline()
assert "uid" not in open(pts_path, encoding="utf-8").read()          # .points – czysty format Georeferencera
dock.clear_all_gcp()
dock.library_analysis = None
dock.gcps_changed()
dock.load_gcp_from_file()
assert [(g.name, g.uid, g.status, g.comment) for g in dock.gcps] == before, [(g.name, g.uid) for g in dock.gcps]
assert dock.absent == {uid["P7"]: ("P7", "pominięty")}
assert "Powiązane z biblioteką" in dock.lbl_status.text(), dock.lbl_status.text()
# dopisanie tego samego pliku: powiązania już użyte nie są dublowane
ANSWER[0] = QMessageBox.StandardButton.No
n = len(dock.gcps)
dock.load_gcp_from_file()
ANSWER[0] = QMessageBox.StandardButton.Yes
assert len(dock.gcps) == 2 * n and all(g.uid is None for g in dock.gcps[n:])
assert "pominięto" in dock.lbl_status.text()

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().setValue(lp.SETTINGS_PATH, PATH_BEFORE)
QgsSettings().setValue(dock_mod.SETTINGS_NAMES, NAMES_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY LINK OK")
