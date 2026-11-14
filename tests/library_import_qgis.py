"""
Import punktów do biblioteki (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_import_qgis.py
Warstwa wektorowa w innym układzie (dopasowanie pól jak w PRNG), tylko zaznaczone, plik CSV,
duplikaty, uzupełnianie nazw dawnych, nowy i istniejący zestaw.
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
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsPointXY,
    QgsProject, QgsSettings, QgsVectorLayer,
)

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lb = __import__(f"{PKG}.library", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
idlg = __import__(f"{PKG}.import_dialog", fromlist=["*"])


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
TMP = tempfile.mkdtemp(prefix="qma_imp_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
wgs = QgsCoordinateReferenceSystem("EPSG:4326")
project = QgsProject.instance()
project.setCrs(crs)
iface = Iface()
iface.canvas.setDestinationCrs(crs)
dock = dock_mod.QMapAnalystDockWidget(iface)
panel = dock.library_panel
path = os.path.join(TMP, "lib.gpkg")
assert panel.create_library(path, crs)
ILZA = (690000.0, 380000.0)
with lb.Library.open(path) as lib:
    uid_ilza = lib.add_point(lb.RefPoint("Iłża", *ILZA, type="town", alt_names="Ilsa"))
panel.refresh_layer()
panel.reload_sets()
assert panel.btn_import.isEnabled()

# --- warstwa „PRNG” w EPSG:4326
to_wgs = QgsCoordinateTransform(crs, wgs, project)
src = QgsVectorLayer("Point?crs=EPSG:4326&field=NAZWA_GLOWNA:string&field=RODZAJ_OBIEKTU:string"
                     "&field=NAZWA_HISTORYCZNA:string", "PRNG miejscowości", "memory")
rows = [
    ("Iłża", "miasto", "Ilża|Илжа", (ILZA[0] + 10, ILZA[1])),       # duplikat istniejącego (10 m)
    ("Kielce", "miasto", "", (640000.0, 355000.0)),
    ("Wąchock", "klasztor", "Wanchock", (660000.0, 372000.0)),
    ("Bez nazwy?", "osiedle", "", (650000.0, 360000.0)),               # nierozpoznany typ -> domyślny
    ("", "wieś", "", (651000.0, 361000.0)),                            # bez nazwy – pominięty
]
feats = []
for name, typ, alt, xy in rows:
    f = QgsFeature(src.fields())
    f.setAttributes([name or None, typ, alt or None])
    f.setGeometry(QgsGeometry.fromPointXY(to_wgs.transform(QgsPointXY(*xy))))
    feats.append(f)
src.dataProvider().addFeatures(feats)
project.addMapLayer(src)

dlg = idlg.ImportDialog(panel)
dlg.cmb_source.setCurrentIndex(dlg.cmb_source.findData(src.id()))
m = dlg.mapping()
assert m["name"] == "NAZWA_GLOWNA" and m["type"] == "RODZAJ_OBIEKTU" and m["alt_names"] == "NAZWA_HISTORYCZNA", m
assert dlg.default_source == "PRNG miejscowości"
assert "4" in dlg.lbl_summary.text() and "1" in dlg.lbl_summary.text(), dlg.lbl_summary.text()
assert not dlg.chk_selected.isEnabled()
dlg.cmb_default_type.setCurrentIndex(dlg.cmb_default_type.findData("other"))
dlg.cmb_set.setCurrentIndex(dlg.cmb_set.findData(idlg.NEW_SET))
dlg.ed_set.setText("PRNG-test")
dlg.accept()
assert dlg.result() == dlg.DialogCode.Accepted
assert dlg.result_stats == (3, 1, 0, 1), dlg.result_stats
with lb.Library.open(path) as lib:
    pts = {p.name: p for p in lib.points()}
    assert set(pts) == {"Iłża", "Kielce", "Wąchock", "Bez nazwy?"}, set(pts)
    assert abs(pts["Kielce"].x - 640000.0) < 0.01 and abs(pts["Kielce"].y - 355000.0) < 0.01
    assert pts["Wąchock"].type == "monastery" and pts["Wąchock"].alt_names == "Wanchock"
    assert pts["Bez nazwy?"].type == "other" and pts["Kielce"].source == "PRNG miejscowości"
    assert pts["Iłża"].uid == uid_ilza and pts["Iłża"].alt_names == "Ilsa|Илжа", pts["Iłża"].alt_names
    sets = {name: sid for sid, name, _d, _n in lib.sets()}
    assert set(lib.set_members(sets["PRNG-test"])) == {p.uid for p in pts.values()}   # także duplikat
assert panel.current_set() == sets["PRNG-test"]

# --- tylko zaznaczone, ponowny import = same duplikaty
src.selectByExpression("\"NAZWA_GLOWNA\" = 'Kielce'")
dlg = idlg.ImportDialog(panel)
dlg.cmb_source.setCurrentIndex(dlg.cmb_source.findData(src.id()))
assert dlg.chk_selected.isEnabled() and dlg.chk_selected.isChecked()
assert dlg.points()[0][0].name == "Kielce" and len(dlg.points()[0]) == 1
dlg.accept()
assert dlg.result_stats == (0, 1, 0, 0), dlg.result_stats

# --- CSV w EPSG:2180, przecinek dziesiętny, istniejący zestaw
csv_path = os.path.join(TMP, "punkty.csv")
with open(csv_path, "w", encoding="cp1250") as f:
    f.write("nazwa;x;y;typ;uwagi\n")
    f.write("Bodzentyn;655000,5;365000,25;miasto;zamek biskupi\n")
    f.write("Kościół w Kielcach;640030;355010;kościół;\n")            # 32 m od Kielc, inna nazwa
    f.write("Zły wiersz;abc;1;wieś;\n")
dlg = idlg.ImportDialog(panel)
dlg.load_csv(csv_path)
assert dlg.cmb_source.currentData() == idlg.CSV
assert dlg.cmb_x.currentData() == "x" and dlg.cmb_y.currentData() == "y"
dlg.ed_crs.setText("EPSG:2180")
pts_csv, skipped = dlg.points()
assert [p.name for p in pts_csv] == ["Bodzentyn", "Kościół w Kielcach"] and skipped == 0, (pts_csv, skipped)
assert pts_csv[0].x == 655000.5 and pts_csv[0].note == "zamek biskupi" and pts_csv[1].type == "church"
assert pts_csv[0].source == "punkty.csv"
dlg.cmb_set.setCurrentIndex(dlg.cmb_set.findData(sets["PRNG-test"]))
dlg.accept()
assert dlg.result_stats == (2, 0, 1, 0), dlg.result_stats
with lb.Library.open(path) as lib:
    assert len(lib.points()) == 6 and len(lib.set_members(sets["PRNG-test"])) == 6

# --- komunikat po imporcie (ścieżka przez przycisk)
idlg.ImportDialog.exec = lambda self: (self.cmb_source.setCurrentIndex(self.cmb_source.findData(src.id())),
                                       self.accept(), self.result())[-1]
src.removeSelection()
panel.import_dialog()
assert "Import:" in dock.lbl_status.text() and "4" in dock.lbl_status.text(), dock.lbl_status.text()

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().setValue(lp.SETTINGS_PATH, PATH_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY IMPORT OK")
