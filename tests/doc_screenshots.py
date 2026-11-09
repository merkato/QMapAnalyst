"""
Zrzuty ekranu panelu QMapAnalyst do dokumentacji (docs/*.png). Uruchomienie bez offscreen (potrzebne czcionki):
    python-qgis.bat tests/doc_screenshots.py pl|en <skan.jpg>

Skan: arkusz VI szkicu indykacyjnego katastru austriackiego gminy Milówka (Archiwum Narodowe w Krakowie),
obraz 2000 × 1733 px. Piksele punktów GCP wskazują szczegóły tego obrazu; współrzędne odniesienia są wyznaczane
z założonej transformacji (skala 1:2880 przy 62 DPI, obrót, skurcz papieru, łagodne zniekształcenia, szum,
jeden punkt odstający) – to ilustracja działania wtyczki, a nie georeferencja arkusza.
"""
import faulthandler; faulthandler.enable()  # noqa: E702
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
OUT_DIR = os.path.join(PLUGIN_DIR, "docs")
PKG = os.path.basename(PLUGIN_DIR)
LANG = sys.argv[1] if len(sys.argv) > 1 else "pl"
SCAN = sys.argv[2]
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))

import numpy as np  # noqa: E402
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,  # noqa: E402
                       QgsPointXY, QgsProject, QgsRasterLayer)

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.PyQt.QtCore import QTranslator  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QSplitter  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402

lang_mod = __import__(f"{PKG}.lang", fromlist=["*"])
lang_mod.set_language(LANG)
translator = QTranslator()
translator.load(os.path.join(PLUGIN_DIR, "i18n", f"QMapAnalyst_{LANG}.qm"))
QgsApplication.installTranslator(translator)
dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])


class Iface:
    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas(self.win)
        self.win.setCentralWidget(self.canvas)

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.win


def settle(canvas=None, n=30):
    for _ in range(n):
        qgs.processEvents()
    if canvas is not None:
        canvas.refresh()
        canvas.waitWhileRendering()
        for _ in range(n):
            qgs.processEvents()


iface = Iface()
crs = QgsCoordinateReferenceSystem("EPSG:2180")
iface.canvas.setDestinationCrs(crs)
QgsProject.instance().setCrs(crs)
lyr = QgsRasterLayer(SCAN, "Milowka_arkusz_VI")
assert lyr.isValid(), SCAN

# --- punkty: piksele (kolumna, wiersz) na wyraźnych szczegółach arkusza
pix = np.array([
    (168, 168), (172, 332), (518, 418), (795, 578), (1713, 295), (1515, 577), (1430, 705),
    (1335, 700), (962, 452), (1105, 1040), (1430, 1070), (980, 1122), (430, 1212), (300, 890),
    (650, 830), (1590, 1500), (1765, 1777), (168, 1777), (1190, 345), (1760, 1240), (820, 1450),
    (1240, 1610),
], dtype=float)
old = np.column_stack([pix[:, 0], -pix[:, 1]])

# --- założona (demonstracyjna) relacja skan → PUWG 1992 w okolicy Milówki
DPI = 62
m_per_px = 2880 * 0.0254 / DPI
ct = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, QgsProject.instance())
c = ct.transform(QgsPointXY(19.0886, 49.5611))
rot = math.radians(0.8)
sx, sy = m_per_px * 1.000, m_per_px * 0.9955          # skurcz papieru w pionie ~0,45 %
u = old - np.array([966.0, -972.0])
xy = np.column_stack([sx * (u[:, 0] * math.cos(rot) - u[:, 1] * math.sin(rot)),
                      sy * (u[:, 0] * math.sin(rot) + u[:, 1] * math.cos(rot))])
xy += np.column_stack([2.2 * np.sin(u[:, 1] / 420.0), 1.8 * np.sin(u[:, 0] / 380.0)])  # łagodne zniekształcenia
rng = np.random.default_rng(7)
xy += rng.normal(0, 0.9, xy.shape)
xy[9] += [7.5, -6.0]                                  # punkt odstający (błąd identyfikacji)
ref = xy + [c.x(), c.y()]

dock = dock_mod.QMapAnalystDockWidget(iface)
dock.resize(1180, 860)
dock.show()
dock.set_old_raster(lyr)
for (ox, oy), (rx, ry) in zip(old, ref):
    dock.add_gcp(float(ox), float(oy), float(rx), float(ry))
dock.gcps_changed()
dock.spin_dpi.setValue(DPI)
dock.cmb_transform_type.setCurrentIndex(1)   # afiniczna
dock._recalc_timer.stop()
dock.run_analysis()
dock._recalc_timer.stop()
print("analysis:", dock.lbl_status.text())

suffix = "" if LANG == "pl" else "_en"
splitter = dock.findChild(QSplitter)


def shot(name, tab, sizes=(560, 300)):
    splitter.setSizes(list(sizes))
    dock.tab_widget.setCurrentIndex(tab)
    settle(dock.old_canvas)
    path = os.path.join(OUT_DIR, f"{name}{suffix}.png")
    dock.grab().save(path)
    print("saved", path)


# 1. GCP – tabela przewinięta do punktu odstającego (#10)
dock.zoom_full_old()
dock.tbl_gcp.clearSelection()
dock.tbl_gcp.scrollToItem(dock.tbl_gcp.item(9, 0))
shot("gcp_table", 0, sizes=(470, 390))

# 2. Analiza
dock.txt_report.verticalScrollBar().setValue(0)
shot("analysis", 1, sizes=(330, 530))

# 3. Warstwy z nakładkami na skanie
dock.spin_grid_step.setValue(200)
dock.sld_confidence.setValue(600)
dock.spin_magnification.setValue(20.0)
dock.generate_old_vectors()
dock.generate_historic_grid()
dock.spin_iso_density.setValue(8)
dock.generate_isolines("scale")
shot("results", 3)

# 4. Odwzorowanie – ranking i siatka geograficzna najlepszego kandydata
dock.estimate_projection()
dock.clear_old_overlays()
dock.generate_graticule()
dock.zoom_full_old()
shot("projection", 2, sizes=(430, 430))

dock.cleanup()
print("DONE")
