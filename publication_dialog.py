"""
Okno „Wydawnictwo wieloarkuszowe”: zgodność arkuszy jednej mapy, zakładki (punkty na dwóch arkuszach),
skorowidz arkuszy na mapie współczesnej, eksport.
"""

import math

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTabWidget, QVBoxLayout,
)
from qgis.core import (
    QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsPalLayerSettings, QgsPointXY, QgsProject,
    QgsSingleSymbolRenderer, QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling,
)

from . import library as lb
from . import math_engine as me
from . import publication as pb
from .series_dialog import _INT, _REAL, _STR, _item, _num, _table


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


class PublicationDialog(QDialog):
    """Arkusze mapy map_id. host – panel QMapAnalyst (open_library_analysis), może być None."""

    def __init__(self, panel, map_id, host=None, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.map_id = map_id
        self.host = host
        self.pub = None
        self.current_dirty = False
        self.chosen_analysis = None
        self.setWindowTitle(tr("Wydawnictwo wieloarkuszowe"))
        self.resize(860, 560)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_title = QLabel()
        top.addWidget(self.lbl_title, 1)
        top.addWidget(QLabel(tr("Model:")))
        self.cmb_model = QComboBox()
        for key, meta in me.MODELS.items():
            self.cmb_model.addItem(QCoreApplication.translate("QMapAnalystEngine", meta["name"]), key)
        self.cmb_model.setCurrentIndex(self.cmb_model.findData("affine"))
        self.cmb_model.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.cmb_model)
        lay.addLayout(top)

        self.tabs = QTabWidget()
        self.tbl_sheets = _table([tr("Arkusz"), tr("Punkty"), "σ₀ [m]", "RMSE [m]", tr("Maks. [m]"), tr("Skala"),
                                  tr("Obrót [°]"), tr("Skale osi X/Y"), tr("Odstające"), tr("Zasięg")])
        self.tbl_sheets.cellDoubleClicked.connect(self.on_sheet_double_clicked)
        self.tabs.addTab(self.tbl_sheets, tr("Arkusze"))
        self.tbl_overlaps = _table([tr("Punkt"), tr("Arkusz A"), tr("Arkusz B"), tr("Rozbieżność [m]")])
        self.tbl_overlaps.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.tbl_overlaps, tr("Zakładki"))
        lay.addWidget(self.tabs, 1)

        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.lbl_summary)

        row = QHBoxLayout()
        self.btn_index = QPushButton(tr("Skorowidz arkuszy"))
        self.btn_index.setToolTip(tr("Warstwa zasięgów arkuszy na mapie głównej (ramka mapy na skanie przeliczona "
                                     "transformacją arkusza) z numerem arkusza i statystyką"))
        self.btn_index.clicked.connect(self.add_index_layer)
        self.btn_export = QPushButton(tr("Eksport CSV…"))
        self.btn_export.clicked.connect(self.export)
        close = QPushButton(tr("Zamknij"))
        close.clicked.connect(self.reject)
        row.addWidget(self.btn_index)
        row.addStretch()
        row.addWidget(self.btn_export)
        row.addWidget(close)
        lay.addLayout(row)
        self.refresh()

    # --- dane --------------------------------------------------------------------
    def refresh(self, *args):
        current = None
        if self.panel.current_analysis is not None:
            analysis, links, self.current_dirty = self.panel.current_analysis()
            current = (analysis, links) if links else None
        try:
            with self.panel.library() as lib:
                self.pub = pb.build_publication(lib, self.map_id, self.cmb_model.currentData(), current=current)
        except lb.LibraryError as e:
            self.pub = None
            self.lbl_summary.setText(self.panel._library_error(e))
            return
        self._fill_sheets()
        self._fill_overlaps()
        self._fill_summary()

    def _sheet_label(self, s):
        label = tr("arkusz {s}").format(s=s.analysis.sheet) if s.analysis.sheet else f"#{s.analysis.analysis_id}"
        if s.analysis.analysis_id == self.pub.current_id and self.current_dirty:
            label += " *"
        return label

    def _fill_sheets(self):
        t = self.tbl_sheets
        t.setRowCount(len(self.pub.sheets))
        for r, s in enumerate(self.pub.sheets):
            scale = f"1 : {s.scale_den:,.0f}".replace(",", " ") if math.isfinite(s.scale_den) else (
                tr("brak DPI") if s.fitted else "–")
            cells = [_item(self._sheet_label(s), align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                     _item(str(s.n)), _item(_num(s.sigma0_m)), _item(_num(s.rmse_m)), _item(_num(s.max_m)), _item(scale),
                     _item(_num(s.rotation_deg, 2)), _item(_num(s.axis_ratio, 3)),
                     _item(str(len(s.outliers)) if s.fitted else "–"),
                     _item("✓" if s.footprint else "–", None if s.footprint else tr(
                         "brak zasięgu – za mało punktów do wyrównania albo brak ramki mapy w arkuszu"),
                         align=Qt.AlignmentFlag.AlignCenter)]
            if not s.fitted:
                cells[1].setToolTip(tr("za mało punktów do wyrównania wybranym modelem"))
            for c, it in enumerate(cells):
                t.setItem(r, c, it)

    def _fill_overlaps(self):
        t = self.tbl_overlaps
        ov = self.pub.overlaps
        sheets = {s.analysis.analysis_id: s for s in self.pub.sheets}
        vmax = max((o.diff_m for o in ov if math.isfinite(o.diff_m)), default=0.0) or 1.0
        t.setRowCount(len(ov))
        for r, o in enumerate(ov):
            diff = _item(_num(o.diff_m))
            if math.isfinite(o.diff_m):
                diff.setBackground(QBrush(QColor(220, 50, 50, int(20 + 140 * min(o.diff_m / vmax, 1.0)))))
            for c, it in enumerate([_item(o.name, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                                    _item(self._sheet_label(sheets[o.a])), _item(self._sheet_label(sheets[o.b])), diff]):
                t.setItem(r, c, it)

    def _fill_summary(self):
        pub = self.pub
        m = pub.map
        years = f" ({m.year_from})" if m.year_from else ""
        self.lbl_title.setText(tr("„{title}”{years}: arkusze {n}").format(title=m.title or "?", years=years, n=len(pub.sheets)))
        lines = []
        sc = pub.spread("scale_den")
        if sc:
            lines.append(tr("Skala arkuszy: 1 : {a} – 1 : {b} (rozrzut {p} %).").format(
                a=f"{sc[0]:,.0f}".replace(",", " "), b=f"{sc[1]:,.0f}".replace(",", " "), p=_num(pub.scale_spread_pct())))
        rot = pub.spread("rotation_deg")
        if rot:
            lines.append(tr("Obrót: {a}° – {b}°.").format(a=_num(rot[0], 2), b=_num(rot[1], 2)))
        rm = pub.spread("rmse_m")
        if rm:
            lines.append(tr("RMSE arkuszy: {a} – {b} m.").format(a=_num(rm[0]), b=_num(rm[1])))
        if pub.overlaps:
            o = pub.overlaps[0]
            sheets = {s.analysis.analysis_id: s for s in pub.sheets}
            lines.append(tr("Zakładki: {n} par wskazań; największa rozbieżność {d} m – {name} ({a} / {b}).").format(
                n=len(pub.overlaps), d=_num(o.diff_m), name=o.name, a=self._sheet_label(sheets[o.a]),
                b=self._sheet_label(sheets[o.b])))
        else:
            lines.append(tr("Brak punktów wskazanych na dwóch arkuszach (zakładek)."))
        self.lbl_summary.setText(" ".join(lines))

    # --- akcje -------------------------------------------------------------------
    def on_sheet_double_clicked(self, row, _col):
        if self.pub is None or not 0 <= row < len(self.pub.sheets) or self.host is None:
            return
        aid = self.pub.sheets[row].analysis.analysis_id
        if aid != self.pub.current_id and self.host.open_library_analysis(aid):
            self.chosen_analysis = aid
            self.accept()

    def add_index_layer(self):
        pub = self.pub
        if pub is None:
            return None
        layer = QgsVectorLayer("Polygon", tr("Skorowidz arkuszy – {title}").format(title=pub.map.title or "?"), "memory")
        if self.panel.library_crs().isValid():
            layer.setCrs(self.panel.library_crs())
        pr = layer.dataProvider()
        pr.addAttributes([QgsField("sheet", _STR), QgsField("points", _INT), QgsField("rmse_m", _REAL),
                          QgsField("scale_den", _REAL), QgsField("rotation", _REAL)])
        layer.updateFields()
        feats = []
        for s in pub.sheets:
            if not s.footprint:
                continue
            f = QgsFeature(layer.fields())
            f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in s.footprint]]))
            f.setAttributes([s.analysis.sheet, s.n, _finite(s.rmse_m), _finite(s.scale_den), _finite(s.rotation_deg)])
            feats.append(f)
        pr.addFeatures(feats)
        layer.updateExtents()
        layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
            {"color": "0,140,200,30", "outline_color": "0,110,170", "outline_width": "0.5"})))
        settings = QgsPalLayerSettings()
        settings.fieldName = "sheet"
        fmt = QgsTextFormat()
        fmt.setSize(11)
        settings.setFormat(fmt)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
        QgsProject.instance().addMapLayer(layer)
        return layer

    def export(self):
        if self.pub is None:
            return
        path, _flt = QFileDialog.getSaveFileName(self, tr("Eksport wydawnictwa wieloarkuszowego"),
                                                 f"{self.pub.map.title or 'mapa'}_arkusze.csv", "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(pb.sheets_csv(self.pub))
        self.lbl_summary.setText(tr("Zapisano: {f}").format(f=path))


def _finite(v):
    return round(v, 3) if v is not None and math.isfinite(v) else None
