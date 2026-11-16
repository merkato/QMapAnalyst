"""
Rektyfikacja wydawnictwa (offscreen):
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/library_pubrect_qgis.py
Biblioteka z mapą 2×2 arkusze (skany syntetyczne, ramki, punkty), okno wydawnictwa → zakładka
„Wyrównanie blokowe” (siatka wykryta, poprawka i zapis), okno „Rektyfikacja wydawnictwa”: tryb węzłów,
mozaika VRT bez szczelin, zapis siatki, priorytetu i zasięgów w bibliotece, grupa warstw w projekcie.
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
from qgis.PyQt.QtWidgets import QDialog, QMainWindow, QMessageBox  # noqa: E402
from osgeo import gdal  # noqa: E402

dock_mod = __import__(f"{PKG}.dockwidget", fromlist=["*"])
lb = __import__(f"{PKG}.library", fromlist=["*"])
lp = __import__(f"{PKG}.library_panel", fromlist=["*"])
pd = __import__(f"{PKG}.publication_dialog", fromlist=["*"])
prd = __import__(f"{PKG}.publication_rectify_dialog", fromlist=["*"])
spec = importlib.util.spec_from_file_location("test_publication_rectify", os.path.join(HERE, "test_publication_rectify.py"))
tpr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tpr)


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
TMP = tempfile.mkdtemp(prefix="qma_pubrect_ui_")
crs = QgsCoordinateReferenceSystem("EPSG:2180")
project = QgsProject.instance()
project.setCrs(crs)
iface = Iface()
dock = dock_mod.QMapAnalystDockWidget(iface)
panel = dock.library_panel

# --- biblioteka: mapa 2×2 arkusze ze skanami
sheets, scans = tpr.make()
path = os.path.join(TMP, "lib.gpkg")
with lb.Library.create(path, "EPSG:2180") as lib:
    mid = lib.save_map(lb.MapInfo(title="Testkarte", year_from=1780))
    ids = {}
    for s in sheets:
        aid = lib.save_analysis(lb.Analysis(sheet=s.label, map_id=mid, scan_path=scans[s.key], ref_crs="EPSG:2180",
                                            frame=lb.frame_text(s.frame)))
        lib.save_links(aid, [lb.Link(x, y, X, Y, name=f"P{k}") for k, ((x, y), (X, Y)) in enumerate(zip(s.src, s.dst))])
        ids[s.label] = aid
assert panel.use_library(path)

dlg = pd.PublicationDialog(panel, mid, dock)
blk = dlg.block
assert len(blk.sheets) == 4 and {s.label: s.grid for s in blk.sheets} == {"00": (0, 0), "01": (0, 1), "10": (1, 0), "11": (1, 1)}
assert not any(s.grid_saved for s in blk.sheets)
# poprawka siatki w tabeli i zapis
row = [s.label for s in blk.sheets].index("11")
blk.tbl.item(row, 1).setText("3, 3")
assert blk.sheets[row].grid == (2, 2)
blk.tbl.item(row, 1).setText("2, 2")
assert blk.save_grid()
with lb.Library.open(path) as lib:
    a = lib.get_analysis(ids["11"])
    assert (a.grid_row, a.grid_col) == (1, 1)

# --- okno rektyfikacji wydawnictwa
prd.PublicationRectifyDialog.exec = lambda self: QDialog.DialogCode.Rejected
rd = dlg.open_rectify()
assert rd.plan is not None and rd.plan.stitch == "nodes" and not rd.sld_blend.isEnabled()
assert "→" in rd.lbl_stats.text() and rd.spin_res.value() > 0
first = rd.lst_priority.item(0).data(0x0100)
rd.ed_dir.setText(os.path.join(TMP, "wynik"))
rd.spin_res.setValue(10.0)
rd.cmb_format.setCurrentIndex(rd.cmb_format.findData("gtiff_lzw"))
n_before = len(project.mapLayers())
outs = rd.run()
assert len(outs) == 4 and all(os.path.exists(p) for p in outs)
vrt = os.path.join(TMP, "wynik", "Testkarte.vrt")
ds = gdal.Open(vrt)
assert ds is not None and ds.RasterCount == 4
ds = None
assert len(project.mapLayers()) == n_before + 1                 # mozaika VRT w grupie
group = project.layerTreeRoot().children()[0]
assert group.name() == "Testkarte – rektyfikacja"
with lb.Library.open(path) as lib:
    fp = lib.footprints(mid)
    assert len(fp) == 4 and all(f["method"] == "tps" and len(f["ring"]) == 96 for f in fp)
    assert lib.get_analysis(first).priority == 0

# tryb blokowy: styki z wyrównania blokowego (drobne zakładki) – suwak blendowania aktywny
rd.cmb_stitch.setCurrentIndex(1)
rd.recompute()
assert rd.plan.stitch == "block" and rd.sld_blend.isEnabled() == bool(rd.plan.overlaps())

dock.cleanup()
project.removeAllMapLayers()
QgsSettings().setValue(lp.SETTINGS_PATH, PATH_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)
print("LIBRARY PUBRECT OK")
