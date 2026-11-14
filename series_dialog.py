"""
Okno „Seria czasowa”: porównanie map z różnych lat dla zestawu – statystyki map (wydawnictwo wieloarkuszowe
to jeden element), podobieństwo reszt (ślad kopiowania), błędy położenia punktów; warstwy wektorów błędów
i trajektorii punktów na mapie głównej; eksport.
"""

import math

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
)
from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsGeometry, QgsLineSymbol,
    QgsMarkerLineSymbolLayer, QgsMarkerSymbol, QgsPointXY, QgsProject, QgsRendererCategory, QgsSingleSymbolRenderer,
    QgsVectorLayer,
)

from . import coverage as cv
from . import library as lb
from . import math_engine as me
from . import series as sr
from . import series_report as srep

try:
    from qgis.PyQt.QtCore import QMetaType
    _STR, _INT, _REAL = QMetaType.Type.QString, QMetaType.Type.Int, QMetaType.Type.Double
except (ImportError, AttributeError):          # starsze QGIS
    from qgis.PyQt.QtCore import QVariant
    _STR, _INT, _REAL = QVariant.String, QVariant.Int, QVariant.Double

PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22"]


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


def _num(v, nd=1):
    return "–" if v is None or not math.isfinite(v) else f"{v:,.{nd}f}".replace(",", " ")


def _item(text, tip=None, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter):
    it = QTableWidgetItem(text)
    it.setTextAlignment(align)
    if tip:
        it.setToolTip(tip)
    return it


def _table(columns):
    t = QTableWidget(0, len(columns))
    t.setHorizontalHeaderLabels(columns)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.verticalHeader().setDefaultSectionSize(22)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    return t


class SeriesDialog(QDialog):
    """Seria czasowa zestawu wybranego w panelu biblioteki."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.series = None
        self.current_dirty = False
        self.setWindowTitle(tr("Seria czasowa"))
        self.resize(900, 600)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_title = QLabel()
        top.addWidget(self.lbl_title, 1)
        top.addWidget(QLabel(tr("Model:")))
        self.cmb_model = QComboBox()
        for key, meta in me.MODELS.items():
            self.cmb_model.addItem(QCoreApplication.translate("QMapAnalystEngine", meta["name"]), key)
        self.cmb_model.setCurrentIndex(self.cmb_model.findData("affine"))
        self.cmb_model.setToolTip(tr("Ten sam model dla wszystkich map i ich arkuszy – porównywalne statystyki i reszty"))
        self.cmb_model.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.cmb_model)
        lay.addLayout(top)

        self.tabs = QTabWidget()
        self.tbl_sheets = _table([tr("Mapa"), tr("Arkusze"), tr("Punkty"), "σ₀ [m]", "RMSE [m]", tr("Maks. [m]"),
                                  tr("Skala"), tr("Obrót [°]"), tr("Skale osi X/Y"), tr("Odstające")])
        self.tbl_sheets.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tbl_sheets.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.tbl_sheets.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tabs.addTab(self.tbl_sheets, tr("Mapy"))
        self.tbl_pairs = _table([tr("Mapa wcześniejsza"), tr("Mapa późniejsza"), tr("Punkty wspólne"), "r", "p",
                                 tr("Śr. różnica [m]")])
        self.tbl_pairs.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tbl_pairs.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.tbl_pairs, tr("Podobieństwo reszt"))
        self.tbl_points = _table([])
        self.tabs.addTab(self.tbl_points, tr("Błędy punktów"))
        self.charts_view = srep.ChartsView()
        self.tabs.addTab(self.charts_view, tr("Wykresy"))
        self.charts = []
        lay.addWidget(self.tabs, 1)

        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.lbl_summary)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Powiększenie wektorów:")))
        self.sp_scale = QSpinBox()
        self.sp_scale.setRange(1, 1000)
        self.sp_scale.setValue(10)
        self.sp_scale.setPrefix("× ")
        self.sp_scale.setToolTip(tr("Wektory błędów na mapie są powiększone, by były widoczne w skali mapy współczesnej"))
        row.addWidget(self.sp_scale)
        self.btn_vectors = QPushButton(tr("Warstwa wektorów błędów"))
        self.btn_vectors.setToolTip(tr("Dla każdej mapy: odcinek od położenia odniesienia punktu w stronę, w którą "
                                       "mapa go przesuwa (po usunięciu transformacji)"))
        self.btn_vectors.clicked.connect(self.add_vectors_layer)
        self.btn_traj = QPushButton(tr("Warstwa trajektorii"))
        self.btn_traj.setToolTip(tr("Dla każdego punktu: łamana przez położenia, które nadają mu kolejne mapy (wg roku)"))
        self.btn_traj.clicked.connect(self.add_trajectories_layer)
        row.addWidget(self.btn_vectors)
        row.addWidget(self.btn_traj)
        row.addStretch()
        self.btn_export = QPushButton(tr("Eksport"))
        self.btn_export.setToolTip(tr("Raport PDF lub HTML z wykresami, same wykresy (SVG i PNG – do prezentacji) "
                                      "albo dane: CSV map, CSV błędów punktów, JSON z całą serią"))
        menu = QMenu(self.btn_export)
        menu.addAction(tr("Raport PDF…"), lambda: self.export_report("pdf"))
        menu.addAction(tr("Raport HTML…"), lambda: self.export_report("html"))
        menu.addAction(tr("Wykresy (SVG i PNG)…"), self.export_charts)
        menu.addSeparator()
        menu.addAction(tr("Dane (CSV / JSON)…"), self.export)
        self.btn_export.setMenu(menu)
        close = QPushButton(tr("Zamknij"))
        close.clicked.connect(self.reject)
        row.addWidget(self.btn_export)
        row.addWidget(close)
        lay.addLayout(row)
        self.refresh()

    # --- dane --------------------------------------------------------------------
    def refresh(self, *args):
        sid = self.panel.current_set()
        current = None
        if self.panel.current_analysis is not None:
            analysis, links, self.current_dirty = self.panel.current_analysis()
            current = (analysis, links) if links else None
        try:
            with self.panel.library() as lib:
                self.series = sr.build_series(lib, sid, self.cmb_model.currentData(), current=current)
        except lb.LibraryError as e:
            self.series = None
            self.lbl_summary.setText(self.panel._library_error(e))
            return
        self._fill_sheets()
        self._fill_pairs()
        self._fill_points()
        self._fill_summary()
        self.charts = srep.series_charts(self.series, self._unit_m(), self._label)
        self.charts_view.set_charts(self.charts)

    def _unit_m(self):
        unit = lb.crs_metres_per_unit(self.panel._crs_wkt or self.panel._crs_authid)
        return unit if math.isfinite(unit) else 1.0

    def _title(self):
        return tr("Seria czasowa") + f": {self.series.set_name}"

    def export_report(self, kind):
        if self.series is None:
            return None
        path = srep.ask_report_path(self, tr("Raport serii czasowej"), f"{self.series.set_name}_seria_czasowa", kind)
        if not path:
            return None
        html, images = srep.series_report(self.series, kind, self._unit_m())
        srep.save_report(path, html, images, self._title(), self.cmb_model.currentText())
        self.lbl_summary.setText(tr("Zapisano: {f}").format(f=path))
        return path

    def export_charts(self):
        if self.series is None:
            return None
        folder = srep.ask_charts_folder(self)
        if not folder:
            return None
        files = srep.export_charts(folder, self.charts, srep.safe_name(self.series.set_name) + "_")
        self.lbl_summary.setText(tr("Zapisano wykresy ({n} plików) w folderze {f}").format(n=len(files), f=folder))
        return files

    def _label(self, s):
        label = cv.analysis_label(s.column) if s.key != cv.CURRENT else (s.analysis.title or tr("bieżąca analiza"))
        if s.key == self.series.current_id and (self.current_dirty or s.key == cv.CURRENT):
            label += " *"
        return label

    def _sheets_text(self, s):
        """Numer arkusza (jeden arkusz) albo liczba arkuszy wydawnictwa z listą w podpowiedzi."""
        parts = s.parts or [s]
        names = [p.analysis.sheet or "?" for p in parts]
        if len(parts) == 1:
            return names[0] if parts[0].analysis.sheet else "", None
        return str(len(parts)), tr("arkusze: {names}").format(names=", ".join(names))

    def _fill_sheets(self):
        t = self.tbl_sheets
        ser = self.series
        t.setRowCount(len(ser.maps))
        for r, s in enumerate(ser.maps):
            sheets, sheets_tip = self._sheets_text(s)
            scale = f"1 : {s.scale_den:,.0f}".replace(",", " ") if math.isfinite(s.scale_den) else (
                tr("brak DPI") if s.fitted else "–")
            cells = [_item(self._label(s), align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                     _item(sheets, sheets_tip), _item(str(s.n)), _item(_num(s.sigma0_m)), _item(_num(s.rmse_m)),
                     _item(_num(s.max_m)), _item(scale), _item(_num(s.rotation_deg, 2)), _item(_num(s.axis_ratio, 3)),
                     _item(str(len(s.outliers)) if s.fitted else "–",
                           ", ".join(self._name(u) for u in s.outliers) or None)]
            if not s.fitted:
                cells[2].setToolTip(tr("za mało punktów do wyrównania wybranym modelem"))
            for c, it in enumerate(cells):
                t.setItem(r, c, it)
        # długie nazwy arkuszy nie mogą zabierać miejsca nazwie mapy
        t.resizeColumnToContents(1)
        t.setColumnWidth(1, min(t.columnWidth(1), 140))

    def _name(self, uid):
        return next((p.name for p in self.series.points if p.uid == uid), "?")

    def _fill_pairs(self):
        t = self.tbl_pairs
        ser = self.series
        by_key = {s.key: s for s in ser.maps}
        pairs = [p for p in ser.pairs if p.n > 0]
        t.setRowCount(len(pairs))
        bold = QFont()
        bold.setBold(True)
        for r, p in enumerate(pairs):
            cells = [_item(self._label(by_key[p.a]), align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                     _item(self._label(by_key[p.b]), align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                     _item(str(p.n)), _item(_num(p.r, 2)), _item(_num(p.p, 3)), _item(_num(p.diff_m))]
            if p.n < sr.MIN_COMMON:
                cells[2].setToolTip(tr("za mało punktów wspólnych do porównania (min. {k})").format(k=sr.MIN_COMMON))
            for c, it in enumerate(cells):
                if p.similar:
                    it.setFont(bold)
                    it.setBackground(QBrush(QColor(220, 50, 50, 70)))
                    it.setToolTip(tr("Błędy punktów wspólnych są do siebie podobne – możliwe kopiowanie treści "
                                     "(albo wspólny błąd punktów odniesienia)"))
                t.setItem(r, c, it)

    def _fill_points(self):
        ser = self.series
        t = self.tbl_points
        t.clear()
        t.setColumnCount(len(ser.maps) + 2)
        t.setHorizontalHeaderLabels([self._label(s) for s in ser.maps] + [tr("Mapy"), tr("Średnio [m]")])
        t.setRowCount(len(ser.points))
        vmax = max((v for s in ser.maps for v in s.err_m.values()), default=0.0) or 1.0
        for r, p in enumerate(ser.points):
            t.setVerticalHeaderItem(r, QTableWidgetItem(p.name))
            for c, s in enumerate(ser.maps):
                v = s.err_m.get(p.uid)
                it = _item(_num(v) if v is not None else "")
                if v is not None:
                    it.setBackground(QBrush(QColor(220, 50, 50, int(20 + 140 * min(v / vmax, 1.0)))))
                    if p.uid in s.outliers:
                        it.setText(it.text() + " !")
                        it.setToolTip(tr("odstający (test Baardy)"))
                t.setItem(r, c, it)
            n, mean, _v = ser.point_summary(p.uid)
            t.setItem(r, len(ser.maps), _item(str(n)))
            t.setItem(r, len(ser.maps) + 1, _item(_num(mean)))

    def _fill_summary(self):
        ser = self.series
        self.lbl_title.setText(tr("Zestaw „{name}”: punkty {n}, mapy {m}").format(
            name=ser.set_name, n=len(ser.points), m=len(ser.maps)))
        if not ser.maps:
            self.lbl_summary.setText(tr("Żadna analiza zapisana w bibliotece nie zawiera punktów tego zestawu."))
            return
        by_key = {s.key: s for s in ser.maps}
        similar = ser.similar_pairs()
        lines = [tr("Błąd położenia punktu: gdzie mapa umieszcza obiekt względem jego położenia w bibliotece, po usunięciu "
                    "transformacji arkusza (arkusze wydawnictwa wieloarkuszowego wyrównywane osobno). Podobieństwo reszt: "
                    "korelacja wektorowa r błędów punktów wspólnych, p – test permutacyjny.")]
        if similar:
            lines.append(tr("Podobne błędy (możliwe kopiowanie): {pairs}").format(
                pairs="; ".join(f"{self._label(by_key[p.a])} → {self._label(by_key[p.b])} (r = {_num(p.r, 2)})" for p in similar)))
        else:
            lines.append(tr("Brak par map o istotnie podobnych błędach."))
        self.lbl_summary.setText("\n".join(lines))

    # --- warstwy -------------------------------------------------------------------
    def _new_layer(self, kind, name):
        layer = QgsVectorLayer(kind, name, "memory")
        layer.setCrs(self.panel.library_crs() if self.panel.library_crs().isValid() else QgsCoordinateReferenceSystem())
        return layer

    def _style_by_map(self, layer, field, values, arrows):
        cats = []
        for k, (value, label) in enumerate(values):
            sym = QgsLineSymbol.createSimple({"line_color": PALETTE[k % len(PALETTE)], "line_width": "0.6"})
            if arrows:
                marker = QgsMarkerLineSymbolLayer()
                marker.setPlacements(_enum_placement())
                head = QgsMarkerSymbol.createSimple({"name": "filled_arrowhead", "color": PALETTE[k % len(PALETTE)],
                                                     "size": "2.4", "outline_style": "no"})
                marker.setSubSymbol(head)
                marker.setRotateSymbols(True)
                sym.appendSymbolLayer(marker)
            cats.append(QgsRendererCategory(value, sym, label))
        layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))

    def add_vectors_layer(self):
        ser = self.series
        if ser is None or not ser.maps:
            return None
        k = self.sp_scale.value()
        layer = self._new_layer("LineString", tr("Wektory błędów – {name} (× {k})").format(name=ser.set_name, k=k))
        pr = layer.dataProvider()
        pr.addAttributes([QgsField("point", _STR), QgsField("uid", _STR), QgsField("map", _STR), QgsField("sheet", _STR),
                          QgsField("year", _INT), QgsField("error_m", _REAL), QgsField("outlier", _INT)])
        layer.updateFields()
        names = {p.uid: p.name for p in ser.points}
        feats = []
        for s in ser.maps:
            label = self._label(s)
            for uid, (x, y) in s.pos.items():
                dx, dy = s.vec[uid]
                f = QgsFeature(layer.fields())
                f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y), QgsPointXY(x + k * dx, y + k * dy)]))
                f.setAttributes([names.get(uid, ""), uid, label, self._sheets_text(s)[0], s.column.year_from or None,
                                 round(s.err_m[uid], 2), int(uid in s.outliers)])
                feats.append(f)
        pr.addFeatures(feats)
        layer.updateExtents()
        self._style_by_map(layer, "map", [(self._label(s), self._label(s)) for s in ser.maps], arrows=True)
        QgsProject.instance().addMapLayer(layer)
        return layer

    def add_trajectories_layer(self):
        ser = self.series
        if ser is None or not ser.maps:
            return None
        k = self.sp_scale.value()
        layer = self._new_layer("LineString", tr("Trajektorie punktów – {name} (× {k})").format(name=ser.set_name, k=k))
        pr = layer.dataProvider()
        pr.addAttributes([QgsField("point", _STR), QgsField("uid", _STR), QgsField("maps", _INT),
                          QgsField("mean_m", _REAL), QgsField("path", _STR)])
        layer.updateFields()
        feats = []
        for p in ser.points:
            traj = ser.trajectory(p.uid)
            if len(traj) < 2:
                continue
            pts = [QgsPointXY(x + k * dx, y + k * dy) for _s, (x, y), (dx, dy) in traj]
            n, mean, _v = ser.point_summary(p.uid)
            f = QgsFeature(layer.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY(pts))
            f.setAttributes([p.name, p.uid, n, round(mean, 2), " → ".join(self._label(s) for s, _p, _d in traj)])
            feats.append(f)
        pr.addFeatures(feats)
        layer.updateExtents()
        sym = QgsLineSymbol.createSimple({"line_color": "#6a1b9a", "line_width": "0.5"})
        marker = QgsMarkerLineSymbolLayer()
        marker.setPlacements(_enum_placement(vertices=True))
        marker.setSubSymbol(QgsMarkerSymbol.createSimple({"name": "circle", "color": "#6a1b9a", "size": "1.4",
                                                          "outline_style": "no"}))
        sym.appendSymbolLayer(marker)
        layer.setRenderer(QgsSingleSymbolRenderer(sym))
        QgsProject.instance().addMapLayer(layer)
        return layer

    # --- eksport ---------------------------------------------------------------------
    def export(self):
        ser = self.series
        if ser is None:
            return
        flt_sheets, flt_points, flt_json = tr("Mapy (CSV) (*.csv)"), tr("Błędy punktów (CSV) (*.csv)"), "JSON (*.json)"
        path, flt = QFileDialog.getSaveFileName(self, tr("Eksport serii czasowej"), f"{ser.set_name}_seria",
                                                ";;".join((flt_sheets, flt_points, flt_json)))
        if not path:
            return
        kind = "json" if flt == flt_json or path.lower().endswith(".json") else ("points" if flt == flt_points else "sheets")
        ext = ".json" if kind == "json" else ".csv"
        if not path.lower().endswith(ext):
            path += ext
        text = sr.to_json(ser) if kind == "json" else (sr.points_csv(ser) if kind == "points" else sr.maps_csv(ser))
        with open(path, "w", encoding="utf-8" if kind == "json" else "utf-8-sig", newline="") as f:
            f.write(text)
        self.lbl_summary.setText(tr("Zapisano: {f}").format(f=path))


def _enum_placement(vertices=False):
    """Umieszczenie znaczników na linii: ostatni wierzchołek (grot) albo wszystkie wierzchołki."""
    from qgis.core import Qgis
    flags = getattr(Qgis, "MarkerLinePlacement", None)
    if flags is not None:
        return flags.Vertex if vertices else flags.LastVertex
    return QgsMarkerLineSymbolLayer.Vertex if vertices else QgsMarkerLineSymbolLayer.LastVertex
