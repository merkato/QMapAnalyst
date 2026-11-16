"""
Okno „Rektyfikacja wydawnictwa”: wszystkie arkusze mapy jako jedna mozaika – zszycie (węzły siatki albo
wyrównanie blokowe), pole korekcji lokalnej w terenie, przycięcie do ramek, priorytet arkuszy i suwak
„przycinanie ↔ blendowanie” przy zakładkach, mozaika VRT, opcjonalnie mozaika COG z blendowaniem,
zapis siatki i zasięgów w bibliotece.
"""

import math
import os

import numpy as np
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressDialog, QPushButton,
    QSlider, QVBoxLayout,
)
from qgis.core import QgsProject, QgsRasterLayer

from . import block as bk
from . import library as lb
from . import publication_rectify as pr
from . import rectify as rc
from .rectify_dialog import _m, format_labels, method_labels, nice_resolution


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


class PublicationRectifyDialog(QDialog):
    def __init__(self, panel, sheets, map_info, unit_m=1.0, seam_weight=None, model="bilinear", parent=None):
        super().__init__(parent)
        self.panel = panel
        self.sheets = [s for s in sheets if s.key]          # bez niezapisanego stanu bieżącego
        self.map = map_info
        self.unit_m = unit_m
        self.seam_weight = seam_weight
        self.model = model
        self.plan = None
        self.outputs = []
        self.setWindowTitle(tr("Rektyfikacja wydawnictwa wieloarkuszowego"))
        self.resize(620, 640)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.recompute)
        self._build()
        self.recompute()

    # --- interfejs ---------------------------------------------------------------
    def _build(self):
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.cmb_stitch = QComboBox()
        self.cmb_stitch.addItem(tr("Węzły siatki – styki bez szczelin"), pr.STITCH_NODES)
        self.cmb_stitch.addItem(tr("Wyrównanie blokowe (suwak z zakładki) – arkusze zachodzące na siebie"), pr.STITCH_BLOCK)
        if any(s.grid is None for s in self.sheets):
            self.cmb_stitch.setCurrentIndex(1)
        self.cmb_stitch.currentIndexChanged.connect(lambda _i: self._timer.start())
        form.addRow(tr("Zszycie"), self.cmb_stitch)
        self.cmb_method = QComboBox()
        for key, label in method_labels().items():
            self.cmb_method.addItem(label, key)
        self.cmb_method.currentIndexChanged.connect(self.on_method_changed)
        form.addRow(tr("Korekcja"), self.cmb_method)
        row = QHBoxLayout()
        self.sld_smooth = QSlider(Qt.Orientation.Horizontal)
        self.sld_smooth.setRange(0, 100)
        self.sld_smooth.setValue(50)
        self.sld_smooth.setEnabled(False)
        self.sld_smooth.valueChanged.connect(lambda _v: self._timer.start())
        self.chk_auto = QCheckBox(tr("auto"))
        self.chk_auto.setChecked(True)
        self.chk_auto.toggled.connect(self.on_method_changed)
        row.addWidget(QLabel(tr("luźno")))
        row.addWidget(self.sld_smooth, 1)
        row.addWidget(QLabel(tr("sztywno")))
        row.addWidget(self.chk_auto)
        form.addRow(tr("Wygładzanie"), row)
        row = QHBoxLayout()
        self.sld_blend = QSlider(Qt.Orientation.Horizontal)
        self.sld_blend.setRange(0, 100)
        self.sld_blend.setValue(0)
        self.sld_blend.setToolTip(tr("Lewy koniec – strefa zakładki należy do arkusza o wyższym priorytecie; w prawo – "
                                     "szersze płynne przejście między arkuszami"))
        self.sld_blend.valueChanged.connect(self.on_blend_changed)
        self.lbl_blend = QLabel()
        self.lbl_blend.setMinimumWidth(90)
        row.addWidget(QLabel(tr("przycinanie")))
        row.addWidget(self.sld_blend, 1)
        row.addWidget(QLabel(tr("blendowanie")))
        row.addWidget(self.lbl_blend)
        form.addRow(tr("Zakładki"), row)
        self.lst_priority = QListWidget()
        self.lst_priority.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.lst_priority.setMaximumHeight(120)
        self.lst_priority.setToolTip(tr("Kolejność arkuszy przy zakładkach – przeciągnij; u góry arkusz najważniejszy"))
        for i in pr.priority_order(self.sheets, {s.key: getattr(s.analysis, "priority", None) for s in self.sheets}):
            it = QListWidgetItem(self.sheets[i].label)
            it.setData(Qt.ItemDataRole.UserRole, self.sheets[i].key)
            self.lst_priority.addItem(it)
        self.lst_priority.model().rowsMoved.connect(lambda *a: self._timer.start())
        form.addRow(tr("Priorytet"), self.lst_priority)
        self.lbl_stats = QLabel()
        self.lbl_stats.setWordWrap(True)
        form.addRow(tr("Wynik"), self.lbl_stats)
        self.spin_res = QDoubleSpinBox()
        self.spin_res.setDecimals(3)
        self.spin_res.setRange(0.001, 1e6)
        self.spin_res.setValue(self._default_resolution())
        self.spin_res.setSuffix(" /px")
        form.addRow(tr("Rozdzielczość"), self.spin_res)
        self.cmb_format = QComboBox()
        for key, label in format_labels().items():
            self.cmb_format.addItem(label, key)
        form.addRow(tr("Format"), self.cmb_format)
        row = QHBoxLayout()
        self.ed_dir = QLineEdit(self._default_dir())
        btn = QPushButton("…")
        btn.clicked.connect(self.browse)
        row.addWidget(self.ed_dir, 1)
        row.addWidget(btn)
        form.addRow(tr("Katalog wynikowy"), row)
        self.chk_add = QCheckBox(tr("Dodaj do projektu"))
        self.chk_add.setChecked(True)
        self.chk_composite = QCheckBox(tr("Zapisz też mozaikę COG z blendowaniem"))
        self.chk_save = QCheckBox(tr("Zapisz siatkę, priorytet i zasięgi arkuszy w bibliotece"))
        self.chk_save.setChecked(True)
        for c in (self.chk_add, self.chk_composite, self.chk_save):
            form.addRow("", c)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.btn_run = bb.addButton(tr("Rektyfikuj"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_run.clicked.connect(self.run)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _default_resolution(self):
        vals = []
        for s in self.sheets:
            if len(s.src) >= 3:
                try:
                    f = np.linalg.lstsq(np.column_stack([np.ones(len(s.src)), s.src]), s.dst, rcond=None)[0]
                    vals.append(math.sqrt(abs(f[1, 0] * f[2, 1] - f[2, 0] * f[1, 1])))
                except np.linalg.LinAlgError:
                    continue
        return nice_resolution(float(np.median(vals))) if vals else 1.0

    def _default_dir(self):
        scans = [s.analysis.scan_path for s in self.sheets if s.analysis is not None and s.analysis.scan_path]
        base = os.path.dirname(scans[0]) if scans else os.path.expanduser("~")
        return os.path.join(base, pr.safe_name(self.map.title or "wydawnictwo") + "_rektyfikacja")

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, tr("Katalog wynikowy"), self.ed_dir.text())
        if path:
            self.ed_dir.setText(path)

    def on_method_changed(self, *_a):
        tps = self.cmb_method.currentData() == "tps"
        self.chk_auto.setEnabled(tps)
        self.sld_smooth.setEnabled(tps and not self.chk_auto.isChecked())
        self._timer.start()

    def blend_width(self):
        """Szerokość przejścia [jednostki układu]: suwak 0…100 → 0 … 10 % mediany szerokości arkusza."""
        if self.plan is None or not self.plan.sheets:
            return 0.0
        widths = [float(np.hypot(*(sp.ring[96] - sp.ring[0]))) for sp in self.plan.sheets]
        return self.sld_blend.value() / 100.0 * 0.1 * float(np.median(widths))

    def on_blend_changed(self, _v=None):
        w = self.blend_width()
        self.lbl_blend.setText(tr("przycięcie") if w <= 0 else f"{_m(w * self.unit_m)} m")
        self.chk_composite.setEnabled(w > 0 and bool(self.plan and self.plan.overlaps()))

    def priorities(self):
        return {self.lst_priority.item(k).data(Qt.ItemDataRole.UserRole): k for k in range(self.lst_priority.count())}

    # --- obliczenia --------------------------------------------------------------
    def recompute(self):
        method = self.cmb_method.currentData()
        lam = None if (method != "tps" or self.chk_auto.isChecked()) else rc.slider_to_lambda(self.sld_smooth.value())
        try:
            self.plan = pr.build_plan(self.sheets, method, lam, self.cmb_stitch.currentData(), self.model,
                                      self.seam_weight, self.priorities(), self.unit_m)
        except (bk.BlockError, rc.RectifyError) as e:
            self.plan = None
            self.lbl_stats.setText(tr("Nie można zbudować planu: {e}").format(e=e))
            self.btn_run.setEnabled(False)
            return
        self.btn_run.setEnabled(True)
        c = self.plan.correction
        if method == "tps" and c.auto:
            self.sld_smooth.blockSignals(True)
            self.sld_smooth.setValue(int(round(rc.lambda_to_slider(c.lam))))
            self.sld_smooth.blockSignals(False)
        f = self.unit_m
        lines = [tr("RMSE przed korekcją {a} m → po korekcji {b} m").format(a=_m(c.rmse_before * f), b=_m(c.rmse_after * f))]
        if math.isfinite(c.loo_rmse):
            lines.append(tr("walidacja krzyżowa {v} m").format(v=_m(c.loo_rmse * f)))
        ov = self.plan.overlaps()
        lines.append(tr("Zakładki arkuszy: {n} par.").format(n=len(ov)) if ov else tr("Arkusze stykają się bez zakładek."))
        missing = [s.label for s in self.sheets if not (s.analysis and s.analysis.scan_path and os.path.exists(s.analysis.scan_path))]
        if missing:
            lines.append("<b>" + tr("Brak pliku skanu: {s} – arkusze zostaną pominięte.").format(s=", ".join(missing)) + "</b>")
        self.lbl_stats.setText("<br>".join(lines))
        self.sld_blend.setEnabled(bool(ov))
        self.on_blend_changed()

    # --- przetworzenie -----------------------------------------------------------
    def run(self):
        if self.plan is None:
            return []
        out_dir = self.ed_dir.text().strip()
        if not out_dir:
            return []
        scans = {s.key: s.analysis.scan_path for s in self.sheets if s.analysis is not None}
        crs = self.panel.library_crs()
        crs_text = crs.authid() or crs.toWkt()
        prog = QProgressDialog(tr("Przetwarzanie arkuszy…"), tr("Przerwij"), 0, 1000, self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(300)

        def progress(k, n, frac):
            prog.setValue(int(1000 * (k + frac) / max(n, 1)))
            prog.setLabelText(tr("Arkusz {k} z {n}…").format(k=k + 1, n=n))
            QCoreApplication.processEvents()
            return not prog.wasCanceled()
        try:
            self.outputs = pr.warp_sheets(self.plan, scans, out_dir, crs_text, self.spin_res.value(),
                                          self.cmb_format.currentData(), progress)
            mosaic = pr.write_mosaic(self.plan, os.path.join(out_dir, pr.safe_name(self.map.title or "mozaika") + ".vrt"),
                                     self.spin_res.value()) if self.outputs else None
            width = self.blend_width()
            layers = pr.write_blend_vrts(self.plan, width) if self.outputs else []
            blended = None
            if self.chk_composite.isEnabled() and self.chk_composite.isChecked() and layers:
                blended = pr.composite(layers, os.path.join(out_dir, pr.safe_name(self.map.title or "mozaika") + "_blend.tif"),
                                       fmt=self.cmb_format.currentData())
        except RuntimeError as e:
            prog.close()
            QMessageBox.warning(self, tr("Rektyfikacja wydawnictwa"), tr("Nie udało się przetworzyć arkuszy: {e}").format(e=e))
            return []
        prog.close()
        if self.chk_save.isChecked():
            self._save_library()
        if self.chk_add.isChecked() and self.outputs:
            self._add_to_project(mosaic, layers, blended)
        self.lbl_stats.setText(self.lbl_stats.text() + "<br>" + tr("Zapisano {n} arkuszy w katalogu {d}.").format(
            n=len(self.outputs), d=out_dir))
        return self.outputs

    def _save_library(self):
        prio = self.priorities()
        layout = {s.key: (s.grid[0] if s.grid else None, s.grid[1] if s.grid else None, prio.get(s.key)) for s in self.sheets}
        label = self.cmb_method.currentData() + ("" if self.plan.stitch == pr.STITCH_NODES else f"+block:{self.model}")
        try:
            with self.panel.library() as lib:
                lib.set_sheet_layout(layout)
                lib.save_footprints(self.map.map_id, pr.footprints_items(self.plan, label))
        except lb.LibraryError as e:
            QMessageBox.warning(self, tr("Rektyfikacja wydawnictwa"), self.panel._library_error(e))

    def _add_to_project(self, mosaic, layers, blended):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        title = self.map.title or tr("wydawnictwo")
        group = root.insertGroup(0, tr("{t} – rektyfikacja").format(t=title))
        if blended:
            lyr = QgsRasterLayer(blended, tr("{t} – mozaika z blendowaniem").format(t=title))
            if lyr.isValid():
                project.addMapLayer(lyr, False)
                group.addLayer(lyr)
        if self.plan.overlaps() and self.blend_width() > 0:
            for path, sp in zip(layers, [sp for sp in self.plan.sheets if sp.output]):   # u góry – najważniejszy
                lyr = QgsRasterLayer(path, sp.sheet.label)
                if lyr.isValid():
                    project.addMapLayer(lyr, False)
                    group.addLayer(lyr)
        elif mosaic:
            lyr = QgsRasterLayer(mosaic, tr("{t} – mozaika").format(t=title))
            if lyr.isValid():
                project.addMapLayer(lyr, False)
                group.addLayer(lyr)
