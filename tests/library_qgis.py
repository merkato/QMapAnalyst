"""
Zakładka Biblioteka w QGIS bez okna (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_qgis.py
Zakłada bibliotekę, dodaje warstwę na mapę główną, tworzy zestaw z zaznaczenia, filtruje,
wyróżnia punkty użyte w analizie, sprawdza formularz edycji (uid automatyczny) i usunięcie warstwy.
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
    QgsApplication, QgsCoordinateReferenceSystem, QgsExpressionContextUtils, QgsFeature, QgsGeometry, QgsPointXY,
    QgsExpressionContext, QgsProject, QgsSettings,
)

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox  # noqa: E402

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
TMP = tempfile.mkdtemp(prefix="qma_libui_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface = Iface()
iface.canvas.setDestinationCrs(crs)
project = QgsProject.instance()
project.setCrs(crs)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
warnings = []
QMessageBox.warning = staticmethod(lambda *a, **k: warnings.append(a[2]))

SET_ONLY_BEFORE = QgsSettings().value(lp.SETTINGS_SET_ONLY, False, type=bool)
dock = dock_mod.QMapAnalystDockWidget(iface)
panel = dock.library_panel
panel.chk_set_only.setChecked(True)        # ten test sprawdza filtrowanie mapy po zestawie
tabs = [dock.tab_widget.tabText(i) for i in range(dock.tab_widget.count())]
assert "Biblioteka" in tabs, tabs
assert panel.path is None and not panel.chk_layer.isEnabled()

# --- plik, który nie jest biblioteką
bad = os.path.join(TMP, "bad.gpkg")
open(bad, "w").write("x")
assert not panel.use_library(bad) and warnings, "brak ostrzeżenia dla złego pliku"

# --- nowa biblioteka z punktami
path = os.path.join(TMP, "biblioteka.gpkg")
assert panel.create_library(path, crs)
assert panel.layer is not None and panel.chk_layer.isChecked()
assert QgsSettings().value(lp.SETTINGS_PATH) == os.path.abspath(path)
with lb.Library.open(path) as lib:
    uids = lib.add_points([
        lb.RefPoint("Iłża", 690000.0, 380000.0, type="town", alt_names="Ilza"),
        lb.RefPoint("Kielce", 640000.0, 355000.0, type="town"),
        lb.RefPoint("Wąchock", 660000.0, 372000.0, type="monastery"),
        lb.RefPoint("Kamień", 650000.0, 360000.0, type="unknown-code"),
    ])
layer = panel.layer
layer.dataProvider().reloadData()
layer.reload()
assert layer.featureCount() == 4, layer.featureCount()
cats = {c.value(): c.label() for c in layer.renderer().categories()}
assert cats["town"] == "miasto" and cats["monastery"] == "klasztor" and None in cats, cats

# --- formularz: uid nadawany automatycznie przy dodawaniu w QGIS
layer.startEditing()
f = QgsFeature(layer.fields())
f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(655000, 365000)))
idx = layer.fields().indexOf("uid")
default = layer.defaultValue(idx, f)
assert default and len(str(default)) == 36, default
f["uid"] = default
f["name"] = "Bodzentyn"
f["type"] = "town"
assert layer.addFeature(f)
assert layer.commitChanges(), layer.commitErrors()
assert layer.editorWidgetSetup(layer.fields().indexOf("type")).type() == "ValueMap"
with lb.Library.open(path) as lib:
    assert [p.name for p, _d in lib.search("bodzentyn")] == ["Bodzentyn"]

# --- zestaw z zaznaczenia i filtr
layer.selectByExpression("\"name\" IN ('Iłża', 'Wąchock')")
assert len(panel.selected_uids()) == 2
sid = panel.create_set("Kielecczyzna-18", panel.selected_uids())
assert panel.current_set() == sid
assert layer.featureCount() == 2, layer.subsetString()
assert "(2)" in panel.cmb_set.currentText()
layer.selectByExpression("\"name\" = 'Kielce'")    # niewidoczny w filtrze – zaznaczenie puste
assert panel.selected_uids() == []
panel.cmb_set.setCurrentIndex(0)
assert layer.subsetString() == "" and layer.featureCount() == 5

# usunięcie z zestawu (zaznaczenie w widoku zestawu)
panel.cmb_set.setCurrentIndex(panel.cmb_set.findData(sid))
layer.selectByExpression("\"name\" = 'Iłża'")
panel.remove_selected_from_set()
assert layer.featureCount() == 1

# dodanie do zestawu: zaznaczenie w widoku wszystkich punktów, potem wybór zestawu bez filtrowania
panel.cmb_set.setCurrentIndex(0)
layer.selectByExpression("\"name\" IN ('Iłża', 'Kielce')")
panel.cmb_set.blockSignals(True)
panel.cmb_set.setCurrentIndex(panel.cmb_set.findData(sid))
panel.cmb_set.blockSignals(False)
panel.add_selected_to_set()
assert layer.featureCount() == 3, layer.featureCount()

# --- niezapisany nowy punkt i punkt bez uid (np. wklejony z innej warstwy)
panel.cmb_set.blockSignals(True)
panel.cmb_set.setCurrentIndex(0)
panel.cmb_set.blockSignals(False)
panel.apply_filter()
layer.startEditing()
f = QgsFeature(layer.fields())
f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(670000, 375000)))
f["uid"] = layer.defaultValue(idx, f)
f["name"] = "Szydłowiec"
assert layer.addFeature(f)
assert layer.isModified()
nouid = QgsFeature(layer.fields())
nouid.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(671000, 376000)))
nouid["name"] = "Bez uid"
nouid["uid"] = None
assert layer.addFeature(nouid)
layer.selectByExpression("\"name\" IN ('Szydłowiec', 'Bez uid')")
assert len(layer.selectedFeatures()) == 2
panel.cmb_set.blockSignals(True)
panel.cmb_set.setCurrentIndex(panel.cmb_set.findData(sid))
panel.cmb_set.blockSignals(False)
panel.add_selected_to_set()
assert not layer.isModified() and layer.isEditable(), "zmiany zapisane, edycja trwa dalej"
with lb.Library.open(path) as lib:
    names = sorted(p.name for p in lib.points(sid))
    assert "Szydłowiec" in names and "Bez uid" in names, names
    assert all(p.uid for p in lib.points())
layer.commitChanges()
assert layer.featureCount() == 5, layer.featureCount()
with lb.Library.open(path) as lib:
    for p in lib.points():
        if p.name in ("Szydłowiec", "Bez uid"):
            lib.delete_point(p.uid)
panel.reload_sets(select=sid)

# --- pusty zestaw
sid2 = panel.create_set("pusty", [])
assert layer.featureCount() == 0
panel.delete_set()
assert panel.current_set() is None and layer.featureCount() == 5

# --- wyróżnienie punktów użytych w analizie
dock.add_gcp(100, -100, 690000.0, 380000.0, name="Iłża")
dock.gcps[-1].uid = uids[0]
dock.add_gcp(300, -150, 640000.0, 355000.0)
dock.gcps_changed()
var = QgsExpressionContextUtils.layerScope(layer).variable(lp.USED_VAR)
assert var == uids[0], var
ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
all_cats = layer.renderer().categories()       # trzymamy kopie – symbol() należy do kategorii
sym = next(c for c in all_cats if c.value() == "town").symbol()
prop = sym.symbolLayer(0).dataDefinedProperties().property(lp._enum(lp.QgsSymbolLayer, "Property", "StrokeWidth"))
widths = {}
for feat in layer.getFeatures("\"type\" = 'town'"):
    ctx.setFeature(feat)
    val, ok = prop.valueAsDouble(ctx, 0.0)
    assert ok
    widths[feat["name"]] = val
assert widths["Iłża"] == 0.9 and widths["Kielce"] == 0.3, widths

# --- punkty GCP do biblioteki (i do wybranego zestawu)
dock.add_gcp(500, -400, 685000.0, 345000.0, name="")            # nowy punkt
dock.add_gcp(510, -410, 640000.4, 355000.3, name="Kielce?")     # 0,5 m od Kielc – powiązanie
dock.gcps_changed()
assert dock.btn_gcp_to_library.isEnabled()
panel.cmb_set.setCurrentIndex(panel.cmb_set.findData(sid))
before = {p.uid for p in lb.Library.open(path).points()}
dock.tbl_gcp.selectAll()
dock.add_selected_gcp_to_library()
print("status:", dock.lbl_status.text())
g = dock.gcps
assert g[0].uid == uids[0] and g[1].uid == uids[1] and g[3].uid == uids[1], [x.uid for x in g]
assert g[2].uid and g[2].uid not in before
with lb.Library.open(path) as lib:
    newp = lib.get_point(g[2].uid)
    assert newp.name == "GCP 3" and (newp.x, newp.y) == (685000.0, 345000.0), newp
    assert set(x.uid for x in g) <= set(lib.set_members(sid))
assert "1" in dock.lbl_status.text() and "3" in dock.lbl_status.text()
assert QgsExpressionContextUtils.layerScope(layer).variable(lp.USED_VAR).count("|") == 2
assert "Punkt biblioteki" in dock.tbl_gcp.item(2, dock_mod.COL_NAME).toolTip()

# --- bez biblioteki przycisk jest nieaktywny
saved_path = panel.path
panel.path = None
dock.update_enabled_state()
assert not dock.btn_gcp_to_library.isEnabled()
panel.path = saved_path
dock.update_enabled_state()

# --- ukrycie i usunięcie warstwy z projektu
panel.chk_layer.setChecked(False)
assert not project.layerTreeRoot().findLayer(layer.id()).isVisible()
panel.chk_layer.setChecked(True)
project.removeMapLayer(layer.id())
assert panel.layer is None and not panel.chk_layer.isChecked()
panel.chk_layer.setChecked(True)
assert panel.layer is not None and panel.layer.featureCount() == 4, panel.layer.featureCount()  # filtr zestawu

# --- ponowne otwarcie panelu: ścieżka z ustawień, warstwa odnaleziona w projekcie
dock.cleanup()
dock2 = dock_mod.QMapAnalystDockWidget(iface)
assert dock2.library_panel.path == os.path.abspath(path)
assert dock2.library_panel.layer is not None and dock2.library_panel.layer.id() == panel.layer.id()
assert dock2.library_panel.cmb_set.count() == 2, dock2.library_panel.cmb_set.count()     # wszystkie + Kielecczyzna-18
dock2.cleanup()

project.removeAllMapLayers()
QgsSettings().remove(lp.SETTINGS_PATH)
QgsSettings().setValue(lp.SETTINGS_SET_ONLY, SET_ONLY_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY UI OK")
