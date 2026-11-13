"""
Okno importu punktów do biblioteki: źródło (warstwa wektorowa projektu lub plik CSV), dopasowanie pól,
typ domyślny, tolerancja duplikatów, zestaw docelowy.
"""

import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsFeatureRequest, QgsProject,
    QgsVectorLayer,
)

from . import importer as im

NONE = "__none__"
CSV = "__csv__"
NEW_SET = "__new__"


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


def role_labels():
    return {
        "name": tr("Nazwa (wymagana):"), "alt_names": tr("Nazwy dawne / obce:"), "type": tr("Typ:"),
        "feature": tr("Wskazany obiekt:"), "sigma_m": tr("Niepewność współrzędnych [m]:"),
        "valid_from": tr("Istnieje od (rok):"), "valid_to": tr("Istnieje do (rok):"), "source": tr("Źródło:"),
        "note": tr("Uwagi:"),
    }


class ImportDialog(QDialog):
    """Import punktów do biblioteki panelu (LibraryPanel)."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.project = QgsProject.instance()
        self.csv_path = None
        self.csv_fields, self.csv_rows = [], []
        self.default_source = ""
        self.setWindowTitle(tr("Import punktów do biblioteki"))
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.addWidget(self._build_source())
        lay.addWidget(self._build_mapping())
        lay.addWidget(self._build_options())
        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        lay.addWidget(self.lbl_summary)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Importuj"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.on_source_changed()

    # --- budowa -------------------------------------------------------------
    def _build_source(self):
        box = QGroupBox(tr("Źródło"))
        form = QFormLayout(box)
        row = QHBoxLayout()
        self.cmb_source = QComboBox()
        for layer in self.project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.isSpatial() and layer is not self.panel.layer:
                self.cmb_source.addItem(layer.name(), layer.id())
        self.cmb_source.addItem(tr("Plik CSV…"), CSV)
        self.cmb_source.currentIndexChanged.connect(self.on_source_changed)
        self.btn_csv = QPushButton(tr("Wybierz plik…"))
        self.btn_csv.clicked.connect(self.choose_csv)
        row.addWidget(self.cmb_source, 1)
        row.addWidget(self.btn_csv)
        form.addRow(tr("Warstwa lub plik:"), row)
        self.chk_selected = QCheckBox(tr("Tylko zaznaczone obiekty"))
        self.chk_selected.toggled.connect(self.update_summary)
        form.addRow(self.chk_selected)
        self.cmb_x, self.cmb_y = QComboBox(), QComboBox()
        self.ed_crs = QLineEdit()
        self.ed_crs.setPlaceholderText("EPSG:4326")
        self.ed_crs.setToolTip(tr("Układ współrzędnych pliku CSV (kod EPSG lub WKT)"))
        for w in (self.cmb_x, self.cmb_y):
            w.currentIndexChanged.connect(self.update_summary)
        self.ed_crs.textChanged.connect(self.update_summary)
        xy = QHBoxLayout()
        xy.addWidget(QLabel("X:"))
        xy.addWidget(self.cmb_x, 1)
        xy.addWidget(QLabel("Y:"))
        xy.addWidget(self.cmb_y, 1)
        xy.addWidget(self.ed_crs)
        self.lbl_xy = QLabel(tr("Współrzędne CSV:"))
        form.addRow(self.lbl_xy, xy)
        return box

    def _build_mapping(self):
        box = QGroupBox(tr("Pola"))
        form = QFormLayout(box)
        self.cmb_roles = {}
        for role, label in role_labels().items():
            cb = QComboBox()
            cb.currentIndexChanged.connect(self.update_summary)
            self.cmb_roles[role] = cb
            form.addRow(label, cb)
        return box

    def _build_options(self):
        box = QGroupBox(tr("Opcje"))
        form = QFormLayout(box)
        from .library_panel import type_labels
        self.cmb_default_type = QComboBox()
        self.cmb_default_type.addItem(tr("(bez typu)"), "")
        for code, label in type_labels().items():
            self.cmb_default_type.addItem(label, code)
        self.cmb_default_type.setToolTip(tr("Typ punktów, gdy pole typu nie jest wskazane lub ma nierozpoznaną wartość"))
        form.addRow(tr("Typ domyślny:"), self.cmb_default_type)
        self.sp_tol = QDoubleSpinBox()
        geographic = self.panel.library_crs().isGeographic()
        self.sp_tol.setDecimals(6 if geographic else 1)
        self.sp_tol.setRange(0, 1 if geographic else 100000)
        self.sp_tol.setValue(0.0005 if geographic else 50.0)
        self.sp_tol.setSuffix(" °" if geographic else " m")
        self.sp_tol.setToolTip(tr("Punkt w tej odległości od istniejącego, o tej samej nazwie (także dawnej), "
                                  "jest duplikatem – nie zostanie dodany ponownie"))
        form.addRow(tr("Duplikaty w promieniu:"), self.sp_tol)
        self.chk_merge = QCheckBox(tr("Dopisz nowe nazwy dawne do istniejących punktów"))
        self.chk_merge.setChecked(True)
        form.addRow(self.chk_merge)
        row = QHBoxLayout()
        self.cmb_set = QComboBox()
        self.cmb_set.addItem(tr("(bez zestawu)"), None)
        self.cmb_set.addItem(tr("nowy zestaw:"), NEW_SET)
        for sid, name, _d, n in self.panel.sets_list():
            self.cmb_set.addItem(f"{name} ({n})", sid)
        cur = self.panel.current_set()
        if cur is not None:
            self.cmb_set.setCurrentIndex(self.cmb_set.findData(cur))
        self.ed_set = QLineEdit()
        self.ed_set.setPlaceholderText(tr("nazwa nowego zestawu"))
        self.cmb_set.currentIndexChanged.connect(lambda *_a: self.ed_set.setEnabled(self.cmb_set.currentData() == NEW_SET))
        self.ed_set.setEnabled(False)
        row.addWidget(self.cmb_set, 1)
        row.addWidget(self.ed_set, 1)
        form.addRow(tr("Dodaj do zestawu:"), row)
        return box

    # --- źródło ----------------------------------------------------------------
    def source_layer(self):
        lid = self.cmb_source.currentData()
        return self.project.mapLayer(lid) if lid not in (CSV, None) else None

    def source_fields(self):
        layer = self.source_layer()
        if layer is not None:
            return [f.name() for f in layer.fields()]
        return list(self.csv_fields)

    def choose_csv(self):
        path, _f = QFileDialog.getOpenFileName(self, tr("Plik CSV z punktami"), "", "CSV (*.csv *.txt *.tsv)")
        if path:
            self.load_csv(path)

    def load_csv(self, path):
        self.csv_path = path
        self.csv_fields, self.csv_rows = im.read_csv_file(path)
        self.cmb_source.setCurrentIndex(self.cmb_source.findData(CSV))
        self.on_source_changed()

    def on_source_changed(self, *args):
        is_csv = self.cmb_source.currentData() == CSV
        layer = self.source_layer()
        self.btn_csv.setVisible(is_csv)
        has_sel = layer is not None and layer.selectedFeatureCount() > 0
        self.chk_selected.blockSignals(True)   # podsumowanie liczone niżej, po ustawieniu pól
        self.chk_selected.setEnabled(has_sel)
        self.chk_selected.setChecked(has_sel)
        self.chk_selected.blockSignals(False)
        for w in (self.lbl_xy, self.cmb_x, self.cmb_y, self.ed_crs):
            w.setVisible(is_csv)
        fields = self.source_fields()
        self._fill_fields(fields)
        default_source = layer.name() if layer is not None else (os.path.basename(self.csv_path) if self.csv_path else "")
        self.default_source = default_source
        self.update_summary()

    def _fill_fields(self, fields):
        combos = list(self.cmb_roles.items()) + [("x", self.cmb_x), ("y", self.cmb_y)]
        for role, cb in combos:
            cb.blockSignals(True)
            cb.clear()
            cb.addItem(tr("(brak)"), NONE)
            for f in fields:
                cb.addItem(f, f)
            guess = im.guess_field(fields, role)
            if guess is not None:
                cb.setCurrentIndex(cb.findData(guess))
            cb.blockSignals(False)

    # --- rekordy -----------------------------------------------------------------
    def mapping(self):
        return {role: (cb.currentData() if cb.currentData() != NONE else None) for role, cb in self.cmb_roles.items()}

    def _layer_records(self, layer):
        ct = QgsCoordinateTransform(layer.crs(), self.panel.library_crs(), self.project)
        request = QgsFeatureRequest()
        if self.chk_selected.isChecked():
            request.setFilterFids(layer.selectedFeatureIds())
        names = [f.name() for f in layer.fields()]
        out = []
        for f in layer.getFeatures(request):
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            single_point = g.type() == Qgis.GeometryType.Point and not g.isMultipart()
            pt = g.asPoint() if single_point else g.pointOnSurface().asPoint()
            try:
                pt = ct.transform(pt)
            except QgsCsException:
                continue
            rec = {n: f[n] for n in names}
            rec[im.X_KEY], rec[im.Y_KEY] = pt.x(), pt.y()
            out.append(rec)
        return out

    def _csv_records(self):
        fx, fy = self.cmb_x.currentData(), self.cmb_y.currentData()
        crs = QgsCoordinateReferenceSystem(self.ed_crs.text().strip() or "EPSG:4326")
        if fx == NONE or fy == NONE or not crs.isValid():
            return []
        ct = QgsCoordinateTransform(crs, self.panel.library_crs(), self.project)
        out = []
        for row in self.csv_rows:
            x, y = im.parse_number(row.get(fx)), im.parse_number(row.get(fy))
            if x is None or y is None:
                continue
            try:
                pt = ct.transform(x, y)
            except QgsCsException:
                continue
            rec = dict(row)
            rec[im.X_KEY], rec[im.Y_KEY] = pt.x(), pt.y()
            out.append(rec)
        return out

    def records(self):
        layer = self.source_layer()
        return self._layer_records(layer) if layer is not None else self._csv_records()

    def points(self):
        return im.build_points(self.records(), self.mapping(), self.cmb_default_type.currentData(), self.default_source)

    def update_summary(self, *args):
        points, skipped = self.points()
        text = tr("Punkty do importu: {n}.").format(n=len(points))
        if skipped:
            text += " " + tr("Pominięte (bez nazwy lub współrzędnych): {n}.").format(n=skipped)
        self.lbl_summary.setText(text)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok) if hasattr(self, "buttons") else None
        if ok is not None:
            ok.setEnabled(bool(points))

    # --- wynik --------------------------------------------------------------------
    def target_set(self):
        """(set_id lub None, nazwa nowego zestawu lub None)."""
        data = self.cmb_set.currentData()
        if data == NEW_SET:
            name = self.ed_set.text().strip()
            return None, (name or None)
        return data, None

    def accept(self):
        points, _skipped = self.points()
        if not points:
            return
        set_id, new_set = self.target_set()
        result = self.panel.import_points(points, self.sp_tol.value(), set_id, new_set, self.chk_merge.isChecked())
        if result is not None:
            self.result_stats = result
            super().accept()

