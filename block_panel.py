"""
Zakładka „Wyrównanie blokowe” okna wydawnictwa wieloarkuszowego (#8): model arkusza, suwak
„niezależne ↔ zszyte”, σ₀ i RMSE arkuszy, przesunięcie arkusza względem sąsiadów, arkusz odstający,
szczeliny na stykach, krzywa RMSE ↔ szczelina i warstwy (zasięgi po wyrównaniu, wektory szczelin).
"""

import math

from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QSplitter, QVBoxLayout, QWidget,
)
from qgis.core import (
    QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsPalLayerSettings, QgsPointXY, QgsProject,
    QgsSingleSymbolRenderer, QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling,
)

from . import block as bk
from . import charts as ch
from . import library as lb
from . import series_report as srep
from .series_dialog import _INT, _REAL, _STR, _item, _num, _table


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


def model_labels():
    return {"bilinear": tr("Biliniowa w ramce (8 parametrów)"), "affine": tr("Afiniczna (6 parametrów)"),
            "helmert": tr("Helmert (4 parametry)")}


class BlockPanel(QWidget):
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.sheets, self.skipped, self.result, self.curve = [], [], None, []
        self.unit_m, self.chart_svg = 1.0, None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.recompute)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Model arkusza:")))
        self.cmb_model = QComboBox()
        for key, label in model_labels().items():
            self.cmb_model.addItem(label, key)
        self.cmb_model.currentIndexChanged.connect(self.on_model_changed)
        top.addWidget(self.cmb_model)
        top.addSpacing(16)
        top.addWidget(QLabel(tr("niezależne")))
        self.sld_seam = QSlider(Qt.Orientation.Horizontal)
        self.sld_seam.setRange(0, 100)
        self.sld_seam.setValue(bk.SEAM_SLIDER_DEFAULT)
        self.sld_seam.setToolTip(tr("Waga warunków zgodności wspólnych narożników ramek względem punktów dopasowania: "
                                    "w lewo – arkusze wpasowane osobno, w prawo – styki domknięte"))
        self.sld_seam.valueChanged.connect(lambda _v: self._timer.start())
        top.addWidget(self.sld_seam, 1)
        top.addWidget(QLabel(tr("zszyte")))
        self.lbl_weight = QLabel()
        self.lbl_weight.setMinimumWidth(70)
        top.addWidget(self.lbl_weight)
        lay.addLayout(top)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tbl = _table([tr("Arkusz"), tr("Siatka"), tr("Punkty"), "σ₀ [m]", "RMSE [m]",
                           tr("Przesunięcie wzgl. sąsiadów [m]"), tr("Odstający")])
        self.tbl.itemChanged.connect(self.on_item_changed)
        self._filling = False
        split.addWidget(self.tbl)
        self.chart = srep.SvgChart()
        self.chart.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        split.addWidget(self.chart)
        split.setSizes([520, 380])
        lay.addWidget(split, 1)
        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)
        row = QHBoxLayout()
        self.btn_layers = QPushButton(tr("Zasięgi i szczeliny na mapie"))
        self.btn_layers.setToolTip(tr("Warstwy: zasięgi arkuszy po wyrównaniu blokowym i wektory szczelin na stykach "
                                      "oraz w punktach wiążących"))
        self.btn_layers.clicked.connect(self.add_layers)
        row.addWidget(self.btn_layers)
        self.btn_save_grid = QPushButton(tr("Zapisz układ siatki"))
        self.btn_save_grid.setToolTip(tr("Zapisz w bibliotece położenie arkuszy w siatce (wiersz, kolumna) – "
                                         "wykryte po wpasowaniu albo poprawione w kolumnie „Siatka”"))
        self.btn_save_grid.clicked.connect(self.save_grid)
        row.addWidget(self.btn_save_grid)
        row.addStretch()
        lay.addLayout(row)

    # --- dane --------------------------------------------------------------------
    def load(self, lib, map_id, current=None):
        self.sheets, self.skipped = bk.sheets_from_library(lib, map_id, current)
        self.unit_m = lb.crs_metres_per_unit(lib.crs_wkt)
        self.curve = []
        self.recompute()

    def model(self):
        return self.cmb_model.currentData()

    def on_model_changed(self, _i=None):
        self.curve = []
        self.recompute()

    def recompute(self):
        w = bk.slider_to_weight(self.sld_seam.value())
        self.lbl_weight.setText("w = 0" if w == 0 else f"w = {w:.3g}")
        self.result = None
        if not self.sheets:
            self.tbl.setRowCount(0)
            self.lbl_info.setText(tr("Brak arkuszy z ramką mapy – wyrównanie blokowe wymaga ramek (Zasięg mapy w panelu GCP)."))
            self.btn_layers.setEnabled(False)
            return
        try:
            self.result = bk.adjust(self.sheets, self.model(), w, unit_m=self.unit_m)
        except bk.BlockError as e:
            self.tbl.setRowCount(0)
            self.lbl_info.setText(tr("Nie można wyrównać: {e}").format(e=e))
            self.btn_layers.setEnabled(False)
            return
        if not self.curve:
            self.curve = bk.tradeoff(self.sheets, self.model(), positions=(0, 25, 40, 50, 60, 75, 90, 100), unit_m=self.unit_m)
        self.btn_layers.setEnabled(True)
        self._fill_table()
        self._fill_info()
        self._draw_chart()

    def _fill_table(self):
        r = self.result
        t = self.tbl
        self._filling = True
        t.setRowCount(len(self.sheets))
        for k, s in enumerate(self.sheets):
            out = k in r.outlier_sheets
            grid = f"{s.grid[0] + 1}, {s.grid[1] + 1}" if s.grid else "–"
            cells = [_item(s.label, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), _item(grid),
                     _item(str(len(s.src))), _item(_num(r.sheet_sigma0[k])), _item(_num(r.sheet_rmse[k])),
                     _item(_num(r.sheet_offset[k]) if r.sheet_offset else "–"), _item("!" if out else "")]
            if out:
                for it in cells:
                    it.setBackground(QBrush(QColor(220, 50, 50, 60)))
            cells[1].setToolTip(tr("wiersz, kolumna w siatce arkuszy – zapisane w bibliotece") if s.grid_saved else
                                tr("wiersz, kolumna w siatce arkuszy – wykryte po wstępnym wpasowaniu; "
                                   "dwuklik – popraw (np. „2, 3”)"))
            cells[1].setFlags(cells[1].flags() | Qt.ItemFlag.ItemIsEditable)
            for c, it in enumerate(cells):
                t.setItem(k, c, it)
        self._filling = False

    def on_item_changed(self, item):
        if self._filling or item.column() != 1 or not 0 <= item.row() < len(self.sheets):
            return
        parts = [p for p in item.text().replace(";", ",").split(",") if p.strip()]
        try:
            row, col = (int(p) - 1 for p in parts)
        except ValueError:
            self.recompute()
            return
        s = self.sheets[item.row()]
        s.grid, s.grid_saved = (row, col), False
        self.curve = []
        self._timer.start()

    def save_grid(self):
        if not self.sheets:
            return False
        layout = {s.key: (s.grid[0] if s.grid else None, s.grid[1] if s.grid else None,
                          getattr(s.analysis, "priority", None)) for s in self.sheets if s.key}
        try:
            with self.panel.library() as lib:
                lib.set_sheet_layout(layout)
        except lb.LibraryError as e:
            self.lbl_info.setText(self.panel._library_error(e))
            return False
        for s in self.sheets:
            s.grid_saved = s.grid is not None
        self._fill_table()
        self.lbl_info.setText(tr("Zapisano układ siatki arkuszy w bibliotece."))
        return True

    def _fill_info(self):
        r = self.result
        g_mean, g_max = r.gap_stats()
        lines = [tr("σ₀ = {s} m, RMSE punktów = {r} m, liczba nadwyżkowa {f}.").format(
            s=_num(r.sigma0), r=_num(r.rmse), f=r.redundancy)]
        if math.isfinite(g_mean):
            lines.append(tr("Szczeliny na stykach (narożniki ramek): średnio {a} m, maks. {b} m.").format(
                a=_num(g_mean * self.unit_m), b=_num(g_max * self.unit_m)))
        if r.ties:
            bad = sum(1 for w in r.tie_std if w > 3.29)
            lines.append(tr("Punkty wiążące (ten sam obiekt na dwóch arkuszach): {n}, odstające {k}.").format(n=len(r.ties), k=bad))
        if r.outlier_sheets:
            lines.append(tr("Arkusz odstający: {s} – jego punkty są systematycznie przesunięte względem sąsiadów.").format(
                s=", ".join(self.sheets[i].label for i in r.outlier_sheets)))
        if self.skipped:
            lines.append(tr("Pominięte arkusze bez ramki mapy: {s}.").format(
                s=", ".join(a.sheet or f"#{a.analysis_id}" for a in self.skipped)))
        self.lbl_info.setText(" ".join(lines))

    def _draw_chart(self):
        svg = ch.tradeoff_curve(self.curve, self._nearest_curve_pos(), srep.chart_texts())
        self.chart.set_svg(svg)
        self.chart_svg = svg

    def _nearest_curve_pos(self):
        if not self.curve:
            return None
        v = self.sld_seam.value()
        return min((p[0] for p in self.curve), key=lambda q: abs(q - v))

    # --- warstwy -----------------------------------------------------------------
    def add_layers(self):
        r = self.result
        if r is None:
            return []
        crs = self.panel.library_crs()
        poly = QgsVectorLayer("Polygon", tr("Wyrównanie blokowe – zasięgi arkuszy"), "memory")
        lines = QgsVectorLayer("LineString", tr("Wyrównanie blokowe – szczeliny"), "memory")
        for lyr in (poly, lines):
            if crs.isValid():
                lyr.setCrs(crs)
        poly.dataProvider().addAttributes([QgsField("sheet", _STR), QgsField("points", _INT), QgsField("sigma0_m", _REAL),
                                           QgsField("rmse_m", _REAL), QgsField("offset_m", _REAL)])
        lines.dataProvider().addAttributes([QgsField("kind", _STR), QgsField("sheets", _STR), QgsField("name", _STR),
                                            QgsField("gap_m", _REAL)])
        poly.updateFields()
        lines.updateFields()
        feats = []
        for k, s in enumerate(self.sheets):
            f = QgsFeature(poly.fields())
            f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in r.footprint(k)]]))
            f.setAttributes([s.label, len(s.src), _finite(r.sheet_sigma0[k]), _finite(r.sheet_rmse[k]),
                             _finite(r.sheet_offset[k] if r.sheet_offset else float("nan"))])
            feats.append(f)
        poly.dataProvider().addFeatures(feats)
        feats = []
        for g in r.gaps:
            (x, y), (dx, dy) = g.at, g.vec
            f = QgsFeature(lines.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x - dx / 2, y - dy / 2), QgsPointXY(x + dx / 2, y + dy / 2)]))
            f.setAttributes([g.kind, f"{self.sheets[g.a].label}/{self.sheets[g.b].label}", g.name, round(g.gap * self.unit_m, 2)])
            feats.append(f)
        lines.dataProvider().addFeatures(feats)
        for lyr in (poly, lines):
            lyr.updateExtents()
        poly.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
            {"color": "0,140,200,25", "outline_color": "0,110,170", "outline_width": "0.5"})))
        settings = QgsPalLayerSettings()
        settings.fieldName = "sheet"
        fmt = QgsTextFormat()
        fmt.setSize(11)
        settings.setFormat(fmt)
        poly.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        poly.setLabelsEnabled(True)
        from .layers import style_arrows
        style_arrows(lines, QColor(208, 59, 59), 0.6)
        QgsProject.instance().addMapLayers([poly, lines])
        return [poly, lines]


def _finite(v):
    return round(v, 3) if v is not None and math.isfinite(v) else None
