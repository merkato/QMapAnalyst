"""
Okno macierzy pokrycia (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_coverage_qgis.py
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
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QFileDialog, QMainWindow, QMessageBox  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
cd = __import__(f"{PKG}.coverage_dialog", fromlist=["*"])
spec = importlib.util.spec_from_file_location("test_coverage", os.path.join(HERE, "test_coverage.py"))
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)


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
TMP = tempfile.mkdtemp(prefix="qma_covui_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
project = QgsProject.instance()
project.setCrs(crs)
iface = Iface()
iface.canvas.setDestinationCrs(crs)
dock = dock_mod.QMapAnalystDockWidget(iface)
panel = dock.library_panel

path = os.path.join(TMP, "lib.gpkg")
lib, sid, uid, (a1, a2, a3, a4) = tc.make_library(path)
lib.close()
assert panel.use_library(path)
assert not panel.btn_coverage.isEnabled()                 # bez wybranego zestawu
panel.cmb_set.setCurrentIndex(panel.cmb_set.findData(sid))
assert panel.btn_coverage.isEnabled()

dlg = cd.CoverageDialog(panel)
t = dlg.table
assert t.rowCount() == 14 and t.columnCount() == 4, (t.rowCount(), t.columnCount())
assert t.horizontalHeaderItem(0).text() == "Zannoni (1772)" and t.horizontalHeaderItem(3).text() == "Σ"
assert t.verticalHeaderItem(3).text() == "P03" and t.verticalHeaderItem(3).font().bold()
assert t.item(3, 0).text() == "!" and t.item(3, 0).data(Qt.ItemDataRole.UserRole) == "outlier"
assert t.item(13, 0).text() == "○" and t.item(2, 1).text() == "?" and t.item(5, 1).text() == "–"
assert t.item(3, 2).text() == "×" and t.item(4, 2).text() == ""
assert t.item(0, 3).text() == "3/3"
assert "odstające nie badane" in t.horizontalHeaderItem(2).toolTip()
assert "Punkty wspólne wszystkim mapom (pogrubione): 3 – P00, P01, P02" in dlg.lbl_summary.text(), dlg.lbl_summary.text()
assert "P03" in dlg.lbl_summary.text().splitlines()[1]
assert "„K”" in dlg.lbl_title.text()

# model Helmerta – trzecia mapa też wyrównana
dlg.cmb_model.setCurrentIndex(dlg.cmb_model.findData("helmert"))
assert "odstające nie badane" not in t.horizontalHeaderItem(2).toolTip()

# eksport
out_csv = os.path.join(TMP, "k.csv")
out_json = os.path.join(TMP, "k")
answers = iter([(out_csv, ""), (out_json, "")])
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: next(answers))
dlg.export("csv")
dlg.export("json")
assert open(out_csv, encoding="utf-8-sig").readline().startswith("uid,name,Zannoni (1772)")
assert os.path.isfile(out_json + ".json") and "Zapisano" in dlg.lbl_legend.text()

# dwuklik w kolumnę – wczytanie analizy w panelu (przez przycisk w zakładce Biblioteka)
cd.CoverageDialog.exec = lambda self: (self.on_cell_double_clicked(0, 1), self.result())[-1]
panel.coverage_dialog()
assert dock.library_analysis is not None and dock.library_analysis.analysis_id == a2, dock.library_analysis
assert len(dock.gcps) == 13 and dock.gcps[2].status == "uncertain" and not dock.gcps[5].enabled

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().setValue(lp.SETTINGS_PATH, PATH_BEFORE)
QgsSettings().setValue(dock_mod.SETTINGS_NAMES, NAMES_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY COVERAGE OK")
