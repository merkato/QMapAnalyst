"""
Okno macierzy pokrycia punkt × mapa (zestaw punktów biblioteki × analizy map).
"""

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from qgis.core import QgsSettings

from . import coverage as cv
from . import library as lb
from . import math_engine as me

SETTINGS_BY = "QMapAnalyst/coverage_by"

# symbol, kolor tła (RGBA – półprzezroczysty, czytelny w jasnym i ciemnym motywie)
CELL_STYLE = {
    "sure": ("✓", (46, 160, 67, 110)),
    "uncertain": ("?", (230, 170, 30, 120)),
    "outlier": ("!", (220, 50, 50, 140)),
    "illegible": ("×", (128, 128, 128, 90)),
    "disabled": ("–", (128, 128, 128, 60)),
    "absent": ("○", (70, 130, 220, 100)),
}


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


def cell_labels():
    return {
        "sure": tr("pewny"), "uncertain": tr("niepewny"), "outlier": tr("odstający (test Baardy)"),
        "illegible": tr("nieczytelny"), "disabled": tr("wyłączony"), "absent": tr("brak na mapie"),
        None: tr("nie wskazany"),
    }


class CoverageDialog(QDialog):
    """Macierz pokrycia dla zestawu wybranego w panelu biblioteki. Po zamknięciu dwuklikiem
    `chosen_analysis` wskazuje analizę do wczytania (albo None)."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.coverage = None
        self.chosen_analysis = None
        self.current_dirty = False
        self.setWindowTitle(tr("Macierz pokrycia"))
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_title = QLabel()
        top.addWidget(self.lbl_title, 1)
        top.addWidget(QLabel(tr("Kolumny:")))
        self.cmb_by = QComboBox()
        self.cmb_by.addItem(tr("arkusze"), cv.BY_SHEET)
        self.cmb_by.addItem(tr("mapy (arkusze razem)"), cv.BY_MAP)
        self.cmb_by.setToolTip(tr("Mapy: arkusze jednej mapy w jednej kolumnie – punkt obecny na którymkolwiek arkuszu "
                                  "jest obecny na mapie, odstający na którymkolwiek – odstający."))
        self.cmb_by.setCurrentIndex(max(self.cmb_by.findData(QgsSettings().value(SETTINGS_BY, cv.BY_SHEET)), 0))
        self.cmb_by.currentIndexChanged.connect(self.on_by_changed)
        top.addWidget(self.cmb_by)
        top.addWidget(QLabel(tr("Odstające wg modelu:")))
        self.cmb_model = QComboBox()
        for key, meta in me.MODELS.items():
            self.cmb_model.addItem(QCoreApplication.translate("QMapAnalystEngine", meta["name"]), key)
        self.cmb_model.setCurrentIndex(self.cmb_model.findData("affine"))
        self.cmb_model.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.cmb_model)
        lay.addLayout(top)

        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        lay.addWidget(self.table, 1)

        legend = "   ".join(f"{CELL_STYLE[c][0]} {cell_labels()[c]}" for c in cv.CODES)
        self.lbl_legend = QLabel(legend + "   " + tr("(puste – nie wskazany) · dwuklik – wczytaj analizę · * – stan bieżący, niezapisany"))
        self.lbl_legend.setWordWrap(True)
        lay.addWidget(self.lbl_legend)
        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.lbl_summary)

        buttons = QHBoxLayout()
        self.btn_csv = QPushButton(tr("Eksport CSV…"))
        self.btn_csv.clicked.connect(lambda: self.export("csv"))
        self.btn_json = QPushButton(tr("Eksport JSON…"))
        self.btn_json.clicked.connect(lambda: self.export("json"))
        close = QPushButton(tr("Zamknij"))
        close.clicked.connect(self.reject)
        buttons.addWidget(self.btn_csv)
        buttons.addWidget(self.btn_json)
        buttons.addStretch()
        buttons.addWidget(close)
        lay.addLayout(buttons)
        self.refresh()

    # --- dane ------------------------------------------------------------------
    def refresh(self, *args):
        sid = self.panel.current_set()
        current = None
        if self.panel.current_analysis is not None:
            analysis, links, self.current_dirty = self.panel.current_analysis()
            current = (analysis, links) if links else None
        try:
            with self.panel.library() as lib:
                self.coverage = cv.build_coverage(lib, sid, self.cmb_model.currentData(), current=current,
                                                  by=self.cmb_by.currentData())
        except lb.LibraryError as e:
            self.coverage = None
            self.lbl_summary.setText(self.panel._library_error(e))
            return
        self._fill_table()
        self._fill_summary()

    def on_by_changed(self, *args):
        QgsSettings().setValue(SETTINGS_BY, self.cmb_by.currentData())
        self.refresh()

    def _column_tip(self, a):
        cov = self.coverage
        tip = [self._column_label(a)]
        if len(a.analysis_ids) > 1:
            tip.append(tr("arkusze: {n}").format(n=len(a.analysis_ids)))
        if a.key == cov.current_id:
            tip.append(tr("stan bieżący z panelu GCP") + (" – " + tr("niezapisane zmiany") if self.current_dirty else ""))
        if a.scale:
            tip.append(f"1 : {a.scale:,}".replace(",", " "))
        if a.sheet:
            tip.append(tr("arkusz {s}").format(s=a.sheet))
        tip.append(tr("punkty zestawu na mapie: {n}").format(n=cov.column_count(a.key)))
        if not cov.fitted.get(a.key):
            tip.append(tr("odstające nie badane – za mało punktów do wyrównania"))
        return "\n".join(tip)

    def _fill_table(self):
        cov = self.coverage
        t = self.table
        t.clear()
        t.setRowCount(len(cov.points))
        t.setColumnCount(len(cov.analyses) + 1)
        for c, a in enumerate(cov.analyses):
            it = QTableWidgetItem(self._column_label(a))
            it.setToolTip(self._column_tip(a))
            t.setHorizontalHeaderItem(c, it)
        t.setHorizontalHeaderItem(len(cov.analyses), QTableWidgetItem("Σ"))
        common = set(cov.common())
        repeated = set(cov.repeated_outliers())
        labels = cell_labels()
        for r, p in enumerate(cov.points):
            head = QTableWidgetItem(p.name)
            if p.uid in common or p.uid in repeated:
                f = QFont()
                f.setBold(True)
                head.setFont(f)
            if p.uid in repeated:
                head.setForeground(QBrush(QColor(220, 50, 50)))
            head.setToolTip(p.alt_names.replace("|", ", ") if p.alt_names else p.name)
            t.setVerticalHeaderItem(r, head)
            for c, a in enumerate(cov.analyses):
                t.setItem(r, c, self._cell_item(cov.cell(p.uid, a.key), labels))
            total = QTableWidgetItem(f"{cov.row_count(p.uid)}/{len(cov.analyses)}")
            total.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setItem(r, len(cov.analyses), total)

    def _column_label(self, a):
        if a.key == cv.CURRENT:
            label = (a.title or tr("bieżąca analiza")) + " *"
        else:
            label = cv.analysis_label(a)
            if a.key == self.coverage.current_id and self.current_dirty:
                label += " *"
        return label

    @staticmethod
    def _cell_item(code, labels):
        symbol, rgba = CELL_STYLE.get(code, ("", None))
        it = QTableWidgetItem(symbol)
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        it.setToolTip(labels.get(code, ""))
        it.setData(Qt.ItemDataRole.UserRole, code)
        if rgba is not None:
            it.setBackground(QBrush(QColor(*rgba)))
        return it

    def _names(self, uids):
        by_uid = {p.uid: p.name for p in self.coverage.points}
        return ", ".join(by_uid[u] for u in uids)

    def _fill_summary(self):
        cov = self.coverage
        self.lbl_title.setText(tr("Zestaw „{name}”: punkty {n}, mapy {m}").format(
            name=cov.set_name, n=len(cov.points), m=len(cov.analyses)))
        if not cov.analyses:
            self.lbl_summary.setText(tr("Żadna analiza zapisana w bibliotece nie zawiera punktów tego zestawu."))
            return
        common = cov.common()
        text = tr("Punkty wspólne wszystkim mapom (pogrubione): {n}").format(n=len(common))
        if common:
            text += f" – {self._names(common)}"
        repeated = cov.repeated_outliers()
        if repeated:
            text += "\n" + tr("Odstające na co najmniej dwóch mapach (czerwone) – sprawdź identyfikację lub położenie: {names}").format(
                names=self._names(repeated))
        self.lbl_summary.setText(text)

    # --- akcje -------------------------------------------------------------------
    def on_cell_double_clicked(self, row, col):
        if self.coverage is None or col >= len(self.coverage.analyses):
            return
        column = self.coverage.columns[col]
        current = self.panel.current_analysis()[0] if self.panel.current_analysis is not None else None
        cur_id = current.analysis_id if current is not None else cv.CURRENT
        ids = [i for i in column.analysis_ids if i != cur_id]
        if not ids:
            self.lbl_legend.setText(tr("Ta analiza jest otwarta w panelu GCP."))
            return
        aid = ids[0] if len(ids) == 1 else self._choose_sheet(ids)
        if aid is None:
            return
        self.chosen_analysis = aid
        self.accept()

    def _choose_sheet(self, ids):
        try:
            with self.panel.library() as lib:
                sheets = [lib.get_analysis(i) for i in ids]
        except lb.LibraryError:
            return None
        labels = [(tr("arkusz {s}").format(s=a.sheet) if a.sheet else f"#{a.analysis_id}") for a in sheets]
        label, ok = QInputDialog.getItem(self, tr("Wczytaj arkusz"), tr("Arkusz mapy:"), labels, 0, False)
        return ids[labels.index(label)] if ok and label in labels else None

    def export(self, kind):
        if self.coverage is None:
            return
        flt = "CSV (*.csv)" if kind == "csv" else "JSON (*.json)"
        path, _f = QFileDialog.getSaveFileName(self, tr("Eksport macierzy pokrycia"),
                                               f"{self.coverage.set_name}_pokrycie.{kind}", flt)
        if not path:
            return
        if not path.lower().endswith("." + kind):
            path += "." + kind
        text = cv.to_csv(self.coverage) if kind == "csv" else cv.to_json(self.coverage)
        with open(path, "w", encoding="utf-8-sig" if kind == "csv" else "utf-8", newline="") as f:
            f.write(text)
        self.lbl_legend.setText(tr("Zapisano: {f}").format(f=path))
