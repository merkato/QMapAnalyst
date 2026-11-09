"""
Zachowanie wtyczki przy brakujących zależnościach (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/deps_qgis.py scipy|numpy
Blokuje import wskazanego pakietu (sys.modules[...] = None) przed załadowaniem wtyczki.
"""
import faulthandler; faulthandler.enable()  # noqa: E702
import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BLOCK = sys.argv[1] if len(sys.argv) > 1 else "scipy"
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsProject, QgsRasterLayer  # noqa: E402

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox, QToolBar  # noqa: E402

import numpy as real_np  # noqa: E402 – do przygotowania danych testu
for name in list(sys.modules):
    if name == BLOCK or name.startswith(BLOCK + "."):
        del sys.modules[name]
for name in (BLOCK, f"{BLOCK}.optimize", f"{BLOCK}.interpolate", f"{BLOCK}.spatial"):
    sys.modules[name] = None

deps = __import__(f"{PKG}.deps", fromlist=["*"])
plugin_mod = __import__(f"{PKG}.plugin", fromlist=["*"])
required, optional = deps.check()
print("check:", required, optional)
print("hints:", deps.install_commands(required or deps.packages_of(optional)))


class Iface:
    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas(self.win)
        self.win.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar(self.win)
        self.tb = QToolBar(self.win)
        self.messages = []
        orig = self.bar.pushMessage

        def push(*a):
            self.messages.append(a)
            return orig(*a)
        self.bar.pushMessage = push

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.win

    def messageBar(self):
        return self.bar

    def addToolBarIcon(self, a):
        self.tb.addAction(a)

    def removeToolBarIcon(self, a):
        self.tb.removeAction(a)

    def addPluginToMenu(self, *a):
        pass

    def removePluginMenu(self, *a):
        pass

    def addDockWidget(self, area, w):
        self.win.addDockWidget(area, w)

    def removeDockWidget(self, w):
        self.win.removeDockWidget(w)


shown = []
QMessageBox.exec = lambda self: shown.append(self.text() + "\n" + self.informativeText()) or 0

iface = Iface()
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface.canvas.setDestinationCrs(crs)
QgsProject.instance().setCrs(crs)
plugin = plugin_mod.QMapAnalystPlugin(iface)
plugin.initGui()
plugin.action.setChecked(True)
plugin.run(True)

if BLOCK == "numpy":
    assert required == ["numpy"], required
    assert plugin.dockwidget is None and shown and not plugin.action.isChecked()
    print("dialog:", shown[0].replace("\n", " | "))
    plugin.unload()
    print("DEPS numpy OK")
    sys.exit(0)

assert not required and optional, (required, optional)
assert iface.messages, "brak ostrzeżenia na pasku komunikatów"
print("message bar:", iface.messages[0][1])
dock = plugin.dockwidget
assert dock is not None

# --- przepływ pracy bez scipy: analiza, warstwy, estymacja (komunikat zamiast wyjątku)
from osgeo import gdal  # noqa: E402
import tempfile  # noqa: E402
tmp = tempfile.mkdtemp(prefix="qma_deps_")
raster = os.path.join(tmp, "scan.tif")
ds = gdal.GetDriverByName("GTiff").Create(raster, 2000, 1600, 1, gdal.GDT_Byte)
ds.GetRasterBand(1).Fill(200)
ds = None
dock.set_old_raster(QgsRasterLayer(raster, "scan"))
rng = real_np.random.default_rng(3)
for _ in range(15):
    px, py = rng.uniform(100, 1900), -rng.uniform(100, 1500)
    a = math.radians(2)
    rx = 500000 + 5 * (px * math.cos(a) - py * math.sin(a)) + rng.normal(0, 3)
    ry = 250000 + 5 * (px * math.sin(a) + py * math.cos(a)) + rng.normal(0, 3)
    dock.add_gcp(px, py, rx, ry)
dock.gcps_changed()
dock._recalc_timer.stop()
dock.run_analysis()
assert dock.analysis is not None, dock.lbl_status.text()
print("analysis:", dock.lbl_status.text())
for fn in (dock.generate_gcp_points, dock.generate_result_layers, dock.generate_error_circles,
           dock.generate_distortion_grid, dock.generate_old_vectors, dock.generate_historic_grid):
    fn()
    print(fn.__name__, "->", dock.lbl_status.text())
for k in ("scale", "rot", "omega"):
    dock.generate_isolines(k)
    print("isolines", k, "->", dock.lbl_status.text())
    assert "scipy" in dock.lbl_status.text()
dock.estimate_projection()
print("projection ->", dock.lbl_status.text())
assert dock.projection is None
plugin.unload()
print("DEPS scipy OK")
