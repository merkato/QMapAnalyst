"""
Zakładka „Biblioteka”: plik biblioteki punktów referencyjnych, jej warstwa na mapie głównej, zestawy.

Punkty dodaje się i edytuje zwykłymi narzędziami QGIS na warstwie biblioteki (formularz ma listę
typów, uid nadaje się automatycznie). Panel zakłada/otwiera plik, filtruje warstwę po zestawie,
zarządza zestawami (z zaznaczenia na mapie) i wyróżnia punkty użyte w bieżącej analizie.

Połączenie OGR z plikiem (library.Library) otwierane jest tylko na czas operacji – warstwa QGIS
trzyma własne połączenie, a długo otwarte drugie mogłoby blokować zapis.
"""

import os
import uuid
from contextlib import contextmanager
from html import escape

from qgis.PyQt.QtCore import QCoreApplication, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QMenu, QMessageBox, QToolButton, QVBoxLayout, QWidget,
)
from qgis.core import (
    QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsDefaultValue,
    QgsEditorWidgetSetup, QgsExpressionContextUtils, QgsFeatureRequest, QgsPointXY, QgsRectangle, QgsFieldConstraints, QgsMarkerSymbol, QgsProject,
    QgsProperty, QgsRendererCategory, QgsSettings, QgsSymbol, QgsSymbolLayer, QgsVectorLayer,
)

from . import importer as im
from . import library as lb
from .layers import set_labels
from .maps_dialog import AnalysisDialog, analysis_years  # noqa: F401 – używane też przez analysis_store
from .widgets import FlowLayout

SETTINGS_PATH = "QMapAnalyst/library_path"
LAYER_PROP = "qmapanalyst/library"      # właściwość warstwy: ścieżka pliku biblioteki
USED_VAR = "qma_used_uids"              # zmienna warstwy: uid punktów użytych w analizie („|”)
SET_VAR = "qma_set_uids"                # zmienna warstwy: uid punktów zestawu – pozostałe przygaszone („|”)
SETTINGS_SET_ONLY = "QMapAnalyst/library_set_only"
STYLE_PROP = "qmapanalyst/style"        # wersja stylu warstwy (styl starszej wersji jest odświeżany)
STYLE_VERSION = "2"

# kształt, kolor wypełnienia, rozmiar [mm]
TYPE_SYMBOLS = {
    "town": ("square", "#c62828", 3.4),
    "village": ("circle", "#8d6e63", 2.6),
    "church": ("cross_fill", "#6a1b9a", 3.4),
    "monastery": ("cross_fill", "#283593", 3.4),
    "castle": ("pentagon", "#37474f", 3.2),
    "manor": ("hexagon", "#5d4037", 3.0),
    "mill": ("star", "#00838f", 3.2),
    "bridge": ("diamond", "#455a64", 3.0),
    "confluence": ("triangle", "#0277bd", 3.0),
    "crossroads": ("cross2", "#424242", 3.0),
    "peak": ("equilateral_triangle", "#2e7d32", 3.2),
    "control": ("triangle", "#ef6c00", 3.0),
    "other": ("circle", "#757575", 2.4),
}
_USED = f"array_contains(string_to_array(coalesce(@{USED_VAR}, ''), '|'), \"uid\")"
_IN_SET = (f"coalesce(@{SET_VAR}, '') = '' or array_contains(string_to_array(@{SET_VAR}, '|'), \"uid\") "
           f"or {_USED}")


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


def type_labels():
    return {
        "town": tr("miasto"), "village": tr("wieś"), "church": tr("kościół"), "monastery": tr("klasztor"),
        "castle": tr("zamek"), "manor": tr("dwór"), "mill": tr("młyn"), "bridge": tr("most"),
        "confluence": tr("ujście"), "crossroads": tr("skrzyżowanie"), "peak": tr("szczyt"),
        "control": tr("punkt osnowy"), "other": tr("inny"),
    }


def status_labels():
    """Status identyfikacji punktu na danej mapie (kody library.STATUSES)."""
    return {"sure": tr("pewny"), "uncertain": tr("niepewny"), "illegible": tr("nieczytelny"),
            "absent": tr("brak na mapie")}


def field_aliases():
    return {
        "uid": tr("identyfikator"), "name": tr("nazwa"), "alt_names": tr("nazwy dawne (oddzielone |)"),
        "type": tr("typ"), "feature": tr("wskazany obiekt"), "sigma_m": tr("niepewność współrzędnych [m]"),
        "valid_from": tr("istnieje od (rok)"), "valid_to": tr("istnieje do (rok)"), "source": tr("źródło"),
        "note": tr("uwagi"),
    }


def _enum(owner, scoped, name):
    """Wyliczenie QGIS – zakresowe (Qt6 / nowsze QGIS) albo płaskie (starsze)."""
    holder = getattr(owner, scoped, None)
    return getattr(holder, name) if holder is not None and hasattr(holder, name) else getattr(owner, name)


def _quote(value):
    return "'" + str(value).replace("'", "''") + "'"


# ----------------------------------------------------------------------
#  Warstwa biblioteki
# ----------------------------------------------------------------------
def _symbol(shape, color, size):
    sym = QgsMarkerSymbol.createSimple({"name": shape, "color": color, "outline_color": "255,255,255",
                                        "outline_width": "0.3", "size": str(size)})
    sl = sym.symbolLayer(0)
    props = sl.dataDefinedProperties()
    props.setProperty(_enum(QgsSymbolLayer, "Property", "StrokeColor"),
                      QgsProperty.fromExpression(f"if({_USED}, '255,190,0,255', '255,255,255,255')"))
    props.setProperty(_enum(QgsSymbolLayer, "Property", "StrokeWidth"),
                      QgsProperty.fromExpression(f"if({_USED}, 0.9, 0.3)"))
    props.setProperty(_enum(QgsSymbolLayer, "Property", "Size"),
                      QgsProperty.fromExpression(f"if({_USED}, {size * 1.45:.2f}, {size})"))
    sl.setDataDefinedProperties(props)
    # punkty spoza wybranego zestawu (gdy mapa nie jest filtrowana) – przygaszone
    sym.setDataDefinedProperty(_enum(QgsSymbol, "Property", "Opacity"),
                               QgsProperty.fromExpression(f"if({_IN_SET}, 100, 25)"))
    return sym


def style_library_layer(layer):
    labels = type_labels()
    cats = [QgsRendererCategory(code, _symbol(*TYPE_SYMBOLS[code]), labels[code]) for code in lb.POINT_TYPES]
    cats.append(QgsRendererCategory(None, _symbol("circle", "#9e9e9e", 2.2), tr("bez typu")))
    layer.setRenderer(QgsCategorizedSymbolRenderer("type", cats))
    set_labels(layer, '"name"', QColor(25, 25, 25), size=8.0)
    layer.setCustomProperty(STYLE_PROP, STYLE_VERSION)


def configure_form(layer):
    """Formularz edycji w QGIS: lista typów, automatyczny uid, aliasy pól, podpowiedź na mapie."""
    fields = layer.fields()
    for name, alias in field_aliases().items():
        idx = fields.indexOf(name)
        if idx >= 0:
            layer.setFieldAlias(idx, alias)
    uid = fields.indexOf("uid")
    if uid >= 0:
        layer.setDefaultValueDefinition(uid, QgsDefaultValue("uuid('WithoutBraces')"))
        layer.setFieldConstraint(uid, _enum(QgsFieldConstraints, "Constraint", "ConstraintNotNull"))
        layer.setFieldConstraint(uid, _enum(QgsFieldConstraints, "Constraint", "ConstraintUnique"))
        form = layer.editFormConfig()
        form.setReadOnly(uid, True)
        layer.setEditFormConfig(form)
    typ = fields.indexOf("type")
    if typ >= 0:
        labels = type_labels()
        layer.setEditorWidgetSetup(typ, QgsEditorWidgetSetup(
            "ValueMap", {"map": [{labels[c]: c} for c in lb.POINT_TYPES]}))
    layer.setDisplayExpression('"name"')
    layer.setMapTipTemplate(
        "<b>[% \"name\" %]</b>[% if(coalesce(\"alt_names\", '') <> '', '<br>' || replace(\"alt_names\", '|', ', '), '') %]"
        "<br><i>[% represent_value(\"type\") %]</i>"
        "[% if(coalesce(\"feature\", '') <> '', ' – ' || \"feature\", '') %]")


# ----------------------------------------------------------------------
#  Panel
# ----------------------------------------------------------------------
def _tool(icon, text, tip, icon_only=False):
    b = QToolButton()
    ic = QgsApplication.getThemeIcon(icon) if icon else None
    if ic is not None and not ic.isNull():
        b.setIcon(ic)
    else:
        icon_only = False
    b.setText(text)
    b.setToolTip(tip)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly if icon_only else Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    b.setAutoRaise(True)
    return b


class LibraryPanel(QWidget):
    """Zakładka biblioteki punktów. status(text, level) – komunikaty na pasku stanu panelu."""

    libraryChanged = pyqtSignal()
    saveAnalysisRequested = pyqtSignal()
    mapsRequested = pyqtSignal()
    editAnalysisRequested = pyqtSignal()
    openAnalysisIdRequested = pyqtSignal(int)
    setChanged = pyqtSignal()                # zmiana wybranego zestawu lub listy zestawów

    def __init__(self, status=None, parent=None):
        super().__init__(parent)
        self.project = QgsProject.instance()
        self.status = status or (lambda text, level="info": None)
        self.path = None
        self.layer = None
        self._used = set()
        self._crs_authid = ""
        self._crs_wkt = ""
        self._cache = None          # (punkty, {set_id: [uid]}) – do wyszukiwania i „następnego z zestawu”
        # bieżący stan analizy w panelu GCP: () -> (Analysis lub None, [Link], niezapisane) – dla macierzy pokrycia
        self.current_analysis = None
        self.canvas = None          # mapa główna (przybliżanie do punktu) – ustawia panel QMapAnalyst
        self._build()
        self.project.layersWillBeRemoved.connect(self._on_layers_removed)
        path = QgsSettings().value(SETTINGS_PATH, "", type=str)
        if path and os.path.isfile(path):
            self.use_library(path, quiet=True)
        self._update_state()

    # --- interfejs ---------------------------------------------------------
    def _build(self):
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.lbl_file = QLabel()
        self.lbl_file.setWordWrap(True)
        row.addWidget(self.lbl_file, 1)
        self.btn_open = _tool("/mActionFileOpen.svg", tr("Otwórz…"), tr("Otwórz plik biblioteki punktów (GeoPackage)"), True)
        self.btn_open.clicked.connect(self.open_dialog)
        self.btn_new = _tool("/mActionNewGeoPackageLayer.svg", tr("Nowa…"), tr("Załóż nowy plik biblioteki punktów"), True)
        self.btn_new.clicked.connect(self.create_dialog)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_new)
        lay.addLayout(row)

        row = QHBoxLayout()
        self.chk_layer = QCheckBox(tr("Warstwa na mapie głównej"))
        self.chk_layer.setToolTip(tr("Pokaż punkty biblioteki na mapie głównej QGIS (styl według typu, etykiety z nazwą). "
                                     "Punkty użyte w bieżącej analizie mają żółtą obwódkę."))
        self.chk_layer.toggled.connect(self.on_layer_toggled)
        lay.addWidget(self.chk_layer)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Zestaw:")))
        self.cmb_set = QComboBox()
        self.cmb_set.setMinimumContentsLength(12)
        self.cmb_set.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_set.setToolTip(tr("Zestaw roboczy: do niego dodajesz i z niego usuwasz punkty; z niego pochodzą "
                                   "„Następny z zestawu”, wyszukiwarka i macierz pokrycia. Punkty spoza zestawu są na "
                                   "mapie przygaszone (albo ukryte – „Tylko punkty zestawu”)."))
        self.cmb_set.currentIndexChanged.connect(self.apply_filter)
        row.addWidget(self.cmb_set, 1)
        self.btn_set_menu = _tool("/mActionOptions.svg", tr("Zestawy"), tr("Operacje na zestawach"))
        self.btn_set_menu.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.btn_set_menu)
        self.act_set_new = menu.addAction(QgsApplication.getThemeIcon("/mActionNewTableRow.svg"),
                                          tr("Nowy zestaw z zaznaczonych…"), self.new_set_dialog)
        self.act_set_new.setToolTip(tr("Załóż zestaw z punktów zaznaczonych na warstwie biblioteki (lub pusty)"))
        self.act_set_from_analysis = menu.addAction(QgsApplication.getThemeIcon("/mActionNewTableRow.svg"),
                                                    tr("Nowy zestaw z punktów analizy…"), self.new_set_from_analysis)
        menu.addSeparator()
        self.act_set_points = menu.addAction(QgsApplication.getThemeIcon("/mActionOpenTable.svg"),
                                             tr("Punkty zestawu…"), self.set_points_dialog)
        self.act_set_rename = menu.addAction(QgsApplication.getThemeIcon("/mActionEditTable.svg"),
                                             tr("Zmień nazwę…"), self.rename_set_dialog)
        self.act_set_delete = menu.addAction(QgsApplication.getThemeIcon("/mActionDeleteSelected.svg"),
                                             tr("Usuń zestaw…"), self.delete_set)
        self.btn_set_menu.setMenu(menu)
        row.addWidget(self.btn_set_menu)
        lay.addLayout(row)

        row = FlowLayout(spacing=2)
        self.chk_set_only = QCheckBox(tr("Tylko punkty zestawu"))
        self.chk_set_only.setToolTip(tr("Ukryj na mapie punkty spoza wybranego zestawu. Bez tej opcji są przygaszone "
                                        "i można je zaznaczyć, by dodać do zestawu."))
        self.chk_set_only.setChecked(QgsSettings().value(SETTINGS_SET_ONLY, False, type=bool))
        self.chk_set_only.toggled.connect(self.on_set_only_toggled)
        self.btn_set_add = _tool("/mActionAdd.svg", tr("Dodaj zaznaczone"), tr("Dodaj zaznaczone punkty do wybranego zestawu"))
        self.btn_set_add.clicked.connect(self.add_selected_to_set)
        self.btn_set_remove = _tool("/mActionRemove.svg", tr("Usuń zaznaczone"), tr("Usuń zaznaczone punkty z wybranego zestawu (punkty zostają w bibliotece)"))
        self.btn_set_remove.clicked.connect(self.remove_selected_from_set)
        for w in (self.chk_set_only, self.btn_set_add, self.btn_set_remove):
            row.addWidget(w)
        lay.addLayout(row)

        row = FlowLayout(spacing=2)
        self.btn_import = _tool("/mActionSharingImport.svg", tr("Importuj…"), tr(
            "Import punktów z warstwy wektorowej projektu (np. PRNG, OSM, BDOT10k) lub pliku CSV – "
            "z dopasowaniem pól, rozpoznawaniem typów i pomijaniem duplikatów"))
        self.btn_import.clicked.connect(self.import_dialog)
        self.btn_coverage = _tool("/mActionOpenTable.svg", tr("Macierz pokrycia…"), tr(
            "Punkty wybranego zestawu × mapy zapisane w bibliotece: obecne, niepewne, odstające, nieczytelne, "
            "brak na mapie; punkty wspólne wszystkim mapom; eksport CSV/JSON"))
        self.btn_coverage.clicked.connect(self.coverage_dialog)
        self.btn_series = _tool("/mActionShowRasterCalculator.svg", tr("Seria czasowa…"), tr(
            "Porównanie map z różnych lat dla zestawu (wydawnictwo wieloarkuszowe – jedna mapa): statystyki map "
            "(σ₀, RMSE, skala, obrót), błędy położenia punktów, podobieństwo reszt (ślad kopiowania), wektory "
            "błędów i trajektorie punktów na mapie"))
        self.btn_series.clicked.connect(self.series_dialog)
        for b in (self.btn_import, self.btn_coverage, self.btn_series):
            row.addWidget(b)
        lay.addLayout(row)

        lay.addWidget(self._build_analysis_box())

        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)
        hint = QLabel(tr("Punkty dodajesz i poprawiasz narzędziami edycji QGIS na warstwie biblioteki – identyfikator "
                         "nadawany jest automatycznie. Zestaw regionalny: zaznacz punkty na mapie i wybierz "
                         "„Nowy zestaw z zaznaczonych…” z menu zestawów albo „Dodaj zaznaczone”."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholder-text); font-size: 8pt;")
        lay.addWidget(hint)
        lay.addStretch()

    def _build_analysis_box(self):
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(box)
        v.setContentsMargins(6, 4, 6, 4)
        self.lbl_analysis = QLabel()
        self.lbl_analysis.setWordWrap(True)
        v.addWidget(self.lbl_analysis)
        row = FlowLayout(spacing=2)
        self.btn_an_save = _tool("/mActionFileSave.svg", tr("Zapisz analizę"), tr(
            "Zapisz punkty GCP bieżącej mapy w bibliotece: położenie na skanie, współrzędne odniesienia, status, "
            "uwagi oraz punkty zestawu oznaczone jako „brak na mapie”."))
        self.btn_an_save.clicked.connect(self.saveAnalysisRequested)
        self.btn_an_open = _tool("/mActionFileOpen.svg", tr("Mapy i arkusze…"), tr(
            "Mapy i ich arkusze zapisane w bibliotece: wczytanie arkusza, zapis bieżących punktów jako nowego arkusza, "
            "edycja opisu, przeniesienie arkusza do innej mapy, usuwanie"))
        self.btn_an_open.clicked.connect(self.mapsRequested)
        self.btn_an_meta = _tool("/mActionFormView.svg", tr("Opis mapy…"), tr("Tytuł, autor, rok, skala, arkusz mapy"))
        self.btn_an_meta.clicked.connect(self.editAnalysisRequested)
        for b in (self.btn_an_save, self.btn_an_open, self.btn_an_meta):
            row.addWidget(b)
        v.addLayout(row)
        self.set_analysis_label(None)
        return box

    def set_analysis_label(self, analysis, dirty=False):
        if analysis is None or analysis.analysis_id is None:
            text = tr("Analiza mapy: nie zapisana w bibliotece.")
            if dirty:
                text += " " + tr("Zapisz ją, aby punkty były widoczne w macierzy pokrycia.")
            self.lbl_analysis.setText(text)
            return
        years = analysis_years(analysis)
        text = tr("Analiza mapy: <b>{title}</b>").format(title=escape(analysis.title or "?"))
        if years:
            text += f" ({years})"
        if analysis.sheet:
            text += " · " + escape(tr("arkusz {s}").format(s=analysis.sheet))
        if dirty:
            text += " · <b>" + escape(tr("niezapisane zmiany")) + "</b>"
        self.lbl_analysis.setText(text)

    def _update_state(self):
        has = self.path is not None
        if has:
            self.lbl_file.setText(tr("Biblioteka: <b>{name}</b> ({crs})").format(
                name=escape(os.path.basename(self.path)), crs=self._crs_authid or "?"))
            self.lbl_file.setToolTip(self.path)
        else:
            self.lbl_file.setText(tr("Brak biblioteki punktów – otwórz istniejący plik lub załóż nowy."))
            self.lbl_file.setToolTip("")
        sid = self.current_set()
        for w in (self.chk_layer, self.cmb_set, self.btn_set_menu, self.btn_import, self.act_set_new,
                  self.act_set_from_analysis):
            w.setEnabled(has)
        for b in (self.btn_set_add, self.btn_set_remove):
            b.setEnabled(has and sid is not None and self.layer is not None)
        for b in (self.act_set_rename, self.act_set_delete, self.act_set_points, self.btn_coverage, self.btn_series,
                  self.chk_set_only):
            b.setEnabled(has and sid is not None)
        for b in (self.btn_an_save, self.btn_an_open, self.btn_an_meta):
            b.setEnabled(has)
        self.chk_layer.blockSignals(True)
        self.chk_layer.setChecked(self.layer is not None and self._layer_visible())
        self.chk_layer.blockSignals(False)

    # --- plik biblioteki -------------------------------------------------------
    @contextmanager
    def library(self):
        lib = lb.Library.open(self.path)
        try:
            yield lib
        finally:
            lib.close()

    def _library_error(self, exc):
        if isinstance(exc, lb.SchemaTooNew):
            return tr("Plik utworzyła nowsza wersja QMapAnalyst (schemat {v}). Zaktualizuj wtyczkę.").format(v=exc.found)
        if isinstance(exc, lb.NotALibrary):
            return tr("Plik nie jest biblioteką punktów QMapAnalyst.")
        if isinstance(exc, lb.PointInUse):
            return tr("Punkt jest użyty w analizach: {ids}.").format(ids=", ".join(map(str, exc.analyses)))
        return tr("Błąd biblioteki punktów: {err}").format(err=exc)

    def use_library(self, path, quiet=False):
        """Ustawia bieżący plik biblioteki. Zwraca True, gdy się udało."""
        try:
            with lb.Library.open(path) as lib:
                self._crs_authid = lib.crs_authid
                self._crs_wkt = lib.crs_wkt
        except lb.LibraryError as e:
            if not quiet:
                QMessageBox.warning(self, "QMapAnalyst", self._library_error(e))
            self.status(self._library_error(e), "error")
            return False
        if self.layer is not None and self._layer_path(self.layer) != self._norm(path):
            self.project.removeMapLayer(self.layer.id())
            self.layer = None
        self.path = os.path.abspath(path)
        QgsSettings().setValue(SETTINGS_PATH, self.path)
        self.layer = self._find_layer()
        if self.layer is not None:
            self._prepare_layer(self.layer)
        self.reload_sets()
        if not quiet:
            self.status(tr("Biblioteka punktów: {f}").format(f=os.path.basename(self.path)), "ok")
        self.libraryChanged.emit()
        return True

    def open_dialog(self):
        start = os.path.dirname(self.path) if self.path else ""
        path, _f = QFileDialog.getOpenFileName(self, tr("Otwórz bibliotekę punktów"), start, "GeoPackage (*.gpkg)")
        if path and self.use_library(path):
            self.show_layer(True)

    def create_dialog(self):
        path, _f = QFileDialog.getSaveFileName(self, tr("Nowa biblioteka punktów"), "", "GeoPackage (*.gpkg)")
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        crs = self.ask_crs()
        if crs is None:
            return
        if os.path.exists(path):
            QMessageBox.warning(self, "QMapAnalyst", tr("Plik {f} już istnieje – wybierz inną nazwę.").format(f=os.path.basename(path)))
            return
        self.create_library(path, crs)

    def ask_crs(self):
        """Układ współrzędnych nowej biblioteki (domyślnie układ projektu)."""
        from qgis.gui import QgsProjectionSelectionDialog
        dlg = QgsProjectionSelectionDialog(self)
        dlg.setWindowTitle(tr("Układ współrzędnych biblioteki"))
        crs = self.project.crs()
        dlg.setCrs(crs if crs.isValid() else QgsCoordinateReferenceSystem("EPSG:2180"))
        if not dlg.exec():
            return None
        return dlg.crs()

    def create_library(self, path, crs):
        definition = crs.authid() or crs.toWkt()
        try:
            lb.Library.create(path, definition).close()
        except lb.LibraryError as e:
            QMessageBox.warning(self, "QMapAnalyst", self._library_error(e))
            return False
        if self.use_library(path):
            self.show_layer(True)
            return True
        return False

    # --- warstwa ------------------------------------------------------------------
    @staticmethod
    def _norm(path):
        return os.path.normcase(os.path.abspath(path))

    def _layer_path(self, layer):
        return self._norm(layer.customProperty(LAYER_PROP, "") or "x")

    def _find_layer(self):
        """Warstwa biblioteki już obecna w projekcie (np. zapisana w pliku projektu)."""
        want = self._norm(self.path)
        for layer in self.project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.customProperty(LAYER_PROP) and self._layer_path(layer) == want:
                return layer
        return None

    def _prepare_layer(self, layer):
        if layer.customProperty(STYLE_PROP) != STYLE_VERSION:
            style_library_layer(layer)          # warstwa z projektu zapisanego starszą wersją
        configure_form(layer)
        layer.selectionChanged.connect(self._on_selection_changed)
        layer.editingStopped.connect(self.apply_filter)
        layer.afterCommitChanges.connect(self.invalidate_cache)
        QgsExpressionContextUtils.setLayerVariable(layer, USED_VAR, "|".join(sorted(self._used)))
        layer.triggerRepaint()

    def _layer_visible(self):
        node = self.project.layerTreeRoot().findLayer(self.layer.id()) if self.layer else None
        return node is not None and node.isVisible()

    def show_layer(self, visible=True):
        if self.path is None:
            return None
        if self.layer is None and visible:
            layer = QgsVectorLayer(f"{self.path}|layername=ref_points", tr("Biblioteka punktów"), "ogr")
            if not layer.isValid():
                self.status(tr("Nie można wczytać warstwy biblioteki z pliku {f}.").format(f=self.path), "error")
                return None
            layer.setCustomProperty(LAYER_PROP, self.path)
            style_library_layer(layer)
            self._prepare_layer(layer)
            self.project.addMapLayer(layer, False)
            self.project.layerTreeRoot().insertLayer(0, layer)
            self.layer = layer
            self.apply_filter()
        elif self.layer is not None:
            node = self.project.layerTreeRoot().findLayer(self.layer.id())
            if node is not None:
                node.setItemVisibilityChecked(visible)
        self._update_state()
        return self.layer

    def on_layer_toggled(self, checked):
        self.show_layer(checked)

    def _on_selection_changed(self, *args):
        self._update_info()
        self._update_state()

    def _on_layers_removed(self, ids):
        if self.layer is not None and self.layer.id() in ids:
            self.layer = None
            self._update_state()

    def set_used_uids(self, uids):
        """Punkty biblioteki użyte w bieżącej analizie – wyróżnione na warstwie."""
        uids = {u for u in uids if u}
        if uids == self._used:
            return
        self._used = uids
        if self.layer is not None:
            QgsExpressionContextUtils.setLayerVariable(self.layer, USED_VAR, "|".join(sorted(uids)))
            self.layer.triggerRepaint()

    # --- punkty z analizy ----------------------------------------------------------------
    def library_crs(self):
        crs = QgsCoordinateReferenceSystem(self._crs_authid) if self._crs_authid else QgsCoordinateReferenceSystem()
        if not crs.isValid() and self._crs_wkt:
            crs = QgsCoordinateReferenceSystem.fromWkt(self._crs_wkt)
        return crs

    def add_reference_points(self, items, crs, source=""):
        """Dodaje punkty odniesienia (np. z tabeli GCP) do biblioteki i do wybranego zestawu.

        items – lista (nazwa, x, y, uid lub None) we współrzędnych układu crs.
        Punkt już powiązany (uid w bibliotece) lub leżący w bibliotece w odległości ≤ 1 m (1e-5° dla
        układów geograficznych) jest wiązany z istniejącym zamiast dublowany.
        Zwraca (lista uid w kolejności items, liczba nowych, liczba powiązanych) albo None."""
        if self.path is None:
            self.status(tr("Najpierw otwórz lub załóż bibliotekę punktów (zakładka Biblioteka)."), "warn")
            return None
        if self.layer is not None and not self._save_pending_edits():
            return None
        lib_crs = self.library_crs()
        ct = None
        if crs is not None and crs.isValid() and lib_crs.isValid() and crs != lib_crs:
            ct = QgsCoordinateTransform(crs, lib_crs, self.project)
        tol = 1e-5 if lib_crs.isValid() and lib_crs.isGeographic() else 1.0
        try:
            with self.library() as lib:
                result, new_count, linked = self._merge_points(lib, items, ct, tol, source)
                sid = self.current_set()
                if sid is not None:
                    lib.add_to_set(sid, result)
        except (lb.LibraryError, QgsCsException) as e:
            self.status(self._library_error(e), "error")
            return None
        self.refresh_layer()
        self.reload_sets(select=self.current_set())
        return result, new_count, linked

    @staticmethod
    def _merge_points(lib, items, ct, tol, source):
        existing = lib.points()
        result, new_points = [], []
        linked = 0
        for name, x, y, uid in items:
            if uid and lib.get_point(uid) is not None:
                result.append(uid)
                linked += 1
                continue
            if ct is not None:
                pt = ct.transform(float(x), float(y))
                x, y = pt.x(), pt.y()
            near = min(existing, key=lambda p: (p.x - x) ** 2 + (p.y - y) ** 2, default=None)
            if near is not None and ((near.x - x) ** 2 + (near.y - y) ** 2) ** 0.5 <= tol:
                result.append(near.uid or near)  # bez uid – nowy punkt z tej samej listy
                linked += 1
                continue
            p = lb.RefPoint(name, float(x), float(y), source=source)
            new_points.append(p)
            existing.append(p)
            result.append(p)
        lib.add_points(new_points)  # nadaje uid obiektom RefPoint
        return [r.uid if isinstance(r, lb.RefPoint) else r for r in result], len(new_points), linked

    # --- import ---------------------------------------------------------------------------
    def sets_list(self):
        try:
            with self.library() as lib:
                return lib.sets()
        except lb.LibraryError:
            return []

    def coverage_dialog(self):
        if self.current_set() is None:
            self.status(tr("Wybierz zestaw punktów."), "warn")
            return
        if self.layer is not None and not self._save_pending_edits():
            return
        from .coverage_dialog import CoverageDialog
        dlg = CoverageDialog(self, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.chosen_analysis is not None:
            self.openAnalysisIdRequested.emit(dlg.chosen_analysis)

    def series_dialog(self):
        if self.current_set() is None:
            self.status(tr("Wybierz zestaw punktów."), "warn")
            return
        if self.layer is not None and not self._save_pending_edits():
            return
        from .series_dialog import SeriesDialog
        SeriesDialog(self, self).exec()

    def import_dialog(self):
        from .import_dialog import ImportDialog
        dlg = ImportDialog(self, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            added, dups, near, merged = dlg.result_stats
            msg = tr("Import: dodano {a}, pominięto duplikaty: {d}").format(a=added, d=dups)
            if merged:
                msg += ", " + tr("uzupełniono nazwy dawne: {m}").format(m=merged)
            if near:
                msg += ", " + tr("blisko istniejących (inna nazwa): {n}").format(n=near)
            self.status(msg + ".", "ok")

    def import_points(self, points, tol, set_id=None, new_set=None, merge_names=True):
        """Dodaje punkty z pominięciem duplikatów; zwraca (dodane, duplikaty, bliskie, uzupełnione) albo None."""
        if self.path is None:
            return None
        if self.layer is not None and not self._save_pending_edits():
            return None
        try:
            with self.library() as lib:
                to_add, dups, near = im.find_duplicates(lib.points(), points, tol)
                lib.add_points(to_add)
                merged = self._merge_duplicate_names(lib, dups) if merge_names else 0
                uids = [p.uid for p in to_add] + [q.uid for _p, q in dups]
                if new_set:
                    set_id = lib.add_set(new_set)
                if set_id is not None:
                    lib.add_to_set(set_id, list(dict.fromkeys(uids)))
        except lb.LibraryError as e:
            self.status(self._library_error(e), "error")
            return None
        self.refresh_layer()
        self.reload_sets(select=set_id if set_id is not None else self.current_set())
        return len(to_add), len(dups), len(near), merged

    @staticmethod
    def _merge_duplicate_names(lib, dups):
        merged = 0
        for p, q in dups:
            if q.uid and im.merge_alt_names(q, p):
                lib.update_point(q)
                merged += 1
        return merged

    def refresh_layer(self):
        self.invalidate_cache()
        if self.layer is not None:
            self.layer.dataProvider().reloadData()
            self.layer.triggerRepaint()

    # --- wyszukiwanie i wskazywanie punktów -------------------------------------------
    def invalidate_cache(self, *args):
        self._cache = None

    def _cached(self):
        if self._cache is None:
            points, members = [], {}
            if self.path is not None:
                try:
                    with self.library() as lib:
                        points = lib.points()
                        members = {sid: lib.set_members(sid) for sid, *_rest in lib.sets()}
                except lb.LibraryError as e:
                    self.status(self._library_error(e), "error")
            self._cache = (points, members)
        return self._cache

    def set_points(self):
        """Punkty wybranego zestawu albo None, gdy zestaw nie jest wybrany."""
        sid = self.current_set()
        if sid is None:
            return None
        points, members = self._cached()
        uids = set(members.get(sid, ()))
        return [p for p in points if p.uid in uids]

    def search(self, text, near=None, limit=30):
        """Wyszukiwanie w bibliotece (w wybranym zestawie, gdy jest wybrany); near w układzie biblioteki."""
        points = self.set_points()
        if points is None:
            points = self._cached()[0]
        return lb.search_points(points, text, near, limit)

    def to_crs(self, p, crs):
        """Współrzędne punktu biblioteki w układzie crs (QgsPointXY)."""
        lib_crs = self.library_crs()
        pt = QgsPointXY(p.x, p.y)
        if crs is None or not crs.isValid() or not lib_crs.isValid() or crs == lib_crs:
            return pt
        return QgsCoordinateTransform(lib_crs, crs, self.project).transform(pt)

    def from_crs(self, pt, crs):
        """Punkt z układu crs w układzie biblioteki – (x, y) lub None przy błędzie przeliczenia."""
        lib_crs = self.library_crs()
        if crs is None or not crs.isValid() or not lib_crs.isValid() or crs == lib_crs:
            return pt.x(), pt.y()
        try:
            q = QgsCoordinateTransform(crs, lib_crs, self.project).transform(QgsPointXY(pt))
        except QgsCsException:
            return None
        return q.x(), q.y()

    def _metre(self):
        """Długość metra w jednostkach układu biblioteki (przybliżenie dla układu geograficznego)."""
        crs = self.library_crs()
        return 1 / 111320.0 if crs.isValid() and crs.isGeographic() else 1.0

    def match_reference(self, name, pt, crs, exclude=(), named_m=5000.0, near_m=500.0):
        """Punkt biblioteki dla punktu GCP spoza biblioteki (pt w układzie crs):
        (RefPoint, odległość [m], po_nazwie) albo None – zob. library.match_point."""
        xy = self.from_crs(pt, crs)
        if xy is None:
            return None
        unit = self._metre()
        m = lb.match_point(self._cached()[0], name, xy[0], xy[1], named_m * unit, near_m * unit, exclude)
        return None if m is None else (m[0], m[1] / unit, m[2])

    def snap(self, canvas_point, canvas, tolerance_px=10):
        """Punkt widocznej warstwy biblioteki najbliższy kliknięciu (w promieniu tolerance_px) albo None.
        Uwzględnia filtr zestawu i punkty jeszcze niezapisane (bufor edycji)."""
        if self.layer is None or not self._layer_visible():
            return None
        settings = canvas.mapSettings()
        r = tolerance_px * canvas.mapUnitsPerPixel()
        rect = QgsRectangle(canvas_point.x() - r, canvas_point.y() - r, canvas_point.x() + r, canvas_point.y() + r)
        try:
            rect = settings.mapToLayerCoordinates(self.layer, rect)
            click = settings.mapToLayerCoordinates(self.layer, QgsPointXY(canvas_point))
        except QgsCsException:
            return None
        best, best_d = None, None
        for f in self.layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
            g = f.geometry()
            if g is None or g.isEmpty() or not f["uid"]:
                continue
            pt = g.asPoint()
            d = pt.sqrDist(click)
            if best_d is None or d < best_d:
                best, best_d = (f, pt), d
        if best is None:
            return None
        f, pt = best
        return lb.RefPoint(str(f["name"] or ""), pt.x(), pt.y(), uid=str(f["uid"]),
                           alt_names=str(f["alt_names"] or ""), type=str(f["type"] or ""))

    # --- zestawy ---------------------------------------------------------------------
    def current_set(self):
        return self.cmb_set.currentData()

    def reload_sets(self, select=None):
        self.invalidate_cache()
        keep = select if select is not None else self.current_set()
        self.cmb_set.blockSignals(True)
        self.cmb_set.clear()
        total = 0
        sets = []
        if self.path is not None:
            try:
                with self.library() as lib:
                    sets = lib.sets()
                    total = len(lib.points())
            except lb.LibraryError as e:
                self.status(self._library_error(e), "error")
        self.cmb_set.addItem(tr("Wszystkie punkty ({n})").format(n=total), None)
        for sid, name, desc, n in sets:
            self.cmb_set.addItem(f"{name} ({n})", sid)
            self.cmb_set.setItemData(self.cmb_set.count() - 1, desc or name, Qt.ItemDataRole.ToolTipRole)
        idx = self.cmb_set.findData(keep) if keep is not None else 0
        self.cmb_set.setCurrentIndex(max(idx, 0))
        self.cmb_set.blockSignals(False)
        self.apply_filter()

    def on_set_only_toggled(self, checked):
        QgsSettings().setValue(SETTINGS_SET_ONLY, bool(checked))
        self.apply_filter()

    def apply_filter(self, *args):
        """Zestaw roboczy na mapie: filtr warstwy („Tylko punkty zestawu”) albo przygaszenie pozostałych."""
        sid = self.current_set()
        if self.layer is not None:
            uids = []
            if sid is not None:
                try:
                    with self.library() as lib:
                        uids = lib.set_members(sid)
                except lb.LibraryError as e:
                    self.status(self._library_error(e), "error")
            only = sid is not None and self.chk_set_only.isChecked()
            subset = (f"\"uid\" IN ({', '.join(_quote(u) for u in uids)})" if uids else "0") if only else ""
            if self.layer.subsetString() != subset:
                if self.layer.isEditable():
                    # QGIS nie zmienia filtra warstwy w trakcie edycji – zastosuje go editingStopped
                    self.status(tr("Filtr zestawu zostanie zastosowany po zakończeniu edycji warstwy biblioteki."), "warn")
                else:
                    self.layer.setSubsetString(subset)
            dim = "|".join(sorted(uids)) if sid is not None and not only else ""
            if sid is not None and not only and not uids:
                dim = "-"                       # pusty zestaw – wszystkie punkty przygaszone
            QgsExpressionContextUtils.setLayerVariable(self.layer, SET_VAR, dim)
            self.layer.triggerRepaint()
        self._update_info()
        self._update_state()
        self.setChanged.emit()

    def _update_info(self):
        if self.path is None:
            self.lbl_info.setText("")
            return
        sel = len(self.selected_uids())
        used = len(self._used)
        parts = [tr("zaznaczone na mapie: {n}").format(n=sel), tr("użyte w analizie: {n}").format(n=used)]
        self.lbl_info.setText(" · ".join(parts))

    def selected_uids(self):
        if self.layer is None:
            return []
        return [f["uid"] for f in self.layer.selectedFeatures() if f["uid"]]

    def _ask(self, text):
        return QMessageBox.question(self, "QMapAnalyst", text) == QMessageBox.StandardButton.Yes

    def _commit(self, stop_editing):
        if self.layer.commitChanges(stop_editing):
            return True
        self.status(tr("Nie zapisano zmian warstwy biblioteki: {err}").format(
            err="; ".join(self.layer.commitErrors())), "error")
        return False

    def _save_pending_edits(self):
        """Zestawy odwołują się do punktów zapisanych w pliku – niezapisane edycje trzeba najpierw zapisać."""
        if not (self.layer.isEditable() and self.layer.isModified()):
            return True
        if not self._ask(tr("Warstwa biblioteki ma niezapisane zmiany (np. nowe punkty). "
                            "Zestawy obejmują tylko punkty zapisane w pliku. Zapisać zmiany teraz?")):
            self.status(tr("Najpierw zapisz zmiany warstwy biblioteki."), "warn")
            return False
        return self._commit(False)

    def _fill_missing_uids(self, feats):
        """Punkty bez identyfikatora (np. wklejone z innej warstwy) – nadanie uid za zgodą użytkownika."""
        missing = [f.id() for f in feats if not f["uid"]]
        if not missing:
            return True
        if not self._ask(tr("Zaznaczone punkty bez identyfikatora: {n} (np. wklejone z innej warstwy). "
                            "Nadać im identyfikatory?").format(n=len(missing))):
            return False
        if not self.layer.isEditable() and not self.layer.startEditing():
            return False
        idx = self.layer.fields().indexOf("uid")
        for fid in missing:
            self.layer.changeAttributeValue(fid, idx, str(uuid.uuid4()))
        return True

    def selection_for_set(self):
        """uid zaznaczonych punktów gotowych do zestawu albo None (przerwano / brak zaznaczenia)."""
        if self.layer is None:
            return None
        # najpierw uid w buforze edycji (plik wymaga uid), potem zapis zmian
        if not self._fill_missing_uids(self.layer.selectedFeatures()):
            return None
        if not self._save_pending_edits():
            return None
        return self.selected_uids()

    def _require_selection(self):
        uids = self.selection_for_set()
        if uids == []:
            msg = tr("Zaznacz punkty na warstwie biblioteki (narzędzie zaznaczania QGIS).")
            if self.chk_set_only.isChecked():
                msg += " " + tr("Punkty spoza zestawu są ukryte – wyłącz „Tylko punkty zestawu”, aby je zaznaczyć.")
            self.status(msg, "warn")
        return uids or None

    def new_set_dialog(self):
        uids = self.selection_for_set() if self.layer is not None else []
        if uids is None:
            return
        name, ok = QInputDialog.getText(self, tr("Nowy zestaw"),
                                        tr("Nazwa zestawu (zaznaczonych punktów: {n}):").format(n=len(uids)))
        if ok and name.strip():
            self.create_set(name.strip(), uids)

    def new_set_from_analysis(self):
        """Zestaw z punktów biblioteki bieżącej analizy (także „brak na mapie”)."""
        analysis, links, _dirty = self.current_analysis() if self.current_analysis is not None else (None, [], False)
        uids = list(dict.fromkeys(ln.uid for ln in links if ln.uid))
        if not uids:
            self.status(tr("Bieżąca analiza nie ma punktów powiązanych z biblioteką."), "warn")
            return None
        default = analysis.title if analysis is not None else ""
        name, ok = QInputDialog.getText(self, tr("Nowy zestaw"), tr("Nazwa zestawu (punktów analizy: {n}):").format(n=len(uids)),
                                        text=default)
        if ok and name.strip():
            return self.create_set(name.strip(), uids)
        return None

    def set_points_dialog(self):
        if self.current_set() is None:
            return
        from .sets_dialog import SetPointsDialog
        SetPointsDialog(self, self).exec()

    def create_set(self, name, uids=()):
        try:
            with self.library() as lib:
                sid = lib.add_set(name, uids=list(uids))
        except lb.LibraryError as e:
            self.status(self._library_error(e), "error")
            return None
        self.reload_sets(select=sid)
        self.status(tr("Utworzono zestaw „{name}” ({n} pkt).").format(name=name, n=len(uids)), "ok")
        return sid

    def _change_members(self, add):
        sid = self.current_set()
        uids = self._require_selection()
        if sid is None or not uids:
            return
        try:
            with self.library() as lib:
                (lib.add_to_set if add else lib.remove_from_set)(sid, uids)
        except lb.LibraryError as e:
            self.status(self._library_error(e), "error")
            return
        self.reload_sets(select=sid)
        msg = tr("Dodano do zestawu: {n} pkt.") if add else tr("Usunięto z zestawu: {n} pkt.")
        self.status(msg.format(n=len(uids)), "ok")

    def add_selected_to_set(self):
        self._change_members(True)

    def remove_selected_from_set(self):
        self._change_members(False)

    def rename_set_dialog(self):
        sid = self.current_set()
        if sid is None:
            return
        old = self.cmb_set.currentText().rsplit(" (", 1)[0]
        name, ok = QInputDialog.getText(self, tr("Zmień nazwę zestawu"), tr("Nowa nazwa:"), text=old)
        if ok and name.strip():
            self._modify_sets(lambda lib: lib.update_set(sid, name=name.strip()), sid)

    def delete_set(self):
        sid = self.current_set()
        if sid is None:
            return
        name = self.cmb_set.currentText()
        if QMessageBox.question(self, "QMapAnalyst", tr("Usunąć zestaw {name}? Punkty zostaną w bibliotece.").format(name=name)) \
                != QMessageBox.StandardButton.Yes:
            return
        self._modify_sets(lambda lib: lib.delete_set(sid), None)
        self.cmb_set.setCurrentIndex(0)

    def _modify_sets(self, action, select):
        try:
            with self.library() as lib:
                action(lib)
        except lb.LibraryError as e:
            self.status(self._library_error(e), "error")
            return
        self.reload_sets(select=select)

    def cleanup(self):
        pairs = [(self.project.layersWillBeRemoved, self._on_layers_removed)]
        if self.layer is not None:
            pairs.append((self.layer.selectionChanged, self._on_selection_changed))
            pairs.append((self.layer.editingStopped, self.apply_filter))
            pairs.append((self.layer.afterCommitChanges, self.invalidate_cache))
        for sig, slot in pairs:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
