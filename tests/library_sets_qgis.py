"""
Obsługa zestawów punktów biblioteki (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_sets_qgis.py
Zestaw roboczy bez filtrowania mapy (punkty spoza zestawu przygaszone, można je zaznaczyć i dodać),
„Tylko punkty zestawu”, lista zestawów w zakładce GCP, zestaw z punktów analizy, okno punktów zestawu.
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
    QgsApplication, QgsCoordinateReferenceSystem, QgsExpressionContextUtils, QgsProject, QgsSettings, QgsSymbol,
)

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QInputDialog, QMainWindow, QMessageBox  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lb = __import__(f"{PKG}.library", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
sd = __import__(f"{PKG}.sets_dialog", fromlist=["*"])


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
QInputDialog.getText = staticmethod(lambda *a, **k: ("Z analizy", True))
PATH_BEFORE = QgsSettings().value(lp.SETTINGS_PATH, "")
SET_ONLY_BEFORE = QgsSettings().value(lp.SETTINGS_SET_ONLY, False, type=bool)
TMP = tempfile.mkdtemp(prefix="qma_sets_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface = Iface()
iface.canvas.setDestinationCrs(crs)
project = QgsProject.instance()
project.setCrs(crs)

dock = dock_mod.QMapAnalystDockWidget(iface)
panel = dock.library_panel
panel.chk_set_only.setChecked(False)
path = os.path.join(TMP, "lib.gpkg")
assert panel.create_library(path, crs)
with lb.Library.open(path) as lib:
    pts = [lb.RefPoint(n, 600000 + 1000 * i, 300000, alt_names="Ilsa" if n == "Iłża" else "", type="town")
           for i, n in enumerate(["Iłża", "Wąchock", "Kielce", "Łagów", "Opatów"])]
    lib.add_points(pts)
    uid = {p.name: p.uid for p in pts}
    sid = lib.add_set("K", uids=[uid["Iłża"], uid["Wąchock"]])
panel.refresh_layer()
panel.reload_sets()
layer = panel.layer
assert layer is not None

# --- styl: przygaszanie punktów spoza zestawu
cats = layer.renderer().categories()                               # lista musi żyć dłużej niż symbol
cat_sym = cats[0].symbol()
assert cat_sym.dataDefinedProperties().property(lp._enum(QgsSymbol, "Property", "Opacity")).isActive()
assert layer.customProperty(lp.STYLE_PROP) == lp.STYLE_VERSION

# --- 1. zestaw roboczy bez filtrowania: wszystkie punkty widoczne, członkowie w zmiennej warstwy
panel.cmb_set.setCurrentIndex(panel.cmb_set.findData(sid))
assert layer.subsetString() == "" and layer.featureCount() == 5
var = QgsExpressionContextUtils.layerScope(layer).variable(lp.SET_VAR)
assert set(var.split("|")) == {uid["Iłża"], uid["Wąchock"]}, var
# lista w zakładce GCP podąża za zakładką Biblioteka
assert dock.cmb_lib_set.count() == panel.cmb_set.count() and dock.cmb_lib_set.currentData() == sid
# punkty spoza zestawu można zaznaczyć i dodać – bez przełączania na „Wszystkie punkty”
layer.selectByExpression("\"name\" IN ('Kielce', 'Łagów')")
panel.add_selected_to_set()
with lb.Library.open(path) as lib:
    assert sorted(p.name for p in lib.points(sid)) == ["Iłża", "Kielce", "Wąchock", "Łagów"]
assert "(4)" in dock.cmb_lib_set.currentText(), dock.cmb_lib_set.currentText()

# --- 2. „Tylko punkty zestawu” – filtr mapy; podpowiedź przy pustym zaznaczeniu
panel.chk_set_only.setChecked(True)
assert layer.featureCount() == 4 and QgsExpressionContextUtils.layerScope(layer).variable(lp.SET_VAR) == ""
layer.removeSelection()
msgs = []
panel.status = lambda text, level="info": msgs.append(text)
panel.add_selected_to_set()
assert "Tylko punkty zestawu" in msgs[-1], msgs
panel.chk_set_only.setChecked(False)
assert layer.featureCount() == 5

# --- 3. wybór zestawu w zakładce GCP zmienia zestaw roboczy
dock.on_lib_set_chosen(0)
assert panel.current_set() is None and QgsExpressionContextUtils.layerScope(layer).variable(lp.SET_VAR) == ""
dock.on_lib_set_chosen(dock.cmb_lib_set.findData(sid))
assert panel.current_set() == sid

# --- 4. zestaw z punktów analizy (z „brak na mapie”)
dock.add_gcp(10, -10, pts[0].x, pts[0].y, name="Iłża", uid=uid["Iłża"])
dock.add_gcp(20, -10, pts[4].x, pts[4].y, name="Opatów", uid=uid["Opatów"])
dock.add_gcp(30, -10, 1, 1, name="spoza")
dock.absent[uid["Łagów"]] = ("Łagów", "")
dock.gcps_changed()
new_sid = panel.new_set_from_analysis()
with lb.Library.open(path) as lib:
    assert sorted(p.name for p in lib.points(new_sid)) == ["Iłża", "Opatów", "Łagów"]
assert panel.current_set() == new_sid and dock.cmb_lib_set.currentData() == new_sid

# --- 5. okno punktów zestawu: lista, pokazanie na mapie, usuwanie z zestawu
dlg = sd.SetPointsDialog(panel)
assert dlg.table.rowCount() == 3 and dlg.table.item(0, 0).text() == "Iłża" and dlg.table.item(0, 1).text() == "Ilsa"
assert dlg.table.item(0, 3).text() == "✓" and "w bieżącej analizie 2" in dlg.lbl_title.text(), dlg.lbl_title.text()
dlg.zoom_to(0)
assert [f["name"] for f in layer.selectedFeatures()] == ["Iłża"]
assert [dlg.table.item(r, 0).text() for r in range(3)] == ["Iłża", "Łagów", "Opatów"]   # bez znaczenia ogonków
dlg.table.selectRow(1)
dlg.remove_selected()
assert dlg.table.rowCount() == 2
with lb.Library.open(path) as lib:
    assert sorted(p.name for p in lib.points(new_sid)) == ["Iłża", "Opatów"]
    assert len(lib.points()) == 5                                   # punkty zostają w bibliotece

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().setValue(lp.SETTINGS_PATH, PATH_BEFORE)
QgsSettings().setValue(lp.SETTINGS_SET_ONLY, SET_ONLY_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY SETS OK")
