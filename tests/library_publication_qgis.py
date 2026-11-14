"""
Okno „Wydawnictwo wieloarkuszowe” (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_publication_qgis.py
"""
import faulthandler; faulthandler.enable()  # noqa: E702
import importlib.util
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
from qgis.PyQt.QtWidgets import QFileDialog, QMainWindow, QMessageBox  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
md = __import__(f"{PKG}.maps_dialog", fromlist=["*"])
pd = __import__(f"{PKG}.publication_dialog", fromlist=["*"])
spec = importlib.util.spec_from_file_location("test_publication", os.path.join(HERE, "test_publication.py"))
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)


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
TMP = tempfile.mkdtemp(prefix="qma_pubui_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
project = QgsProject.instance()
project.setCrs(crs)
iface = Iface()
iface.canvas.setDestinationCrs(crs)
dock = dock_mod.QMapAnalystDockWidget(iface)
panel = dock.library_panel
path = os.path.join(TMP, "lib.gpkg")
lib, mid, uid, ids = tp.make_library(path)
lib.close()
assert panel.use_library(path)

# --- przycisk w oknie Mapy i arkusze: tylko dla mapy z co najmniej dwoma arkuszami
maps = md.MapsDialog(panel, dock)
tree = maps.tree
items = {tree.topLevelItem(i).text(0): tree.topLevelItem(i) for i in range(tree.topLevelItemCount())}
tree.setCurrentItem(items["Inna"])
assert not maps.btn_publication.isEnabled()
tree.setCurrentItem(items["Flötzkarte (1857)"])
assert maps.btn_publication.isEnabled()
tree.setCurrentItem(items["Flötzkarte (1857)"].child(0))             # arkusz – też jego mapa
assert maps.btn_publication.isEnabled()

# --- okno wydawnictwa
dlg = pd.PublicationDialog(panel, mid, dock)
t = dlg.tbl_sheets
assert t.rowCount() == 3 and [t.item(r, 0).text() for r in range(3)] == ["arkusz 1", "arkusz 2", "arkusz 10"]
assert t.item(0, 5).text().startswith("1 : 47") and t.item(1, 9).text() == "✓"
o = dlg.tbl_overlaps
assert o.rowCount() == 5 and o.item(0, 0).text() == "Z1" and o.item(0, 1).text() == "arkusz 1"
assert "rozrzut" in dlg.lbl_summary.text() and "Z1" in dlg.lbl_summary.text(), dlg.lbl_summary.text()
assert dlg.lbl_title.text() == "„Flötzkarte” (1857): arkusze 3"

layer = dlg.add_index_layer()
assert layer.featureCount() == 3 and layer.crs().authid() == "EPSG:2180" and layer.labelsEnabled()
assert sorted(f["sheet"] for f in layer.getFeatures()) == ["1", "10", "2"]

out = os.path.join(TMP, "arkusze")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (out, ""))
dlg.export()
assert open(out + ".csv", encoding="utf-8-sig").readline().startswith("analysis_id,sheet")

# wykresy i raport
assert len(dlg.charts) == 2 and len(dlg.charts_view.labels) == 2
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (os.path.join(TMP, "wyd"), ""))
pdf = dlg.export_report("pdf")
assert pdf.endswith(".pdf") and open(pdf, "rb").read(5) == b"%PDF-"
html = open(dlg.export_report("html"), encoding="utf-8").read()
assert html.count("<svg") == 2 and "Zakładki" in html and "Z1" in html
charts_dir = os.path.join(TMP, "wykresy")
os.makedirs(charts_dir)
QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: charts_dir)
assert len(dlg.export_charts()) == 4

# dwuklik w arkusz – wczytanie arkusza
dlg.on_sheet_double_clicked(2, 0)
assert dlg.chosen_analysis == ids["10"] and dock.library_analysis.analysis_id == ids["10"]
assert len(dock.gcps) == 7

# stan bieżący wczytanego arkusza (niezapisana zmiana) – gwiazdka
dock.gcps[0].enabled = False
dock.gcps_changed()
dlg.refresh()
assert dlg.tbl_sheets.item(2, 0).text() == "arkusz 10 *" and dlg.tbl_sheets.item(2, 1).text() == "6"

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().setValue(lp.SETTINGS_PATH, PATH_BEFORE)
QgsSettings().setValue(dock_mod.SETTINGS_NAMES, NAMES_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY PUBLICATION OK")
