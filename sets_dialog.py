"""
Punkty zestawu biblioteki: lista z nazwami dawnymi i typem, przybliżenie do punktu na mapie głównej,
usuwanie z zestawu (punkty zostają w bibliotece).
"""

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from . import library as lb


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


class SetPointsDialog(QDialog):
    """Punkty zestawu wybranego w panelu biblioteki (panel.current_set())."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        from .library_panel import type_labels
        self.panel = panel
        self.labels = type_labels()
        self.points = []
        self.setWindowTitle(tr("Punkty zestawu"))
        self.resize(560, 480)
        lay = QVBoxLayout(self)
        self.lbl_title = QLabel()
        lay.addWidget(self.lbl_title)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("Nazwa"), tr("Nazwy dawne"), tr("Typ"), tr("W analizie")])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.cellDoubleClicked.connect(lambda row, _col: self.zoom_to(row))
        lay.addWidget(self.table, 1)
        hint = QLabel(tr("Dwuklik – pokaż punkt na mapie głównej i zaznacz go na warstwie biblioteki."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholder-text); font-size: 8pt;")
        lay.addWidget(hint)
        row = QHBoxLayout()
        self.btn_zoom = QPushButton(tr("Pokaż na mapie"))
        self.btn_zoom.clicked.connect(lambda: self.zoom_to(self.table.currentRow()))
        self.btn_remove = QPushButton(tr("Usuń z zestawu"))
        self.btn_remove.setToolTip(tr("Usuń zaznaczone wiersze z zestawu – punkty zostają w bibliotece"))
        self.btn_remove.clicked.connect(self.remove_selected)
        close = QPushButton(tr("Zamknij"))
        close.clicked.connect(self.reject)
        row.addWidget(self.btn_zoom)
        row.addWidget(self.btn_remove)
        row.addStretch()
        row.addWidget(close)
        lay.addLayout(row)
        self.reload()

    def reload(self):
        sid = self.panel.current_set()
        try:
            with self.panel.library() as lib:
                self.points = sorted(lib.points(sid), key=lambda p: lb.normalize(p.name))
                name = next((n for s, n, _d, _c in lib.sets() if s == sid), "?")
        except lb.LibraryError as e:
            self.lbl_title.setText(self.panel._library_error(e))
            return
        used = self.panel._used
        self.lbl_title.setText(tr("Zestaw „{name}”: punkty {n}, w bieżącej analizie {m}").format(
            name=name, n=len(self.points), m=sum(1 for p in self.points if p.uid in used)))
        t = self.table
        t.setRowCount(len(self.points))
        for r, p in enumerate(self.points):
            cells = (p.name, (p.alt_names or "").replace("|", ", "), self.labels.get(p.type, p.type or ""),
                     "✓" if p.uid in used else "")
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c == 3:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                t.setItem(r, c, it)
        self.btn_zoom.setEnabled(bool(self.points))
        self.btn_remove.setEnabled(bool(self.points))

    def selected_points(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.points[r] for r in rows if 0 <= r < len(self.points)]

    def zoom_to(self, row):
        if not 0 <= row < len(self.points):
            return
        p = self.points[row]
        canvas = self.panel.canvas
        if canvas is not None:
            canvas.setCenter(self.panel.to_crs(p, canvas.mapSettings().destinationCrs()))
            canvas.refresh()
        if self.panel.layer is not None:
            self.panel.layer.selectByExpression(f"\"uid\" = '{p.uid}'")

    def remove_selected(self):
        pts = self.selected_points()
        if not pts:
            return
        names = ", ".join(p.name for p in pts[:10]) + ("…" if len(pts) > 10 else "")
        if QMessageBox.question(self, "QMapAnalyst", tr("Usunąć z zestawu punkty ({n}): {names}? "
                                                         "Punkty zostaną w bibliotece.").format(n=len(pts), names=names)) \
                != QMessageBox.StandardButton.Yes:
            return
        sid = self.panel.current_set()
        try:
            with self.panel.library() as lib:
                lib.remove_from_set(sid, [p.uid for p in pts])
        except lb.LibraryError as e:
            self.lbl_title.setText(self.panel._library_error(e))
            return
        self.panel.reload_sets(select=sid)
        self.reload()
