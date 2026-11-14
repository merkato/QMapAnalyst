"""
Mapy i ich arkusze w bibliotece punktów.

AnalysisDialog – opis arkusza (analizy) z wyborem mapy z listy: opis mapy (tytuł, autor, lata, skala)
jest wspólny dla wszystkich jej arkuszy, arkusz ma własny numer, DPI i uwagi.
MapsDialog – przegląd map i arkuszy: nowa mapa, zapis bieżących punktów jako nowego arkusza,
edycja, przeniesienie arkusza do innej mapy, usuwanie, wczytanie arkusza.
"""

import os
from html import escape

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from . import library as lb

NEW_MAP = -1        # pozycja „nowa mapa” na liście map


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


def analysis_years(a):
    if a.year_from and a.year_to and a.year_to != a.year_from:
        return f"{a.year_from}–{a.year_to}"
    return str(a.year_from or a.year_to or "")


def map_label(m):
    years = analysis_years(m)
    return (m.title or "?") + (f" ({years})" if years else "")


def _year(value):
    sp = QSpinBox()
    sp.setRange(0, 2100)
    sp.setSpecialValueText("?")
    sp.setValue(value or 0)
    return sp


def _scale(value):
    sp = QSpinBox()
    sp.setRange(0, 100000000)
    sp.setSingleStep(1000)
    sp.setPrefix("1 : ")
    sp.setSpecialValueText(tr("nieznana"))
    sp.setValue(value or 0)
    return sp


class MapFields:
    """Pola opisu mapy (tytuł, autor, lata, skala) dodawane do formularza."""

    def __init__(self, form, m):
        self.ed_title = QLineEdit(m.title)
        self.ed_author = QLineEdit(m.author)
        self.sp_from = _year(m.year_from)
        self.sp_to = _year(m.year_to)
        self.sp_scale = _scale(m.scale)
        years = QHBoxLayout()
        years.addWidget(self.sp_from)
        years.addWidget(QLabel("–"))
        years.addWidget(self.sp_to)
        form.addRow(tr("Tytuł:"), self.ed_title)
        form.addRow(tr("Autor / wydawca:"), self.ed_author)
        form.addRow(tr("Rok (od – do):"), years)
        form.addRow(tr("Skala nominalna:"), self.sp_scale)

    def load(self, m):
        self.ed_title.setText(m.title)
        self.ed_author.setText(m.author)
        self.sp_from.setValue(m.year_from or 0)
        self.sp_to.setValue(m.year_to or 0)
        self.sp_scale.setValue(m.scale or 0)

    def store(self, obj):
        obj.title = self.ed_title.text().strip()
        obj.author = self.ed_author.text().strip()
        obj.year_from = self.sp_from.value() or None
        obj.year_to = self.sp_to.value() or None
        obj.scale = self.sp_scale.value() or None


class AnalysisDialog(QDialog):
    """Opis arkusza mapy (library.Analysis) z wyborem mapy z listy `maps` (library.MapInfo).

    Po akceptacji: analysis.map_id – wybrana mapa (None – nowa, utworzy ją zapis analizy);
    map_info – zmieniony opis istniejącej mapy do zapisania (Library.save_map) albo None."""

    def __init__(self, analysis, parent=None, maps=()):
        super().__init__(parent)
        self.setWindowTitle(tr("Opis mapy"))
        self.analysis = analysis
        self.maps = {m.map_id: m for m in maps}
        self.map_info = None
        form = QFormLayout(self)
        self.cmb_map = QComboBox()
        for m in maps:
            self.cmb_map.addItem(map_label(m), m.map_id)
        self.cmb_map.addItem(tr("Nowa mapa…"), NEW_MAP)
        self.cmb_map.setToolTip(tr("Mapa, do której należy arkusz. Opis mapy jest wspólny dla wszystkich jej arkuszy."))
        form.addRow(tr("Mapa:"), self.cmb_map)
        self.lbl_map = QLabel()
        self.lbl_map.setWordWrap(True)
        self.lbl_map.setStyleSheet("color: palette(placeholder-text); font-size: 8pt;")
        form.addRow(self.lbl_map)
        self.fields = MapFields(form, analysis)
        # skróty do pól (zgodność z wcześniejszym formularzem)
        self.ed_title, self.ed_author = self.fields.ed_title, self.fields.ed_author
        self.sp_from, self.sp_to, self.sp_scale = self.fields.sp_from, self.fields.sp_to, self.fields.sp_scale
        self.ed_sheet = QLineEdit(analysis.sheet)
        self.sp_dpi = QDoubleSpinBox()
        self.sp_dpi.setRange(0, 9600)
        self.sp_dpi.setDecimals(0)
        self.sp_dpi.setSpecialValueText(tr("nieznana"))
        self.sp_dpi.setValue(analysis.dpi or 0)
        self.ed_note = QLineEdit(analysis.note)
        form.addRow(tr("Arkusz:"), self.ed_sheet)
        form.addRow(tr("Rozdzielczość skanu [DPI]:"), self.sp_dpi)
        form.addRow(tr("Uwagi:"), self.ed_note)
        scan = QLabel(escape(os.path.basename(analysis.scan_path or "")) or "–")
        scan.setToolTip(analysis.scan_path or "")
        form.addRow(tr("Skan:"), scan)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.cmb_map.setCurrentIndex(self.cmb_map.findData(self._initial_map()))
        self.cmb_map.currentIndexChanged.connect(self.on_map_changed)
        self.on_map_changed()                 # opis wybranej mapy – nie z analizy (np. tytuł warstwy)

    def _initial_map(self):
        a = self.analysis
        if a.map_id in self.maps:
            return a.map_id
        key = lb.normalize(a.title)
        same = [m.map_id for m in self.maps.values() if key and lb.normalize(m.title) == key]
        return same[0] if same else NEW_MAP

    def current_map(self):
        mid = self.cmb_map.currentData()
        return self.maps.get(mid)

    def on_map_changed(self, *args):
        m = self.current_map()
        if m is not None:
            self.fields.load(m)
        self._update_hint()

    def _update_hint(self):
        m = self.current_map()
        if m is None:
            self.lbl_map.setText(tr("Zapis utworzy nową mapę z tym opisem."))
        else:
            self.lbl_map.setText(tr("Zmiany tytułu, autora, lat i skali dotyczą wszystkich arkuszy tej mapy."))

    def accept(self):
        a = self.analysis
        m = self.current_map()
        self.fields.store(a)
        a.sheet = self.ed_sheet.text().strip()
        a.dpi = self.sp_dpi.value() or None
        a.note = self.ed_note.text().strip()
        if m is None:
            a.map_id = None
            self.map_info = None
        else:
            a.map_id = m.map_id
            changed = any(getattr(a, f) != getattr(m, f) for f in lb.MAP_FIELDS)
            self.map_info = lb.MapInfo(**{**vars(m), **{f: getattr(a, f) for f in lb.MAP_FIELDS}}) if changed else None
        super().accept()


def save_with_dialog(lib, analysis, dialog):
    """Zapis analizy po AnalysisDialog: najpierw zmieniony opis mapy, potem arkusz."""
    if dialog is not None and dialog.map_info is not None:
        lib.save_map(dialog.map_info)
    return lib.save_analysis(analysis)


class MapDialog(QDialog):
    """Opis mapy (library.MapInfo)."""

    def __init__(self, m, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Mapa"))
        self.map = m
        form = QFormLayout(self)
        self.fields = MapFields(form, m)
        self.ed_note = QLineEdit(m.note)
        form.addRow(tr("Uwagi:"), self.ed_note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self):
        self.fields.store(self.map)
        self.map.note = self.ed_note.text().strip()
        super().accept()


class MapsDialog(QDialog):
    """Mapy i arkusze biblioteki. `host` – panel QMapAnalyst (open_library_analysis, save_library_analysis_as,
    library_analysis, gcps); `panel` – zakładka Biblioteka (połączenie z plikiem)."""

    COLUMNS = 5

    def __init__(self, panel, host, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.host = host
        self.setWindowTitle(tr("Mapy i arkusze"))
        self.resize(760, 460)
        lay = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(self.COLUMNS)
        self.tree.setHeaderLabels([tr("Mapa / arkusz"), tr("Skala"), tr("Punkty"), tr("Skan"), tr("Zmieniono")])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(self.on_double_clicked)
        lay.addWidget(self.tree, 1)
        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)
        row = QHBoxLayout()
        self.btn_new_map = QPushButton(tr("Nowa mapa…"))
        self.btn_new_map.clicked.connect(self.new_map)
        self.btn_new_sheet = QPushButton(tr("Zapisz punkty jako nowy arkusz…"))
        self.btn_new_sheet.setToolTip(tr("Zapisz bieżące punkty GCP jako nowy arkusz zaznaczonej mapy"))
        self.btn_new_sheet.clicked.connect(self.new_sheet)
        self.btn_edit = QPushButton(tr("Edytuj…"))
        self.btn_edit.clicked.connect(self.edit)
        self.btn_delete = QPushButton(tr("Usuń…"))
        self.btn_delete.clicked.connect(self.delete)
        self.btn_open = QPushButton(tr("Wczytaj"))
        self.btn_open.setToolTip(tr("Wczytaj punkty zaznaczonego arkusza (dwuklik)"))
        self.btn_open.clicked.connect(self.open_sheet)
        close = QPushButton(tr("Zamknij"))
        close.clicked.connect(self.reject)
        for b in (self.btn_new_map, self.btn_new_sheet, self.btn_edit, self.btn_delete, self.btn_open):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(close)
        lay.addLayout(row)
        self.reload()

    # --- dane --------------------------------------------------------------------
    def reload(self, select=None):
        """select – ("map", id) albo ("sheet", id) do zaznaczenia po przeładowaniu."""
        self.tree.clear()
        try:
            with self.panel.library() as lib:
                maps = lib.maps()
                sheets = {m.map_id: sorted(lib.analyses(m.map_id), key=lambda a: (a.sheet, a.analysis_id)) for m in maps}
                counts = {a.analysis_id: sum(1 for ln in lib.links(a.analysis_id) if ln.status != "absent")
                          for lst in sheets.values() for a in lst}
        except lb.LibraryError as e:
            self.lbl_info.setText(self.panel._library_error(e))
            return
        cur = self.host.library_analysis.analysis_id if self.host.library_analysis is not None else None
        bold = QFont()
        bold.setBold(True)
        for m in maps:
            top = QTreeWidgetItem([map_label(m), f"1 : {m.scale:,}".replace(",", " ") if m.scale else "",
                                   str(sum(counts[a.analysis_id] for a in sheets[m.map_id])), "", m.modified[:16]])
            top.setData(0, Qt.ItemDataRole.UserRole, ("map", m.map_id))
            top.setToolTip(0, "\n".join(x for x in (m.title, m.author, m.note) if x))
            self.tree.addTopLevelItem(top)
            for a in sheets[m.map_id]:
                it = QTreeWidgetItem([tr("arkusz {s}").format(s=a.sheet) if a.sheet else tr("(bez numeru arkusza)"),
                                      "", str(counts[a.analysis_id]), os.path.basename(a.scan_path or ""), a.modified[:16]])
                it.setData(0, Qt.ItemDataRole.UserRole, ("sheet", a.analysis_id))
                it.setToolTip(3, a.scan_path or "")
                if a.analysis_id == cur:
                    for c in range(self.COLUMNS):
                        it.setFont(c, bold)
                    it.setToolTip(0, tr("Arkusz otwarty w panelu GCP"))
                top.addChild(it)
                if select == ("sheet", a.analysis_id) or (select is None and a.analysis_id == cur):
                    self.tree.setCurrentItem(it)
            top.setExpanded(True)
            if select == ("map", m.map_id):
                self.tree.setCurrentItem(top)
        self.lbl_info.setText(tr("Mapy: {n}, arkusze: {m}. Arkusz to analiza jednego skanu; opis mapy jest wspólny "
                                 "dla jej arkuszy.").format(n=len(maps), m=sum(len(x) for x in sheets.values())))
        self._update_buttons()

    def selected(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it is not None else (None, None)

    def _selected_map_id(self):
        kind, ident = self.selected()
        if kind == "map":
            return ident
        if kind == "sheet":
            return self.tree.currentItem().parent().data(0, Qt.ItemDataRole.UserRole)[1]
        return None

    def _update_buttons(self):
        kind, _ident = self.selected()
        has_points = bool(self.host.gcps) or bool(self.host.absent)
        self.btn_new_sheet.setEnabled(kind is not None and has_points)
        self.btn_edit.setEnabled(kind is not None)
        self.btn_delete.setEnabled(kind is not None)
        self.btn_open.setEnabled(kind == "sheet")

    def _run(self, fn):
        try:
            with self.panel.library() as lib:
                return fn(lib)
        except lb.LibraryError as e:
            self.lbl_info.setText(self.panel._library_error(e))
            return None

    # --- akcje -------------------------------------------------------------------
    def new_map(self):
        m = lb.MapInfo()
        if MapDialog(m, self).exec() != QDialog.DialogCode.Accepted:
            return
        mid = self._run(lambda lib: lib.save_map(m))
        if mid is not None:
            self.reload(("map", mid))

    def new_sheet(self):
        mid = self._selected_map_id()
        if mid is None:
            return
        if self.host.save_library_analysis_as(mid):
            self.reload(("sheet", self.host.library_analysis.analysis_id))

    def edit(self):
        kind, ident = self.selected()
        if kind == "map":
            m = self._run(lambda lib: lib.get_map(ident))
            if m is not None and MapDialog(m, self).exec() == QDialog.DialogCode.Accepted:
                self._run(lambda lib: lib.save_map(m))
                self._sync_host()
                self.reload(("map", ident))
        elif kind == "sheet":
            data = self._run(lambda lib: (lib.get_analysis(ident), lib.maps()))
            if data is None or data[0] is None:
                return
            a, maps = data
            dlg = AnalysisDialog(a, self, maps)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._run(lambda lib: save_with_dialog(lib, a, dlg))
                self._sync_host()
                self.reload(("sheet", ident))

    def _sync_host(self):
        """Opis otwartej analizy po zmianach w bibliotece."""
        cur = self.host.library_analysis
        if cur is None or cur.analysis_id is None:
            return
        a = self._run(lambda lib: lib.get_analysis(cur.analysis_id))
        if a is not None:
            for f in lb.MAP_FIELDS + ("sheet", "dpi", "note", "map_id"):
                setattr(cur, f, getattr(a, f))
            self.host.update_analysis_label()

    def delete(self):
        kind, ident = self.selected()
        if kind == "map":
            m = self._run(lambda lib: (lib.get_map(ident), len(lib.analyses(ident))))
            if m is None:
                return
            m, n = m
            text = (tr("Usunąć mapę „{title}” razem z jej arkuszami ({n}) i ich punktami? Tego nie można cofnąć.")
                    if n else tr("Usunąć mapę „{title}”?")).format(title=m.title or "?", n=n)
            if QMessageBox.question(self, "QMapAnalyst", text) != QMessageBox.StandardButton.Yes:
                return
            sheet_ids = self._run(lambda lib: [a.analysis_id for a in lib.analyses(ident)]) or []
            self._run(lambda lib: lib.delete_map(ident, force=True))
            self._forget_host(sheet_ids)
        elif kind == "sheet":
            text = tr("Usunąć arkusz „{name}” i jego punkty z biblioteki? Tego nie można cofnąć.").format(
                name=self.tree.currentItem().parent().text(0) + " · " + self.tree.currentItem().text(0))
            if QMessageBox.question(self, "QMapAnalyst", text) != QMessageBox.StandardButton.Yes:
                return
            self._run(lambda lib: lib.delete_analysis(ident))
            self._forget_host([ident])
        self.reload()

    def _forget_host(self, analysis_ids):
        """Usunięty arkusz był otwarty – punkty zostają w panelu jako niezapisane."""
        cur = self.host.library_analysis
        if cur is not None and cur.analysis_id in analysis_ids:
            self.host.library_analysis = None
            self.host.update_analysis_label()

    def open_sheet(self):
        kind, ident = self.selected()
        if kind == "sheet" and self.host.open_library_analysis(ident):
            self.accept()

    def on_double_clicked(self, item, column):
        if item.data(0, Qt.ItemDataRole.UserRole)[0] == "sheet":
            self.open_sheet()
