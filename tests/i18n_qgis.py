"""
Kompletność tłumaczeń: cały przepływ pracy w języku angielskim i polskim.

Zbiera wszystkie teksty widoczne w panelu (etykiety, przyciski, podpowiedzi, zakładki,
listy, nagłówki i komórki tabel, raporty, pasek stanu, nazwy warstw i legendy) i sprawdza:
* EN – brak polskich znaków i słów,
* PL – liczby w raporcie z przecinkiem dziesiętnym.

    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/i18n_qgis.py en
    set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/i18n_qgis.py pl
"""
import faulthandler
import math
import os
import re
import sys
import tempfile

faulthandler.enable()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)
LANG = sys.argv[1] if len(sys.argv) > 1 else "en"

import numpy as np
from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRasterLayer)

qgs = QgsApplication([], True)
qgs.initQgis()

from qgis.PyQt.QtCore import QCoreApplication, QEvent, QTranslator
from qgis.PyQt.QtWidgets import (QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit, QMainWindow,
                                 QTableWidget, QTabWidget, QTextBrowser, QWidget)
from qgis.gui import QgsMapCanvas

lang_mod = __import__(f"{PKG}.lang", fromlist=["*"])
lang_mod.set_language(LANG)
translator = QTranslator()
assert translator.load(os.path.join(PLUGIN_DIR, "i18n", f"QMapAnalyst_{LANG}.qm"))
QCoreApplication.installTranslator(translator)

dm = __import__(f"{PKG}.dockwidget", fromlist=["*"])
pj = __import__(f"{PKG}.projections", fromlist=["*"])
me = __import__(f"{PKG}.math_engine", fromlist=["*"])

PL_CHARS = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
PL_WORDS = re.compile(r"\b(nie|oraz|lub|punkt\w*|map[aey]\b|warstw\w*|siatk\w*|błąd\w*|skal[aiy]\b|brak\w*|"
                      r"wymaga\w*|układ\w*|odwzorowan\w*|dawn\w*|wczytaj|zapisz|usuń|oblicz\w*|wynik\w*|"
                      r"analiz\w*|dopasowan\w*|równoleżnik\w*|południk\w*|izolin\w*|okręg\w*|wektor\w*)\b", re.I)
# nazwy własne dopuszczalne w obu językach
ALLOWED = ["Kuna, Panecki, Zawadzki", "Olszewicz", "Carte de l'État-major", "État-major", "Messtischblatt",
           "Carte de Cassini", "Cassini–Soldner", "Gauss–Krüger", "Sanson–Flamsteed", "Krüger"]


def collect_texts(dock):
    out = []

    def add(where, t):
        if t:
            out.append((where, str(t)))

    for w in dock.findChildren(QWidget):
        add("tooltip", w.toolTip())
        if isinstance(w, QLabel):
            add("label", w.text())
        elif isinstance(w, QAbstractButton):
            add("button", w.text())
        elif isinstance(w, QGroupBox):
            add("group", w.title())
        elif isinstance(w, QLineEdit):
            add("placeholder", w.placeholderText())
        elif isinstance(w, QComboBox):
            for i in range(w.count()):
                add("combo", w.itemText(i))
                add("combo-tip", w.itemData(i, 3))  # ToolTipRole
        elif isinstance(w, QTabWidget):
            for i in range(w.count()):
                add("tab", w.tabText(i))
        elif isinstance(w, QTableWidget):
            for c in range(w.columnCount()):
                it = w.horizontalHeaderItem(c)
                if it:
                    add("header", it.text())
                    add("header-tip", it.toolTip())
            for r in range(w.rowCount()):
                for c in range(w.columnCount()):
                    it = w.item(r, c)
                    if it:
                        add("cell", it.text())
                        add("cell-tip", it.toolTip())
        elif isinstance(w, QTextBrowser):
            add("report", w.toPlainText())
    for key in dock.registry.keys():
        lyr = dock.registry.layer(key)
        if lyr is None:
            continue
        add("layer", lyr.name())
        r = lyr.renderer()
        if isinstance(r, QgsCategorizedSymbolRenderer):
            for cat in r.categories():
                add("legend", cat.label())
    return out


def strip_allowed(t):
    for a in ALLOWED:
        t = t.replace(a, "")
    return t


def run():
    win = QMainWindow()
    canvas = QgsMapCanvas(win)
    win.setCentralWidget(canvas)

    class Iface:
        def mapCanvas(self):
            return canvas

        def mainWindow(self):
            return win

    crs = QgsCoordinateReferenceSystem("EPSG:2180")
    canvas.setDestinationCrs(crs)
    dock = dm.QMapAnalystDockWidget(Iface())
    errors = []
    statuses = []
    orig_set_status = dock.set_status

    def set_status(text, level="info"):
        statuses.append(text)
        orig_set_status(text, level)
    dock.set_status = set_status
    orig_rep = dock.report_exception

    def report_exception(where, exc):
        errors.append((where, repr(exc)))
        orig_rep(where, exc)
    dock.report_exception = report_exception

    tmp = tempfile.mkdtemp(prefix=f"qma_i18n_{LANG}_")
    from osgeo import gdal
    gdal.UseExceptions()
    path = os.path.join(tmp, "scan.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 3000, 2400, 1, gdal.GDT_Byte)
    ds = None
    raster = QgsRasterLayer(path, "scan")
    dock.set_old_raster(raster)

    rng = np.random.default_rng(1)
    n = 20
    ll = np.column_stack([rng.uniform(15.0, 23.5, n), rng.uniform(49.5, 54.5, n)])
    bonne = pj.PROJECTIONS_BY_KEY["bonne"]
    x, y = bonne.func(np.radians(ll[:, 0]), np.radians(ll[:, 1]), {"lon0": math.radians(21.04), "lat1": math.radians(52)})
    P = np.column_stack([x, y])
    P = (P - P.min(0)) / np.ptp(P, axis=0).max() * 2800 + [100, -2700] + rng.normal(0, 1.0, (n, 2))
    P[4] += [40, -35]
    ct = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, QgsProject.instance())
    for (lo, la), (px, py) in zip(ll, P):
        r = ct.transform(QgsPointXY(lo, la))
        dock.add_gcp(px, py, r.x(), r.y())
    dock.gcps_changed()
    dock._recalc_timer.stop()

    texts = []
    for i in range(dock.cmb_transform_type.count()):
        dock.cmb_transform_type.setCurrentIndex(i)
        dock._recalc_timer.stop()
        dock.run_analysis()
        texts += [("report-" + dock.cmb_transform_type.currentData(), dock.txt_report.toPlainText())]
    dock.cmb_transform_type.setCurrentIndex(1)
    dock._recalc_timer.stop()
    dock.run_analysis()
    dock.spin_grid_step.setValue(50000)
    for fn in (dock.generate_gcp_points, dock.generate_result_layers, dock.generate_error_circles,
               dock.generate_distortion_grid, dock.generate_old_vectors, dock.generate_old_circles,
               dock.generate_historic_grid):
        fn()
    for k in ("scale", "rot", "omega"):
        dock.generate_isolines(k)
    dock.estimate_projection()
    dock.generate_graticule()
    dock.cmb_hyp_preset.setCurrentIndex(dock.cmb_hyp_preset.findData("tkkp"))
    dock.run_hypothesis()
    texts.append(("proj-hyp", dock.txt_proj.toPlainText()))
    for c in dock.projection.candidates:   # szczegóły każdego kandydata
        dock.tbl_proj.selectRow(dock.projection.candidates.index(c))
        texts.append(("proj-" + c.key, dock.txt_proj.toPlainText()))
    dock.render_report()
    texts.append(("report-full", dock.txt_report.toPlainText()))
    # błędy użytkownika i silnika
    dock.hyp_edits["lat1"].setText("xx")
    dock.run_hypothesis()
    dock.cmb_transform_type.setCurrentIndex(2)
    for g in dock.gcps[6:]:
        g.enabled = False
    dock.gcps_changed()
    dock._recalc_timer.stop()
    dock.run_analysis()
    for g in dock.gcps:
        g.enabled = True
    dock.cmb_transform_type.setCurrentIndex(1)
    # błąd silnika (punkty współliniowe) – komunikat ma być przetłumaczony
    st = me.EngineError("Układ równań jest osobliwy – punkty są współliniowe lub zdublowane.")
    statuses.append(dock.error_text(st))
    # eksport: pełny raport (PDF/TXT/HTML), mapy, CSV, TIFF
    dock.file_export_dir.setFilePath(tmp)
    dock.execute_export()
    files = sorted(os.listdir(tmp))
    base = dock.export_basename()
    for suffix, ext in (("raport", "pdf"), ("raport", "txt"), ("mapy", "pdf"), ("punkty", "csv"),
                        ("odwzorowania", "csv"), ("widok", "tif")):
        name = f"{base}_{dock.tr(suffix)}.{ext}"
        assert name in files, (name, files)
    txt = open(os.path.join(tmp, f"{base}_{dock.tr('raport')}.txt"), encoding="utf-8").read()
    texts.append(("report-txt", txt))
    texts.append(("report-html", dock.build_full_report_html(dict(__import__(f"{PKG}.report", fromlist=["*"]).LIGHT))))
    for tab in range(dock.tab_widget.count()):
        dock.tab_widget.setCurrentIndex(tab)

    texts += collect_texts(dock)
    texts += [("status", s) for s in statuses]

    problems = []
    for where, t in texts:
        if LANG == "en":
            tt = strip_allowed(t)
            m = PL_CHARS.search(tt) or PL_WORDS.search(tt)
            if m:
                problems.append((where, m.group(0), t[:140].replace("\n", " | ")))
    if LANG == "pl":
        rep = dict(texts)["report-full"] + "\n" + dict(texts)["report-txt"]
        if not re.search(r"\d,\d", rep):
            problems.append(("report-pl", "brak przecinka dziesiętnego", rep[:200]))
        bad = [m.group(0) for m in re.finditer(r"(?<![\w.])\d+\.\d+(?![\w.])", rep)]
        if bad:
            problems.append(("report-pl", "kropka dziesiętna", ", ".join(bad[:10])))
        cells = [t for w, t in texts if w == "cell"]
        if any(re.fullmatch(r"-?\d+\.\d+", c) for c in cells):
            problems.append(("cells-pl", "kropka dziesiętna w tabeli", ""))

    print(f"[{LANG}] zebrano {len(texts)} tekstów, problemów: {len(problems)}")
    seen = set()
    for p in problems:
        if p[2] in seen:
            continue
        seen.add(p[2])
        print("   ", p)
    sample = dict(texts).get("report-full", "")[:400].replace("\n", " | ")
    print(f"[{LANG}] raport: {sample}")
    print(f"[{LANG}] status: {statuses[-3:]}")
    assert not [e for e in errors if "EngineError" not in e[1]], errors

    dock.close()
    dock.cleanup()
    dock.deleteLater()
    QgsApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QgsProject.instance().clear()
    return problems


problems = run()
import gc
gc.collect()
qgs.exitQgis()
print("I18N OK" if not problems else "I18N PROBLEMS", flush=True)
sys.exit(1 if problems else 0)
