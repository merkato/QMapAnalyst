"""
Mapy i arkusze w bibliotece punktów (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_maps_qgis.py
Zapis analizy z wyborem mapy z listy, okno „Mapy i arkusze” (nowa mapa, nowy arkusz z bieżących punktów,
edycja opisu mapy dla wszystkich arkuszy, przeniesienie arkusza, usuwanie, wczytanie), macierz według mapy.
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

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog, QInputDialog, QMainWindow, QMessageBox  # noqa: E402
from osgeo import gdal  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lb = __import__(f"{PKG}.library", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
md = __import__(f"{PKG}.maps_dialog", fromlist=["*"])
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


QUESTIONS = []
QMessageBox.question = staticmethod(lambda *a, **k: (QUESTIONS.append(a[2]), QMessageBox.StandardButton.Yes)[1])
QInputDialog.getText = staticmethod(lambda *a, **k: ("16", True))

# zachowanie okien opisu w teście: (indeks mapy na liście albo "new", pola)
PLAN = {}


def analysis_exec(self):
    choice = PLAN.get("map", "new")
    self.cmb_map.setCurrentIndex(self.cmb_map.count() - 1 if choice == "new" else self.cmb_map.findData(choice))
    for attr, value in PLAN.get("fields", {}).items():
        w = getattr(self, attr)
        w.setText(value) if hasattr(w, "setText") else w.setValue(value)
    PLAN["seen_maps"] = [self.cmb_map.itemText(i) for i in range(self.cmb_map.count())]
    PLAN["initial"] = PLAN.get("initial")
    self.accept()
    return QDialog.DialogCode.Accepted


def map_exec(self):
    for attr, value in PLAN.get("map_fields", {}).items():
        w = getattr(self.fields, attr)
        w.setText(value) if hasattr(w, "setText") else w.setValue(value)
    self.accept()
    return QDialog.DialogCode.Accepted


md.AnalysisDialog.exec = analysis_exec
md.MapDialog.exec = map_exec
PATH_BEFORE = QgsSettings().value(lp.SETTINGS_PATH, "")
BY_BEFORE = QgsSettings().value(cd.SETTINGS_BY, cv.BY_SHEET)
NAMES_BEFORE = QgsSettings().value(dock_mod.SETTINGS_NAMES, False, type=bool)  # wczytanie arkusza z nazwami je włącza
TMP = tempfile.mkdtemp(prefix="qma_maps_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface = Iface()
iface.canvas.setDestinationCrs(crs)
project = QgsProject.instance()
project.setCrs(crs)

raster = os.path.join(TMP, "zannoni_14.tif")
ds = gdal.GetDriverByName("GTiff").Create(raster, 400, 300, 1, gdal.GDT_Byte)
ds.GetRasterBand(1).Fill(200)
ds = None
dock = dock_mod.QMapAnalystDockWidget(iface)
dock.set_old_raster(QgsRasterLayer(raster, "Zannoni"))
panel = dock.library_panel
path = os.path.join(TMP, "lib.gpkg")
assert panel.create_library(path, crs)
with lb.Library.open(path) as lib:
    pts = [lb.RefPoint(f"P{i}", 600000 + 1000 * i, 300000 + 700 * (i % 3)) for i in range(6)]
    lib.add_points(pts)
    uid = {p.name: p.uid for p in pts}
    sid = lib.add_set("S", uids=list(uid.values()))
panel.refresh_layer()
panel.reload_sets(select=sid)


def put_points(names):
    dock.clear_all_gcp()
    dock.library_analysis = None
    for k, n in enumerate(names):
        p = next(q for q in pts if q.name == n)
        dock.add_gcp(10 + 50 * k, -10 - 30 * (k % 3), p.x, p.y, name=n, uid=p.uid)
    dock.gcps_changed()


# --- 1. pierwszy zapis – nowa mapa z opisu
put_points(["P0", "P1", "P2"])
PLAN.update({"map": "new", "fields": {"ed_title": "Zannoni", "sp_from": 1772, "ed_sheet": "14"}})
assert dock.save_library_analysis()
a14 = dock.library_analysis
assert a14.map_id is not None and a14.sheet == "14" and PLAN["seen_maps"] == ["Nowa mapa…"]
assert "arkusz 14" in panel.lbl_analysis.text()
zannoni = a14.map_id

# --- 2. drugi skan: mapa wybrana z listy (podpowiedź po tytule warstwy), własny arkusz
put_points(["P2", "P3", "P4"])
dock.old_raster_layer.setName("zannoni")
dlg = md.AnalysisDialog(dock._new_library_analysis(), dock, dock._library_maps())
assert dlg.cmb_map.currentData() == zannoni                                        # wybór wstępny wg tytułu
assert dlg.ed_title.text() == "Zannoni" and dlg.sp_from.value() == 1772             # opis z mapy, nie z warstwy
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)  # ten sam skan – nowa analiza
PLAN.update({"map": zannoni, "fields": {"ed_sheet": "15"}})
assert dock.save_library_analysis()
a15 = dock.library_analysis
assert a15.map_id == zannoni and a15.analysis_id != a14.analysis_id and a15.title == "Zannoni"
QMessageBox.question = staticmethod(lambda *a, **k: (QUESTIONS.append(a[2]), QMessageBox.StandardButton.Yes)[1])

# --- 3. okno Mapy i arkusze
dlg = md.MapsDialog(panel, dock)
tree = dlg.tree
assert tree.topLevelItemCount() == 1 and tree.topLevelItem(0).childCount() == 2
assert tree.topLevelItem(0).text(0) == "Zannoni (1772)" and tree.topLevelItem(0).text(2) == "6"
assert dlg.selected() == ("sheet", a15.analysis_id) and tree.currentItem().font(0).bold()
# edycja opisu mapy – dotyczy obu arkuszy i otwartej analizy
tree.setCurrentItem(tree.topLevelItem(0))
PLAN["map_fields"] = {"ed_title": "Carte de la Pologne", "sp_to": 1772}
dlg.edit()
with lb.Library.open(path) as lib:
    assert {a.title for a in lib.analyses(zannoni)} == {"Carte de la Pologne"}
assert dock.library_analysis.title == "Carte de la Pologne" and "Carte de la Pologne" in panel.lbl_analysis.text()
# nowa mapa i nowy arkusz z bieżących punktów
PLAN["map_fields"] = {"ed_title": "TKKP Iłża", "sp_from": 1960, "sp_scale": 25000}
dlg.new_map()
tkkp = dlg.selected()[1]
assert dlg.selected()[0] == "map" and tree.topLevelItemCount() == 2
put_points(["P0", "P5"])
dlg._update_buttons()
assert dlg.btn_new_sheet.isEnabled()
dlg.new_sheet()                                                     # arkusz „16” (QInputDialog)
a16 = dock.library_analysis
assert a16.map_id == tkkp and a16.sheet == "16" and a16.title == "TKKP Iłża" and dlg.selected() == ("sheet", a16.analysis_id)
assert not dock.analysis_dirty()
# przeniesienie arkusza 15 do mapy TKKP (okno opisu arkusza)
it15 = next(tree.topLevelItem(i).child(j) for i in range(tree.topLevelItemCount())
            for j in range(tree.topLevelItem(i).childCount())
            if tree.topLevelItem(i).child(j).data(0, 256) == ("sheet", a15.analysis_id))
tree.setCurrentItem(it15)
PLAN.update({"map": tkkp, "fields": {}})
dlg.edit()
with lb.Library.open(path) as lib:
    moved = lib.get_analysis(a15.analysis_id)
    assert moved.map_id == tkkp and moved.title == "TKKP Iłża" and moved.sheet == "15"

# --- 4. macierz według mapy: arkusze mapy razem
cov_dlg = cd.CoverageDialog(panel)
cov_dlg.cmb_by.setCurrentIndex(cov_dlg.cmb_by.findData(cv.BY_SHEET))
assert cov_dlg.table.columnCount() == 3 + 1                         # trzy arkusze + Σ
cov_dlg.cmb_by.setCurrentIndex(cov_dlg.cmb_by.findData(cv.BY_MAP))
cov = cov_dlg.coverage
assert [c.key for c in cov.columns] == [zannoni, tkkp], [c.key for c in cov.columns]
assert cov.cell(uid["P4"], tkkp) == "sure" and cov.cell(uid["P5"], tkkp) == "sure" and cov.cell(uid["P4"], zannoni) is None
assert cov_dlg.table.horizontalHeaderItem(1).text().startswith("TKKP Iłża")
QInputDialog.getItem = staticmethod(lambda parent, title, label, items, *a, **k: (items[0], True))
cov_dlg.on_cell_double_clicked(0, 1)                                # TKKP: arkusz 16 otwarty – zostaje 15
assert cov_dlg.chosen_analysis == a15.analysis_id

# --- 5. wczytanie i usuwanie
dlg.reload(("sheet", a14.analysis_id))
assert dlg.selected() == ("sheet", a14.analysis_id)
dlg.open_sheet()
assert dock.library_analysis.analysis_id == a14.analysis_id and [g.name for g in dock.gcps] == ["P0", "P1", "P2"]
dlg = md.MapsDialog(panel, dock)
dlg.reload(("sheet", a14.analysis_id))
dlg.delete()                                                        # arkusz otwarty – punkty zostają, niezapisane
assert dock.library_analysis is None and len(dock.gcps) == 3 and dock.analysis_dirty()
assert "Usunąć arkusz" in QUESTIONS[-1]
dlg.reload(("map", tkkp))
dlg.delete()
assert "arkuszami (2)" in QUESTIONS[-1], QUESTIONS[-1]
with lb.Library.open(path) as lib:
    assert [m.title for m in lib.maps()] == ["Carte de la Pologne"] and lib.analyses() == []

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().setValue(lp.SETTINGS_PATH, PATH_BEFORE)
QgsSettings().setValue(cd.SETTINGS_BY, BY_BEFORE)
QgsSettings().setValue(dock_mod.SETTINGS_NAMES, NAMES_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY MAPS OK")
