"""
Zrzuty do dokumentacji wersji 0.3 (biblioteka punktów, seria czasowa, wydawnictwo wieloarkuszowe).
Uruchomienie bez offscreen (potrzebne czcionki), na KOPII biblioteki:
    python-qgis.bat tests/doc_screenshots_library.py pl|en <biblioteka.gpkg>

Biblioteka: zestaw „TKKP” (Kielecczyzna) – Polonia et Silesia (1585), Zannoni (1772), Karte von West-Galizien
(1801–1808) i TKKP (1822–1843, arkusze Iłża i Szydłów); skany według ścieżek zapisanych w bibliotece.
Podkład mapy głównej – OpenStreetMap (gdy dostępna sieć).
"""
import faulthandler; faulthandler.enable()  # noqa: E702
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
OUT_DIR = os.path.join(PLUGIN_DIR, "docs")
PKG = os.path.basename(PLUGIN_DIR)
LANG = sys.argv[1] if len(sys.argv) > 1 else "pl"
LIBRARY = sys.argv[2]
REPORT_PDF = None
SUFFIX = "" if LANG == "pl" else "_en"
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
os.environ.pop("QT_QPA_PLATFORM", None)

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsProject, QgsRasterLayer,  # noqa: E402
                       QgsRectangle, QgsSettings)

qgs = QgsApplication([], True)
qgs.initQgis()
from qgis.PyQt.QtCore import QTranslator  # noqa: E402
from qgis.PyQt.QtGui import QImage  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox, QSplitter  # noqa: E402
from qgis.gui import QgsLayerTreeMapCanvasBridge, QgsMapCanvas  # noqa: E402

lang_mod = __import__(f"{PKG}.lang", fromlist=["*"])
lang_mod.set_language(LANG)
translator = QTranslator()
translator.load(os.path.join(PLUGIN_DIR, "i18n", f"QMapAnalyst_{LANG}.qm"))
QgsApplication.installTranslator(translator)
dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
cd = __import__(f"{PKG}.coverage_dialog", fromlist=["*"])
cv = __import__(f"{PKG}.coverage", fromlist=["*"])
md = __import__(f"{PKG}.maps_dialog", fromlist=["*"])
sd = __import__(f"{PKG}.series_dialog", fromlist=["*"])
pd = __import__(f"{PKG}.publication_dialog", fromlist=["*"])
srep = __import__(f"{PKG}.series_report", fromlist=["*"])
lb = __import__(f"{PKG}.library", fromlist=["*"])

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
SETTINGS = {k: QgsSettings().value(k) for k in (lp.SETTINGS_PATH, lp.SETTINGS_SET_ONLY, dock_mod.SETTINGS_NAMES, cd.SETTINGS_BY)}


class Iface:
    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas(self.win)
        self.win.setCentralWidget(self.canvas)

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.win


def settle(canvas=None, n=40):
    for _ in range(n):
        qgs.processEvents()
    if canvas is not None:
        canvas.refresh()
        canvas.waitWhileRendering()
        for _ in range(n):
            qgs.processEvents()


def save(widget, name, fmt="png"):
    """Zrzut widżetu; mapy (podkład OSM, bez napisów interfejsu) – JPEG bez przyrostka języka."""
    if fmt == "jpg":
        path = os.path.join(OUT_DIR, f"{name}.jpg")
        widget.grab().toImage().convertToFormat(QImage.Format.Format_RGB32).save(path, "JPG", 85)
    else:
        path = os.path.join(OUT_DIR, f"{name}{SUFFIX}.png")
        widget.grab().save(path)
    print("saved", path)


def show_dialog(dlg, w, h, name, tab=None):
    dlg.resize(w, h)
    dlg.show()
    if tab is not None:
        dlg.tabs.setCurrentIndex(tab)
    settle()
    save(dlg, name)


crs = QgsCoordinateReferenceSystem("EPSG:2180")
project = QgsProject.instance()
project.setCrs(crs)
iface = Iface()
iface.canvas.setDestinationCrs(crs)
iface.win.resize(1100, 760)
bridge = QgsLayerTreeMapCanvasBridge(project.layerTreeRoot(), iface.canvas)   # jak w QGIS: drzewo warstw → mapa
osm = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0", "OpenStreetMap", "wms")
print("OSM:", osm.isValid())
if osm.isValid():
    project.addMapLayer(osm)

dock = dock_mod.QMapAnalystDockWidget(iface)
dock.chk_names.setChecked(True)
dock.resize(1180, 860)
dock.show()
panel = dock.library_panel
panel.chk_set_only.setChecked(False)
assert panel.use_library(LIBRARY)
panel.show_layer(True)
with lb.Library.open(LIBRARY) as lib:
    sid = next(s for s, n, _d, _c in lib.sets() if n == "TKKP")
    tkkp = next(m for m in lib.maps() if m.title == "TKKP")
    sheets = sorted(lib.analyses(tkkp.map_id), key=lambda a: a.analysis_id)
panel.cmb_set.setCurrentIndex(panel.cmb_set.findData(sid))
if osm.isValid():                                          # podkład pod warstwą biblioteki
    root = project.layerTreeRoot()
    node = root.findLayer(osm.id())
    root.insertChildNode(-1, node.clone())
    root.removeChildNode(node)
splitter = dock.findChild(QSplitter)

# --- arkusz TKKP Szydłów z biblioteki: punkty powiązane, ramka mapy, transformacja
assert dock.open_library_analysis(sheets[-1].analysis_id)
dock.cmb_transform_type.setCurrentIndex(dock.cmb_transform_type.findData("affine"))
dock._recalc_timer.stop()
dock.run_analysis()
dock._recalc_timer.stop()
dock.zoom_full_old()
print("analysis:", dock.lbl_status.text())

splitter.setSizes([470, 390])
dock.tab_widget.setCurrentIndex(0)
settle(dock.old_canvas)
save(dock, "gcp_library")

lib_tab = next(i for i in range(dock.tab_widget.count()) if dock.tab_widget.widget(i) is panel
               or panel.isAncestorOf(dock.tab_widget.widget(i)) or dock.tab_widget.widget(i).isAncestorOf(panel))
splitter.setSizes([380, 480])
dock.tab_widget.setCurrentIndex(lib_tab)
settle(dock.old_canvas)
save(dock, "library_tab")

# --- mapa główna: biblioteka (zestaw), zasięg arkusza z transformacji
area = dock.footprint_ref()
if area:
    xs, ys = [x for x, _y in area], [y for _x, y in area]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    iface.canvas.setExtent(QgsRectangle(min(xs) - 0.25 * w, min(ys) - 0.25 * h, max(xs) + 0.25 * w, max(ys) + 0.25 * h))
iface.win.show()
dock.update_frame_bands()
settle(iface.canvas, 80)
save(iface.canvas, "frame_main", "jpg")

# --- okna: macierz pokrycia, mapy i arkusze
cov = cd.CoverageDialog(panel)
cov.cmb_by.setCurrentIndex(cov.cmb_by.findData(cv.BY_MAP))
show_dialog(cov, 900, 620, "coverage")
cov.close()
maps = md.MapsDialog(panel, dock)
show_dialog(maps, 860, 420, "maps_dialog")
maps.close()

# --- seria czasowa: tabele, wykresy, warstwa wektorów na mapie głównej
ser = sd.SeriesDialog(panel)
show_dialog(ser, 1000, 620, "series_maps", tab=0)
ser.tabs.setCurrentIndex(1)
settle()
save(ser, "series_similarity")
ser.tabs.setCurrentIndex(3)
settle()
save(ser, "series_charts")
for name, _title, svg in ser.charts:
    srep.svg_to_image(svg, scale=1.5).save(os.path.join(OUT_DIR, f"chart_{name}{SUFFIX}.png"))
report_pdf = os.path.join(OUT_DIR, f"_raport{SUFFIX}.pdf")      # raport serii czasowej (strony do instrukcji)
html, images = srep.series_report(ser.series, "pdf", ser._unit_m())
srep.save_report(report_pdf, html, images, ser._title(), ser.cmb_model.currentText())
REPORT_PDF = report_pdf
ser.sp_scale.setValue(3)              # Polonia: błędy rzędu 10 km
vec = ser.add_vectors_layer()
traj = ser.add_trajectories_layer()
ser.close()
pts = [p for m in ser.series.maps for p in m.pos.values()]
if pts:
    xs, ys = [x for x, _y in pts], [y for _x, y in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    iface.canvas.setExtent(QgsRectangle(min(xs) - 0.15 * w, min(ys) - 0.15 * h, max(xs) + 0.15 * w, max(ys) + 0.15 * h))
dock._drop_band("main")
project.layerTreeRoot().findLayer(traj.id()).setItemVisibilityChecked(False)
settle(iface.canvas, 80)
save(iface.canvas, "series_vectors_main", "jpg")
project.layerTreeRoot().findLayer(vec.id()).setItemVisibilityChecked(False)
project.layerTreeRoot().findLayer(traj.id()).setItemVisibilityChecked(True)
settle(iface.canvas, 80)
save(iface.canvas, "series_trajectories_main", "jpg")

# --- wydawnictwo wieloarkuszowe TKKP (arkusze Iłża i Szydłów)
pub = pd.PublicationDialog(panel, tkkp.map_id, dock)
show_dialog(pub, 900, 520, "publication", tab=0)
for name, _title, svg in pub.charts:
    srep.svg_to_image(svg, scale=1.5).save(os.path.join(OUT_DIR, f"chart_{name}{SUFFIX}.png"))
pub.close()

# --- strony raportu serii czasowej (PDF użytkownika)
if REPORT_PDF:
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(REPORT_PDF)
    md_ = ds.GetMetadata("SUBDATASETS") or {}
    pages = [md_[k] for k in sorted(md_, key=lambda k: int(k.split("_")[1])) if k.endswith("_NAME")] or [REPORT_PDF]
    for i, page in enumerate(pages[:2], 1):
        out = os.path.join(OUT_DIR, f"report_series_p{i}{SUFFIX}.png")
        gdal.Translate(out, gdal.Open(page), format="PNG", width=820)
        for aux in (out + ".aux.xml", REPORT_PDF + ".aux.xml"):
            if os.path.exists(aux):
                os.remove(aux)
        print("saved", out)
    ds = None
    os.remove(REPORT_PDF)

dock.cleanup()
project.removeAllMapLayers()
for k, v in SETTINGS.items():
    if v is None:
        QgsSettings().remove(k)
    else:
        QgsSettings().setValue(k, v)
print("DONE")
