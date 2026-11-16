"""
Seria czasowa – zakładka „Pola korekcji” (#5) i okno „Rektyfikacja serii”.

Pola korekcji: dla każdej pary map korelacja wektorowa pól (istotność – losowy obrót pola), statystyka
Mantela, korelacja lokalnej dylatacji i obrotu, warstwa wektorów różnic pól.
Rektyfikacja serii: każda mapa tą samą metodą (wygładzanie z walidacji krzyżowej osobno dla każdej mapy),
wydawnictwa wieloarkuszowe – zszyte węzłami siatki, grupa warstw w projekcie w kolejności lat.
"""

import math
import os

import numpy as np
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)
from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer

from . import block as bk
from . import library as lb
from . import publication_rectify as pr
from . import rectify as rc
from . import series_fields as sf
from .rectify_dialog import format_labels, method_labels, nice_resolution
from .series_dialog import _REAL, _STR, _item, _num, _table
from .tasks import run_task


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


def warning_text(code):
    return {"too_few_points": tr("za mało punktów na jednej z map"),
            "no_common_area": tr("brak wspólnego obszaru punktów"),
            "small_common_area": tr("za mały wspólny obszar")}.get(code, "")


class FieldsPanel(QWidget):
    def __init__(self, dialog, parent=None):
        super().__init__(parent)
        self.dialog = dialog
        self.series = None
        self.results = []
        lay = QVBoxLayout(self)
        hint = QLabel(tr("Pole korekcji mapy – wygładzony TPS jej reszt (jak w rektyfikacji). Podobne pola dwóch map to "
                         "drugi, obok korelacji reszt w punktach, ślad kopiowania. Istotność: losowy obrót pola drugiej "
                         "mapy (zachowuje jego gładkość)."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(hint)
        self.tbl = _table([tr("Mapa wcześniejsza"), tr("Mapa późniejsza"), tr("Węzły"), tr("r pól"), "p",
                           tr("Mantel r"), "p", tr("r dylatacji"), tr("r obrotu"), tr("Uwagi")])
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.tbl, 1)
        row = QHBoxLayout()
        self.btn_run = QPushButton(tr("Porównaj pola korekcji"))
        self.btn_run.clicked.connect(self.compute)
        self.btn_diff = QPushButton(tr("Warstwa różnic pól"))
        self.btn_diff.setToolTip(tr("Dla zaznaczonej pary: wektory c_A − c_B w węzłach wspólnego obszaru"))
        self.btn_diff.clicked.connect(self.add_diff_layer)
        self.btn_diff.setEnabled(False)
        row.addWidget(self.btn_run)
        row.addWidget(self.btn_diff)
        row.addStretch()
        lay.addLayout(row)

    def set_series(self, ser):
        self.series = ser
        self.results = []
        self.tbl.setRowCount(0)
        self.btn_diff.setEnabled(False)

    def compute(self):
        if self.series is None:
            return []
        self.results = sf.compare_series_fields(self.series)
        by = {m.key: m for m in self.series.maps}
        t = self.tbl
        t.setRowCount(len(self.results))
        for r, c in enumerate(self.results):
            sig = c.ok and math.isfinite(c.p_field) and c.p_field < 0.05 and c.r_field > 0
            cells = [_item(self.dialog._label(by[c.a]), align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                     _item(self.dialog._label(by[c.b]), align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                     _item(str(c.nodes)), _item(_num(c.r_field, 2)), _item(_num(c.p_field, 3)), _item(_num(c.mantel_r, 2)),
                     _item(_num(c.mantel_p, 3)), _item(_num(c.r_dilation, 2)), _item(_num(c.r_rotation, 2)),
                     _item(warning_text(c.warning), align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)]
            if sig:
                for it in cells[:5]:
                    it.setBackground(QBrush(QColor(220, 50, 50, 50)))
            for k, it in enumerate(cells):
                t.setItem(r, k, it)
        self.btn_diff.setEnabled(any(c.diff is not None for c in self.results))
        return self.results

    def add_diff_layer(self):
        row = self.tbl.currentRow()
        if not 0 <= row < len(self.results) or self.results[row].diff is None:
            row = next((k for k, c in enumerate(self.results) if c.diff is not None), -1)
        if row < 0:
            return None
        c = self.results[row]
        by = {m.key: m for m in self.series.maps}
        name = tr("Różnica pól korekcji: {a} − {b}").format(a=self.dialog._label(by[c.a]), b=self.dialog._label(by[c.b]))
        lyr = QgsVectorLayer("LineString", name, "memory")
        crs = self.dialog.panel.library_crs()
        if crs.isValid():
            lyr.setCrs(crs)
        lyr.dataProvider().addAttributes([QgsField("diff_m", _REAL), QgsField("pair", _STR)])
        lyr.updateFields()
        unit = lb.crs_metres_per_unit(crs.toWkt()) if crs.isValid() else 1.0
        feats = []
        for (x, y), (dx, dy) in zip(c.grid, c.diff):
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y), QgsPointXY(x + dx, y + dy)]))
            f.setAttributes([round(float(np.hypot(dx, dy)) * unit, 1), f"{c.a}-{c.b}"])
            feats.append(f)
        lyr.dataProvider().addFeatures(feats)
        lyr.updateExtents()
        from .layers import style_arrows
        style_arrows(lyr, QColor(120, 60, 170), 0.4)
        QgsProject.instance().addMapLayer(lyr)
        return lyr


# ---------------------------------------------------------------------------
#  Rektyfikacja serii
# ---------------------------------------------------------------------------
def scan_frame(scan):
    """Rogi całego skanu we współrzędnych skanu QGIS (bez georeferencji: y = −linia)."""
    from osgeo import gdal
    ds = gdal.Open(scan)
    if ds is None:
        return None
    W, H = ds.RasterXSize, ds.RasterYSize
    gt = ds.GetGeoTransform(can_return_null=True)
    ds = None
    if gt is None:
        return [(0, 0), (W, 0), (W, -H), (0, -H)]
    return [gdal.ApplyGeoTransform(gt, px, ln) for px, ln in ((0, 0), (W, 0), (W, H), (0, H))]


def series_map_sheets(lib, analysis):
    """Arkusze mapy elementu serii (BlockSheet); arkusze bez ramki – ramka = cały skan."""
    sheets, skipped = bk.sheets_from_library(lib, analysis.map_id)
    for a in skipped:
        frame = scan_frame(a.scan_path) if a.scan_path and os.path.exists(a.scan_path) else None
        if frame is None:
            continue
        links = [ln for ln in lib.links(a.analysis_id) if ln.computable and ln.enabled]
        to_lib = lb.crs_transformer(a.ref_crs, lib.crs_wkt)
        dst = [to_lib(ln.ref_x, ln.ref_y) if to_lib else (ln.ref_x, ln.ref_y) for ln in links]
        sheets.append(bk.BlockSheet(a.analysis_id, a.sheet or f"#{a.analysis_id}", frame,
                                    [(ln.x_old, ln.y_old) for ln in links], dst, [ln.uid or "" for ln in links],
                                    [ln.name or "" for ln in links], analysis=a))
    return sheets


def _resolution(sheets):
    vals = []
    for s in sheets:
        if len(s.src) >= 3:
            f = np.linalg.lstsq(np.column_stack([np.ones(len(s.src)), s.src]), s.dst, rcond=None)[0]
            vals.append(math.sqrt(abs(f[1, 0] * f[2, 1] - f[2, 0] * f[1, 1])))
    return nice_resolution(float(np.median(vals))) if vals else 1.0


class SeriesRectifyDialog(QDialog):
    def __init__(self, dialog, parent=None):
        super().__init__(parent or dialog)
        self.dialog = dialog
        self.outputs = []
        self.task = None
        self.background = True
        self.setWindowTitle(tr("Rektyfikacja serii czasowej"))
        self.resize(520, 300)
        lay = QVBoxLayout(self)
        info = QLabel(tr("Każda mapa serii tą samą metodą; wygładzanie dobierane walidacją krzyżową osobno dla każdej mapy. "
                         "Wydawnictwa wieloarkuszowe zszywane węzłami siatki. Warstwy w grupie projektu w kolejności lat."))
        info.setWordWrap(True)
        lay.addWidget(info)
        form = QFormLayout()
        self.cmb_method = QComboBox()
        for key, label in method_labels().items():
            self.cmb_method.addItem(label, key)
        form.addRow(tr("Metoda"), self.cmb_method)
        self.cmb_format = QComboBox()
        for key, label in format_labels().items():
            self.cmb_format.addItem(label, key)
        form.addRow(tr("Format"), self.cmb_format)
        row = QHBoxLayout()
        self.ed_dir = QLineEdit(os.path.join(os.path.expanduser("~"), "seria_rektyfikacja"))
        btn = QPushButton("…")
        btn.clicked.connect(self.browse)
        row.addWidget(self.ed_dir, 1)
        row.addWidget(btn)
        form.addRow(tr("Katalog wynikowy"), row)
        self.chk_add = QCheckBox(tr("Dodaj do projektu"))
        self.chk_add.setChecked(True)
        form.addRow("", self.chk_add)
        lay.addLayout(form)
        self.lbl = QLabel()
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        lay.addWidget(self.progress)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.btn_run = bb.addButton(tr("Rektyfikuj"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_run.clicked.connect(self.run)
        self.btn_cancel = bb.addButton(tr("Przerwij"), QDialogButtonBox.ButtonRole.ActionRole)
        self.btn_cancel.clicked.connect(self.cancel)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._set_running(False)

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, tr("Katalog wynikowy"), self.ed_dir.text())
        if path:
            self.ed_dir.setText(path)

    def run(self):
        """Mapy serii w tle (QgsTask); background=False – od razu (testy), zwraca [(etykieta, ścieżka)]."""
        ser = self.dialog.series
        if ser is None or not ser.maps or self.task is not None:
            return []
        out_root = self.ed_dir.text().strip()
        method = self.cmb_method.currentData()
        model = "helmert" if ser.model == "helmert" else "affine"
        crs = self.dialog.panel.library_crs()
        crs_text = crs.authid() or crs.toWkt()
        fmt = self.cmb_format.currentData()
        try:
            with self.dialog.panel.library() as lib:
                jobs = [(self.dialog._label(m), series_map_sheets(lib, m.analysis)) for m in ser.maps]
        except lb.LibraryError as e:
            self.lbl.setText(self.dialog.panel._library_error(e))
            return []

        def job(progress, canceled):
            done, problems = [], []
            for k, (label, sheets) in enumerate(jobs):
                if canceled():
                    break
                progress(k / len(jobs), tr("Mapa {k} z {n}: {m}").format(k=k + 1, n=len(jobs), m=label))
                result = rectify_series_map(label, sheets, method, model, out_root, crs_text, fmt, problems,
                                            lambda f, k=k: progress((k + f) / len(jobs), label))
                if result:
                    done.append((label, result))
            return done, problems
        self._set_running(True)
        self.task = run_task(tr("Rektyfikacja serii"), job, self._done, self.background, self._on_progress)
        return self.outputs if not self.background else []

    def _set_running(self, on):
        self.progress.setVisible(on)
        self.progress.setValue(0)
        self.btn_run.setEnabled(not on)
        self.btn_cancel.setVisible(on)

    def _on_progress(self, frac, text):
        self.progress.setValue(int(1000 * frac))
        if text:
            self.lbl.setText(text)

    def cancel(self):
        if self.task is not None:
            self.task.cancel()

    def reject(self):
        if self.task is not None:
            self.cancel()
        super().reject()

    def _done(self, result, error):
        self.task = None
        self._set_running(False)
        if error is not None:
            self.lbl.setText(tr("Przerwano.") if isinstance(error, InterruptedError) else str(error))
            return
        done, problems = result
        self.outputs = done
        if self.chk_add.isChecked() and done:
            self._add_group(done)
        msg = tr("Zrektyfikowano map: {n}.").format(n=len(done))
        if problems:
            msg += " " + tr("Pominięte: {s}.").format(s="; ".join(problems))
        self.lbl.setText(msg)

    def _rectify_map(self, label, sheets, method, model, out_root, crs_text, problems):
        return rectify_series_map(label, sheets, method, model, out_root, crs_text, self.cmb_format.currentData(), problems)

    def _add_group(self, done):
        project = QgsProject.instance()
        group = project.layerTreeRoot().insertGroup(0, tr("Seria „{s}” – rektyfikacja").format(s=self.dialog.series.set_name))
        for label, path in reversed(done):                 # u góry najnowsza mapa
            lyr = QgsRasterLayer(path, label)
            if lyr.isValid():
                project.addMapLayer(lyr, False)
                group.addLayer(lyr)
        return group


def suspect_text(ser):
    sus = sf.suspect_points(ser)
    if not sus:
        return ""
    return tr("Punkty podejrzane – duży błąd na wszystkich mapach w zgodnym kierunku (sprawdź współrzędne w bibliotece "
              "albo zmianę obiektu): {p}.").format(p=", ".join(f"{s.name} ({s.maps} map, {s.mean_m:.0f} m)" for s in sus[:8]))



def rectify_series_map(label, sheets, method, model, out_root, crs_text, fmt, problems, progress=None):
    """Rektyfikacja jednej mapy serii (wątek roboczy – bez interfejsu). Zwraca ścieżkę rastra/mozaiki albo None."""
    scans = {s.key: s.analysis.scan_path for s in sheets if s.analysis is not None and s.analysis.scan_path}
    if not sheets or not any(os.path.exists(p) for p in scans.values()):
        problems.append(tr("{m} – brak skanu").format(m=label))
        return None
    stitch = pr.STITCH_NODES if len(sheets) > 1 and all(s.grid is not None for s in sheets) else pr.STITCH_BLOCK
    try:
        plan = pr.build_plan(sheets, method, None, stitch, model, 0.0 if len(sheets) == 1 else None)
        out_dir = os.path.join(out_root, pr.safe_name(label))
        res = _resolution(sheets)
        cb = (lambda k, n, f: progress((k + f) / max(n, 1))) if progress else None
        paths = pr.warp_sheets(plan, scans, out_dir, crs_text, res, fmt, cb)
    except (bk.BlockError, rc.RectifyError, RuntimeError) as e:
        problems.append(f"{label} – {e}")
        return None
    if not paths:
        return None
    return paths[0] if len(paths) == 1 else pr.write_mosaic(plan, os.path.join(out_dir, "mozaika.vrt"), res)
