"""
Zasięg mapy (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_frame_qgis.py
Ramka mapy na skanie (cały skan albo 4 rogi), zasięg na mapie głównej po obliczeniu transformacji,
„Następny z zestawu” bez punktów spoza zasięgu, zapis ramki w analizie, „poza zasięgiem” w macierzy pokrycia.
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
    QgsApplication, QgsCoordinateReferenceSystem, QgsPointXY, QgsProject, QgsRasterLayer, QgsSettings,
)

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog, QMainWindow, QMessageBox  # noqa: E402
from osgeo import gdal  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lb = __import__(f"{PKG}.library", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
md = __import__(f"{PKG}.maps_dialog", fromlist=["*"])
cd = __import__(f"{PKG}.coverage_dialog", fromlist=["*"])


class Iface:
    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas(self.win)
        self.win.setCentralWidget(self.canvas)

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.win


def analysis_exec(self):
    self.cmb_map.setCurrentIndex(self.cmb_map.count() - 1)
    self.ed_title.setText("Mapa z ramką")
    self.accept()
    return QDialog.DialogCode.Accepted


QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
md.AnalysisDialog.exec = analysis_exec
PATH_BEFORE = QgsSettings().value(lp.SETTINGS_PATH, "")
NAMES_BEFORE = QgsSettings().value(dock_mod.SETTINGS_NAMES, False, type=bool)
TMP = tempfile.mkdtemp(prefix="qma_frame_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface = Iface()
iface.canvas.setDestinationCrs(crs)
project = QgsProject.instance()
project.setCrs(crs)
LEFT = Qt.MouseButton.LeftButton


def to_ref(px, py):
    return 600000 + 25 * px, 300000 + 25 * py


raster = os.path.join(TMP, "scan.tif")
ds = gdal.GetDriverByName("GTiff").Create(raster, 2000, 1600, 1, gdal.GDT_Byte)
ds.GetRasterBand(1).Fill(200)
ds = None
PIX = {"P0": (200, -200), "P1": (1800, -300), "P2": (300, -1400), "P3": (1700, -1300), "P4": (600, -700),
       "P5": (1500, -800), "Daleko1": (3000, -500), "Daleko2": (-900, -900)}
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
by_name = {p.name: p for p in pts}

# --- 1. bez transformacji zasięg nieznany; po obliczeniu – cały skan
assert dock.map_frame is None and dock.effective_frame() == [(0, 0), (2000, 0), (2000, -1600), (0, -1600)], dock.effective_frame()
assert dock.footprint_ref() is None and dock.footprint_contains(by_name["Daleko1"]) is None
for n in ("P0", "P1", "P2", "P3"):
    dock.add_gcp(*PIX[n], *to_ref(*PIX[n]), name=n, uid=uid[n])
dock.gcps_changed()
dock._recalc_timer.stop()
dock.run_analysis()
assert dock.analysis is not None
assert dock.footprint_contains(by_name["P5"]) is True and dock.footprint_contains(by_name["Daleko1"]) is False
assert "main" in dock._frame_bands                                   # zasięg na mapie głównej

# --- 2. „Następny z zestawu” pomija punkty spoza zasięgu
dock.next_library_point()
assert dock._lib_target is not None and dock._lib_target.name in ("P4", "P5"), dock._lib_target
assert "Poza zasięgiem mapy: 2" in dock.lbl_status.text(), dock.lbl_status.text()

# --- 3. ramka – 4 rogi wskazane na skanie (P5 poza ramką)
dock.start_frame_capture()
assert dock._lib_target is None and dock.btn_capture.isChecked()
for x, y in ((0, 0), (1000, 0), (1000, -1600), (0, -1600)):
    dock.on_old_canvas_clicked(QgsPointXY(x, y), LEFT)
assert dock.map_frame == [(0, 0), (1000, 0), (1000, -1600), (0, -1600)] and len(dock.gcps) == 4
assert "old" in dock._frame_bands
assert dock.footprint_contains(by_name["P5"]) is False and dock.footprint_contains(by_name["P4"]) is True
dock.next_library_point()
assert dock._lib_target.name == "P4" and "Poza zasięgiem mapy: 3" in dock.lbl_status.text(), dock.lbl_status.text()
dock._clear_lib_target()

# --- 4. zapis ramki w analizie, macierz pokrycia, odczyt
assert dock.save_library_analysis()
aid = dock.library_analysis.analysis_id
with lb.Library.open(path) as lib:
    assert lb.frame_points(lib.get_analysis(aid).frame) == [(0, 0), (1000, 0), (1000, -1600), (0, -1600)]
cov = cd.CoverageDialog(panel).coverage
assert cov.cell(uid["P5"], aid) == "outside" and cov.cell(uid["Daleko1"], aid) == "outside"
assert cov.cell(uid["P4"], aid) is None and cov.cell(uid["P0"], aid) == "sure"
assert not dock.analysis_dirty()
dock.reset_frame()
assert dock.map_frame is None and dock.analysis_dirty()               # zmiana ramki = niezapisana zmiana
cov = cd.CoverageDialog(panel).coverage                                # stan bieżący: cały skan
assert cov.cell(uid["P5"], aid) is None and cov.cell(uid["Daleko1"], aid) == "outside"
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)   # porzuć zmiany
assert dock.open_library_analysis(aid)
assert dock.map_frame == [(0, 0), (1000, 0), (1000, -1600), (0, -1600)] and not dock.analysis_dirty()

dock.cleanup()
assert not dock._frame_bands
project.removeAllMapLayers()
QgsSettings().setValue(lp.SETTINGS_PATH, PATH_BEFORE)
QgsSettings().setValue(dock_mod.SETTINGS_NAMES, NAMES_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY FRAME OK")
