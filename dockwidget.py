"""
Panel główny wtyczki QMapAnalyst.
"""

import functools
import inspect
import math
import os
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property

import numpy as np

from qgis.PyQt.QtCore import Qt, QCoreApplication, QTimer, QSize
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSlider,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QToolButton,
    QVBoxLayout, QWidget, QLineEdit,
)
from qgis.core import (
    Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
    QgsMessageLog, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsUnitTypes,
)
from qgis.gui import (
    QgsCollapsibleGroupBox, QgsFileWidget, QgsMapCanvas, QgsMapToolEmitPoint, QgsMapToolPan, QgsMapToolZoom, QgsVertexMarker,
)

from . import export as ex
from . import gcp_io
from . import math_engine as me
from . import projections as pj
from . import report
from .layers import (
    LayerRegistry, add_features, memory_layer, merge_segments, polyline, set_labels, split_lines,
    style_arrows, style_line, style_outline, style_points,
)

LOG_TAG = "QMapAnalyst"

COL_ACT, COL_OX, COL_OY, COL_RX, COL_RY, COL_ERR, COL_LOO, COL_W = range(8)

C_OLD = QColor(220, 40, 40)
C_REF = QColor(0, 160, 60)
C_OFF = QColor(150, 150, 150)
C_SEL = QColor(255, 190, 0)


# ---------------------------------------------------------------------------
#  Pomocnicze
# ---------------------------------------------------------------------------

def guarded(func):
    """Chroni slot przed niezłapanymi wyjątkami (stabilność QGIS) i przycina argumenty sygnałów."""
    sig = inspect.signature(func)
    pos = [p for p in sig.parameters.values()
           if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    n_args = len(pos) - 1
    has_var = any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if not has_var:
            args = args[:n_args]
        try:
            return func(self, *args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self.report_exception(func.__name__, e)
            return None
    return wrapper


@dataclass
class GCP:
    old_x: float
    old_y: float
    ref_x: float
    ref_y: float
    enabled: bool = True
    old_marker: object = None
    ref_marker: object = None


class AnalysisState:
    """Wynik analizy wraz z leniwie liczonymi pochodnymi (TPS, siatki zniekształceń)."""

    def __init__(self, fit, active_idx, ref_crs, labels, m_per_unit, unit):
        self.fit = fit
        self.active_idx = list(active_idx)
        self.ref_crs = QgsCoordinateReferenceSystem(ref_crs)
        self.labels = labels
        self.m_per_unit = m_per_unit
        self.unit = unit
        self.comparison = []
        self.moran = None
        self.distortion = None
        self._grids = {}

    @cached_property
    def warp(self):
        return me.Warp(self.fit)

    @cached_property
    def inverse_fit(self):
        return me.fit_transform(self.fit.model, self.fit.dst, self.fit.src)

    @cached_property
    def inverse_warp(self):
        return me.Warp.build(self.fit.model, self.fit.dst, self.fit.src)

    def grid(self, n=45):
        if n not in self._grids:
            self._grids[n] = me.local_distortion_grid(self.warp, n, n)
        return self._grids[n]

    @property
    def scale(self):
        return self.fit.derived["scale"]


def _icon(name):
    ic = QgsApplication.getThemeIcon(name)
    return ic if not ic.isNull() else QIcon()


def _tool_button(icon_name, text, tip, checkable=False, fallback=None):
    b = QToolButton()
    ic = _icon(icon_name) if icon_name else QIcon()
    if ic.isNull():
        b.setText(fallback or text)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    else:
        b.setIcon(ic)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setAutoRaise(True)
    b.setIconSize(QSize(20, 20))
    return b


def _button(text, icon_name=None, tip=None):
    b = QPushButton(text)
    if icon_name:
        ic = _icon(icon_name)
        if not ic.isNull():
            b.setIcon(ic)
    if tip:
        b.setToolTip(tip)
    return b


def _num_item(value, digits=2, editable=False, color=None, tip=None):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        it = QTableWidgetItem("–")
    else:
        it = QTableWidgetItem(f"{value:.{digits}f}")
    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    if editable:
        flags |= Qt.ItemFlag.ItemIsEditable
    it.setFlags(flags)
    if color is not None:
        it.setForeground(color)
    if tip:
        it.setToolTip(tip)
    return it


# ---------------------------------------------------------------------------
#  Panel
# ---------------------------------------------------------------------------

class QMapAnalystDockWidget(QDockWidget):

    def __init__(self, iface, parent=None):
        super().__init__("QMapAnalyst", parent)
        self.iface = iface
        self.project = QgsProject.instance()
        self.main_canvas = iface.mapCanvas()
        self.setObjectName("QMapAnalystDockWidget")

        self.old_raster_layer = None
        self.gcps = []
        self.ref_crs = None
        self.analysis = None
        self.analysis_stale = False
        self.projection = None
        self.projection_stale = False

        self._temp_old_point = None
        self._temp_marker = None
        self._prev_main_tool = None
        self._restoring_tool = False
        self._sync_lock = False
        self._pred_marker = None

        self.registry = LayerRegistry(self)
        self.registry.oldLayersChanged.connect(self.rebuild_overlay_list)

        self._recalc_timer = QTimer(self)
        self._recalc_timer.setSingleShot(True)
        self._recalc_timer.setInterval(250)
        self._recalc_timer.timeout.connect(lambda: self.run_analysis(auto=True))

        self.setup_ui()
        self.setup_map_tools()
        self.update_enabled_state()

    def tr(self, message):
        return QCoreApplication.translate("QMapAnalystDockWidget", message)

    # ------------------------------------------------------------------
    #  Statusy i obsługa błędów
    # ------------------------------------------------------------------
    def set_status(self, text, level="info"):
        colors = {"info": "palette(text)", "ok": "#1a7f37", "warn": "#9a6700", "error": "#cf222e"}
        self.lbl_status.setStyleSheet(f"color: {colors.get(level, colors['info'])};")
        self.lbl_status.setText(text)
        self.lbl_status.setToolTip(text)

    def report_exception(self, where, exc):
        QgsMessageLog.logMessage(f"{where}: {traceback.format_exc()}", LOG_TAG, Qgis.MessageLevel.Critical)
        self.set_status(self.tr("Błąd: {err}").format(err=exc), "error")
        self.progress.hide()

    @contextmanager
    def busy(self, message):
        self.set_status(message)
        self.lbl_status.repaint()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            yield
        finally:
            QApplication.restoreOverrideCursor()

    # ------------------------------------------------------------------
    #  Interfejs
    # ------------------------------------------------------------------
    def setup_ui(self):
        main = QWidget()
        root = QVBoxLayout(main)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_canvas_panel())

        self.tab_widget = QTabWidget()
        self.tab_widget.setDocumentMode(True)
        self.tab_widget.addTab(self._build_gcp_tab(), _icon("/mActionCapturePoint.svg"), self.tr("GCP"))
        self.tab_widget.addTab(self._build_analysis_tab(), _icon("/mActionCalculateField.svg"), self.tr("Analiza"))
        self.tab_widget.addTab(self._build_projection_tab(), _icon("/mIconGlobe.svg"), self.tr("Odwzorowanie"))
        self.tab_widget.addTab(self._build_layers_tab(), _icon("/mActionAddLayer.svg"), self.tr("Warstwy"))
        self.tab_widget.addTab(self._build_export_tab(), _icon("/mActionFileSave.svg"), self.tr("Eksport"))
        splitter.addWidget(self.tab_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        root.addWidget(splitter, 1)

        status = QHBoxLayout()
        self.lbl_status = QLabel()
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(120)
        self.progress.setMaximumHeight(14)
        self.progress.setTextVisible(False)
        self.progress.hide()
        status.addWidget(self.lbl_status, 1)
        status.addWidget(self.progress)
        root.addLayout(status)

        self.setWidget(main)
        self.set_status(self.tr("Gotowy. Wczytaj skan mapy dawnej."))

    # --- mapa dawna -----------------------------------------------------
    def _build_canvas_panel(self):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        bar = QHBoxLayout()
        bar.setSpacing(1)
        self.btn_load_raster = _tool_button("/mActionAddRasterLayer.svg", self.tr("Wczytaj skan"),
                                            self.tr("Wczytaj skan mapy dawnej (piksele)…"))
        self.btn_load_raster.clicked.connect(self.load_old_raster)
        self.btn_pan_old = _tool_button("/mActionPan.svg", self.tr("Przesuwaj"), self.tr("Przesuwanie mapy dawnej"), True)
        self.btn_zoom_in = _tool_button("/mActionZoomIn.svg", "+", self.tr("Powiększanie"), True)
        self.btn_zoom_out = _tool_button("/mActionZoomOut.svg", "−", self.tr("Pomniejszanie"), True)
        self.btn_zoom_full = _tool_button("/mActionZoomFullExtent.svg", self.tr("Pełny widok"), self.tr("Pokaż cały skan"))
        self.btn_zoom_full.clicked.connect(self.zoom_full_old)
        self.btn_capture = _tool_button("/mActionCapturePoint.svg", self.tr("Dodaj GCP"),
                                        self.tr("Dodawanie par GCP: kliknij punkt na mapie dawnej, a następnie odpowiadający mu punkt na mapie głównej. "
                                                "Tryb pozostaje aktywny do ponownego kliknięcia."), True, "➕ GCP")
        self.btn_capture.toggled.connect(self.toggle_capture)
        self.btn_sync_views = _tool_button("/mActionLink.svg", self.tr("Synchronizuj"),
                                           self.tr("Synchronizuj widoki mapy dawnej i głównej w obie strony (wymaga obliczonej transformacji). Podczas dodawania GCP mapa główna centruje się na przewidywanym położeniu punktu."),
                                           True, "🔗")
        self.btn_clear_overlays = _tool_button("/mActionDeleteSelected.svg", self.tr("Wyczyść"),
                                               self.tr("Usuń wszystkie nakładki z mapy dawnej"), False, "🧹")
        self.btn_clear_overlays.clicked.connect(self.clear_old_overlays)

        for w in (self.btn_load_raster, None, self.btn_pan_old, self.btn_zoom_in, self.btn_zoom_out,
                  self.btn_zoom_full, None, self.btn_capture, self.btn_sync_views):
            if w is None:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setFrameShadow(QFrame.Shadow.Sunken)
                bar.addWidget(sep)
            else:
                bar.addWidget(w)
        bar.addStretch()
        self.lbl_coords = QLabel()
        self.lbl_coords.setStyleSheet("color: palette(placeholder-text);")
        bar.addWidget(self.lbl_coords)
        bar.addWidget(self.btn_clear_overlays)
        lay.addLayout(bar)

        self.old_canvas = QgsMapCanvas()
        self.old_canvas.setCanvasColor(QColor(250, 250, 248))
        self.old_canvas.setMinimumHeight(220)
        self.old_canvas.enableAntiAliasing(True)
        self.old_canvas.setParallelRenderingEnabled(True)
        self.old_canvas.setCachingEnabled(True)
        lay.addWidget(self.old_canvas, 1)

        self.lst_old_layers = QListWidget()
        self.lst_old_layers.setMaximumHeight(68)
        self.lst_old_layers.setToolTip(self.tr("Widoczność nakładek na mapie dawnej"))
        self.lst_old_layers.itemChanged.connect(self.refresh_old_canvas_layers)
        self.lst_old_layers.hide()
        lay.addWidget(self.lst_old_layers)
        return box

    # --- GCP ------------------------------------------------------------
    def _build_gcp_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        row = QHBoxLayout()
        self.btn_add_gcp = _button(self.tr("Dodaj pary"), "/mActionCapturePoint.svg",
                                   self.tr("Tryb dodawania par GCP (to samo co przycisk na pasku mapy dawnej)"))
        self.btn_add_gcp.setCheckable(True)
        self.btn_add_gcp.toggled.connect(self.btn_capture.setChecked)
        self.btn_capture.toggled.connect(self.btn_add_gcp.setChecked)
        self.btn_remove_gcp = _button(self.tr("Usuń"), "/mActionDeleteSelected.svg", self.tr("Usuń zaznaczone pary"))
        self.btn_remove_gcp.clicked.connect(self.remove_selected_gcp)
        self.btn_toggle_gcp = _button(self.tr("Wł./wył."), None, self.tr("Przełącz aktywność zaznaczonych punktów"))
        self.btn_toggle_gcp.clicked.connect(self.toggle_selected_gcp)
        self.btn_load_gcp = _button("", "/mActionFileOpen.svg", self.tr("Wczytaj GCP (CSV / .points)…"))
        self.btn_load_gcp.clicked.connect(self.load_gcp_from_file)
        self.btn_save_gcp = _button("", "/mActionFileSave.svg", self.tr("Zapisz GCP (CSV / .points)…"))
        self.btn_save_gcp.clicked.connect(self.save_gcp_to_file)
        for b in (self.btn_add_gcp, self.btn_remove_gcp, self.btn_toggle_gcp):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(self.btn_load_gcp)
        row.addWidget(self.btn_save_gcp)
        lay.addLayout(row)

        self.tbl_gcp = QTableWidget(0, 8)
        headers = [
            (self.tr("Akt."), self.tr("Punkt aktywny w obliczeniach")),
            (self.tr("X dawna"), self.tr("Współrzędna X na skanie [px]")),
            (self.tr("Y dawna"), self.tr("Współrzędna Y na skanie [px]")),
            (self.tr("X ref."), self.tr("Współrzędna X w układzie odniesienia")),
            (self.tr("Y ref."), self.tr("Współrzędna Y w układzie odniesienia")),
            (self.tr("Błąd"), self.tr("Odległość punktu transformowanego od referencyjnego")),
            (self.tr("LOO"), self.tr("Błąd walidacji krzyżowej: błąd punktu, gdy transformację wyznaczono bez niego")),
            ("w", self.tr("Reszta standaryzowana (test Baardy). |w| > 3.29 – punkt podejrzany o błąd grubej identyfikacji")),
        ]
        for i, (txt, tip) in enumerate(headers):
            it = QTableWidgetItem(txt)
            it.setToolTip(tip)
            self.tbl_gcp.setHorizontalHeaderItem(i, it)
        hh = self.tbl_gcp.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_ACT, QHeaderView.ResizeMode.ResizeToContents)
        hh.setStretchLastSection(True)
        for c, w in ((COL_OX, 66), (COL_OY, 66), (COL_RX, 82), (COL_RY, 82), (COL_ERR, 58), (COL_LOO, 58)):
            self.tbl_gcp.setColumnWidth(c, w)
        self.tbl_gcp.verticalHeader().setDefaultSectionSize(22)
        self.tbl_gcp.setAlternatingRowColors(True)
        self.tbl_gcp.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_gcp.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                     QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tbl_gcp.itemChanged.connect(self.on_table_item_changed)
        self.tbl_gcp.itemSelectionChanged.connect(self.on_table_selection_changed)
        self.tbl_gcp.verticalHeader().sectionDoubleClicked.connect(self.zoom_to_gcp)
        lay.addWidget(self.tbl_gcp)

        hint = QLabel(self.tr("Dwuklik w numer wiersza – pokaż punkt na obu mapach. Dwuklik w komórkę – edycja współrzędnej."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholder-text); font-size: 8pt;")
        lay.addWidget(hint)
        return tab

    # --- analiza --------------------------------------------------------
    def _build_analysis_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        form = QFormLayout()
        self.cmb_transform_type = QComboBox()
        for key, meta in me.MODELS.items():
            self.cmb_transform_type.addItem(self.tr(meta["name"]), key)
        self.cmb_transform_type.setToolTip(self.tr(
            "Helmert – skala, obrót, przesunięcie (zachowuje kształty).\n"
            "Afiniczna – dodatkowo różne skale osi i skos (np. skurcz papieru).\n"
            "Wielomianowa 2. stopnia – łagodne nieliniowe zniekształcenia (min. 6 punktów)."))
        self.cmb_transform_type.currentIndexChanged.connect(self.on_settings_changed)
        form.addRow(self.tr("Transformacja:"), self.cmb_transform_type)

        self.spin_dpi = QSpinBox()
        self.spin_dpi.setRange(50, 4800)
        self.spin_dpi.setSingleStep(50)
        self.spin_dpi.setValue(300)
        self.spin_dpi.setSuffix(" DPI")
        self.spin_dpi.setToolTip(self.tr("Rozdzielczość skanu – służy do przeliczenia skali pikselowej na skalę mapy papierowej"))
        self.spin_dpi.valueChanged.connect(self.on_dpi_changed)
        form.addRow(self.tr("Rozdzielczość skanu:"), self.spin_dpi)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.btn_calculate = _button(self.tr("Oblicz transformację"), "/mActionCalculateField.svg")
        self.btn_calculate.clicked.connect(self.run_analysis)
        self.chk_auto = QCheckBox(self.tr("Przeliczaj automatycznie"))
        self.chk_auto.setChecked(True)
        self.chk_auto.setToolTip(self.tr("Po zmianie punktów GCP automatycznie przelicz analizę i odśwież istniejące warstwy wynikowe"))
        row.addWidget(self.btn_calculate, 1)
        row.addWidget(self.chk_auto)
        lay.addLayout(row)

        self.txt_report = QTextBrowser()
        self.txt_report.setOpenExternalLinks(True)
        self.txt_report.setPlaceholderText(self.tr("Wyniki analizy pojawią się tutaj."))
        lay.addWidget(self.txt_report, 1)
        return tab

    # --- odwzorowanie ---------------------------------------------------
    def _build_projection_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        info = QLabel(self.tr("Dopasowanie historycznych odwzorowań kartograficznych do punktów GCP. "
                              "Wymaga ≥ 5 punktów rozłożonych na możliwie dużym obszarze (najlepiej > 2°)."))
        info.setWordWrap(True)
        info.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(info)

        row = QHBoxLayout()
        self.cmb_proj_mode = QComboBox()
        self.cmb_proj_mode.addItem(self.tr("Podobieństwo (Helmert)"), "helmert")
        self.cmb_proj_mode.addItem(self.tr("Afiniczne (skurcz papieru)"), "affine")
        self.cmb_proj_mode.setToolTip(self.tr("Sposób dopasowania siatki odwzorowania do skanu. Wariant afiniczny pochłania "
                                              "nierównomierny skurcz papieru, ale zmniejsza zdolność rozróżniania odwzorowań."))
        self.btn_estimate_proj = _button(self.tr("Szacuj odwzorowanie"), "/mIconGlobe.svg")
        self.btn_estimate_proj.clicked.connect(self.estimate_projection)
        row.addWidget(self.cmb_proj_mode)
        row.addWidget(self.btn_estimate_proj, 1)
        lay.addLayout(row)

        split = QSplitter(Qt.Orientation.Vertical)
        self.tbl_proj = QTableWidget(0, 5)
        self.tbl_proj.setHorizontalHeaderLabels([
            self.tr("Odwzorowanie"), "σ₀ [px]", "ΔAICc", self.tr("Waga"), self.tr("Skala"),
        ])
        self.tbl_proj.horizontalHeaderItem(4).setToolTip(self.tr("Skala mapy w środku obszaru punktów"))
        hh = self.tbl_proj.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_proj.verticalHeader().setDefaultSectionSize(22)
        self.tbl_proj.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_proj.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl_proj.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_proj.itemSelectionChanged.connect(self.on_projection_selected)
        split.addWidget(self.tbl_proj)
        self.txt_proj = QTextBrowser()
        split.addWidget(self.txt_proj)
        lay.addWidget(split, 1)
        lay.addWidget(self._build_hypothesis_box())

        row = QHBoxLayout()
        self.cmb_grat_step = QComboBox()
        self.cmb_grat_step.addItem(self.tr("auto"), 0.0)
        for label, val in (("1′", 1 / 60), ("2′", 2 / 60), ("3′", 3 / 60), ("5′", 5 / 60), ("6′", 6 / 60), ("10′", 10 / 60), ("15′", 0.25), ("20′", 1 / 3), ("30′", 0.5),
                           ("1°", 1.0), ("2°", 2.0), ("5°", 5.0), ("10°", 10.0)):
            self.cmb_grat_step.addItem(label, val)
        self.btn_graticule = _button(self.tr("Siatka geograficzna na mapie dawnej"), "/mActionAddLayer.svg",
                                     self.tr("Rysuje siatkę południków i równoleżników wybranego odwzorowania na skanie – "
                                             "porównaj ją z siatką narysowaną na mapie."))
        self.btn_graticule.clicked.connect(self.generate_graticule)
        row.addWidget(QLabel(self.tr("Oczko:")))
        row.addWidget(self.cmb_grat_step)
        row.addWidget(self.btn_graticule, 1)
        lay.addLayout(row)
        return tab

    def _build_hypothesis_box(self):
        box = QgsCollapsibleGroupBox(self.tr("Test hipotezy – znane parametry odwzorowania"))
        box.setCollapsed(True)
        box.setToolTip(self.tr("Ocena odwzorowania o parametrach znanych z literatury lub z opisu mapy (bez ich dopasowywania). "
                               "Przy pojedynczym arkuszu to pewniejszy test niż ranking: porównaj siatkę hipotezy z podziałką ramki mapy."))
        form = QFormLayout(box)

        self.cmb_hyp_preset = QComboBox()
        self.cmb_hyp_preset.addItem(self.tr("— własne parametry —"), None)
        for pr in pj.PRESETS:
            self.cmb_hyp_preset.addItem(self.tr(pr["name"]), pr["id"])
            self.cmb_hyp_preset.setItemData(self.cmb_hyp_preset.count() - 1, self.tr(pr["source"]),
                                            Qt.ItemDataRole.ToolTipRole)
        self.cmb_hyp_preset.currentIndexChanged.connect(self.apply_hypothesis_preset)
        form.addRow(self.tr("Mapa:"), self.cmb_hyp_preset)

        self.cmb_hyp_proj = QComboBox()
        for proj in pj.PROJECTIONS:
            if proj.free:
                self.cmb_hyp_proj.addItem(self.tr(proj.name), proj.key)
        self.cmb_hyp_proj.currentIndexChanged.connect(self.update_hypothesis_fields)
        form.addRow(self.tr("Odwzorowanie:"), self.cmb_hyp_proj)

        self.hyp_edits = {}
        self.hyp_labels = {}
        hint = self.tr("np. 21°02′30″ E, 21 2 30 lub 21.0417")
        for key in ("lon0", "lat0", "lat1", "lat2"):
            ed = QLineEdit()
            ed.setPlaceholderText(hint if key == "lon0" else self.tr("np. 52° N lub 52.0"))
            ed.returnPressed.connect(self.run_hypothesis)
            lab = QLabel(self.tr(pj.PARAM_LABELS[key]) + ":")
            if key == "lon0":
                roww = QWidget()
                h = QHBoxLayout(roww)
                h.setContentsMargins(0, 0, 0, 0)
                self.cmb_hyp_meridian = QComboBox()
                self.cmb_hyp_meridian.addItem("Greenwich", "Greenwich")
                for name, _ in pj.PRIME_MERIDIANS:
                    self.cmb_hyp_meridian.addItem(self.tr(name), name)
                self.cmb_hyp_meridian.setToolTip(self.tr("Południk zerowy, względem którego podano λ₀"))
                h.addWidget(ed, 1)
                h.addWidget(QLabel(self.tr("od")))
                h.addWidget(self.cmb_hyp_meridian)
                form.addRow(lab, roww)
            else:
                form.addRow(lab, ed)
            self.hyp_edits[key] = ed
            self.hyp_labels[key] = lab

        self.lbl_hyp_source = QLabel()
        self.lbl_hyp_source.setWordWrap(True)
        self.lbl_hyp_source.setStyleSheet("color: palette(placeholder-text); font-size: 8pt;")
        form.addRow(self.lbl_hyp_source)
        self.btn_hypothesis = _button(self.tr("Testuj hipotezę i rysuj siatkę"), "/mIconGlobe.svg")
        self.btn_hypothesis.clicked.connect(self.run_hypothesis)
        form.addRow(self.btn_hypothesis)
        self.update_hypothesis_fields()
        return box

    def update_hypothesis_fields(self, *args):
        key = self.cmb_hyp_proj.currentData()
        used = pj.hypothesis_params(key) if key else ()
        for k, ed in self.hyp_edits.items():
            on = k in used
            ed.setEnabled(on)
            self.hyp_labels[k].setEnabled(on)
        self.cmb_hyp_meridian.setEnabled("lon0" in used)

    def apply_hypothesis_preset(self, *args):
        pid = self.cmb_hyp_preset.currentData()
        pr = next((p for p in pj.PRESETS if p["id"] == pid), None)
        if pr is None:
            self.lbl_hyp_source.clear()
            return
        i = self.cmb_hyp_proj.findData(pr["key"])
        if i >= 0:
            self.cmb_hyp_proj.setCurrentIndex(i)
        for k, ed in self.hyp_edits.items():
            ed.setText(pr["params"].get(k, ""))
        j = self.cmb_hyp_meridian.findData(pr["meridian"])
        if j >= 0:
            self.cmb_hyp_meridian.setCurrentIndex(j)
        self.lbl_hyp_source.setText(self.tr("Źródło parametrów: {src} Zweryfikuj z opisem konkretnego arkusza.").format(
            src=self.tr(pr["source"])))

    def hypothesis_values(self):
        key = self.cmb_hyp_proj.currentData()
        params = {}
        for k in pj.hypothesis_params(key):
            text = self.hyp_edits[k].text().strip()
            if not text:
                raise ValueError(self.tr("Podaj wartość parametru: {p}").format(p=self.tr(pj.PARAM_LABELS[k])))
            try:
                v = pj.parse_angle(text)
            except ValueError:
                raise ValueError(self.tr("Niepoprawny zapis kąta „{t}” ({p}).").format(t=text, p=self.tr(pj.PARAM_LABELS[k])))
            if k == "lon0":
                v = (v + pj.meridian_offset(self.cmb_hyp_meridian.currentData()) + 180.0) % 360.0 - 180.0
            elif not -90.0 <= v <= 90.0:
                raise ValueError(self.tr("Szerokość poza zakresem ±90°: {p}").format(p=self.tr(pj.PARAM_LABELS[k])))
            params[k] = v
        return key, params

    @guarded
    def run_hypothesis(self):
        try:
            key, params = self.hypothesis_values()
        except ValueError as e:
            self.set_status(str(e), "warn")
            return
        if self.projection is None or self.projection_stale:
            self.estimate_projection()
            if self.projection is None:
                return
        cand = pj.add_hypothesis(self.projection, key, params)
        self.fill_projection_table()
        self.tbl_proj.selectRow(self.projection.candidates.index(cand))
        self.on_projection_selected()
        self.render_report()
        if self.old_raster_layer is not None:
            self.generate_graticule()
        free = self.projection.free_fit(key)
        msg = self.tr("Hipoteza {name}: σ₀ = {s:.2f} px, ΔAICc = {d:.1f}, miejsce w rankingu: {r}.").format(
            name=self.tr(cand.name), s=cand.sigma0_px, d=cand.delta_aicc,
            r=self.projection.candidates.index(cand) + 1)
        if free is not None and np.isfinite(free.sigma0_px):
            msg += " " + self.tr("Swobodne dopasowanie: σ₀ = {s:.2f} px.").format(s=free.sigma0_px)
        self.set_status(msg, "ok" if cand.delta_aicc < 2 else "warn")

    # --- warstwy --------------------------------------------------------
    def _build_layers_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)

        conf = QGroupBox(self.tr("Parametry"))
        form = QFormLayout(conf)
        srow = QHBoxLayout()
        self.sld_confidence = QSlider(Qt.Orientation.Horizontal)
        self.sld_confidence.setRange(100, 25000)
        self.sld_confidence.setSingleStep(250)
        self.sld_confidence.setValue(3000)
        self.sld_confidence.valueChanged.connect(self.on_slider_changed)
        self.lbl_slider_val = QLabel("3000 m")
        self.lbl_slider_val.setMinimumWidth(70)
        srow.addWidget(self.sld_confidence)
        srow.addWidget(self.lbl_slider_val)
        form.addRow(self.tr("Promień pewności:"), srow)
        self.sld_confidence.setToolTip(self.tr("Obszar w tej odległości od punktów GCP uznawany jest za wiarygodny; "
                                               "poza nim siatki i izolinie są ekstrapolacją"))

        self.spin_grid_step = QSpinBox()
        self.spin_grid_step.setRange(1, 1000000)
        self.spin_grid_step.setSingleStep(100)
        self.spin_grid_step.setValue(1000)
        self.spin_grid_step.setSuffix(" m")
        form.addRow(self.tr("Oczko siatek:"), self.spin_grid_step)

        self.spin_magnification = QDoubleSpinBox()
        self.spin_magnification.setRange(0.1, 10000)
        self.spin_magnification.setDecimals(1)
        self.spin_magnification.setValue(1.0)
        self.spin_magnification.setPrefix("× ")
        self.spin_magnification.setToolTip(self.tr("Powiększenie wektorów przesunięć i okręgów błędów"))
        form.addRow(self.tr("Powiększenie wektorów:"), self.spin_magnification)

        self.spin_iso_density = QSpinBox()
        self.spin_iso_density.setRange(4, 30)
        self.spin_iso_density.setValue(8)
        self.spin_iso_density.setToolTip(self.tr("Przybliżona liczba poziomów izolinii"))
        form.addRow(self.tr("Liczba izolinii:"), self.spin_iso_density)
        lay.addWidget(conf)

        main_grp = QGroupBox(self.tr("Mapa główna (układ odniesienia)"))
        g = QGridLayout(main_grp)
        self.btn_gen_points = _button(self.tr("Punkty z resztami"), None, self.tr("Warstwa punktów GCP ze wszystkimi statystykami w atrybutach"))
        self.btn_gen_points.clicked.connect(self.generate_gcp_points)
        self.btn_gen_vectors = _button(self.tr("Wektory przesunięć"))
        self.btn_gen_vectors.clicked.connect(self.generate_result_layers)
        self.btn_gen_circles = _button(self.tr("Okręgi błędów"))
        self.btn_gen_circles.clicked.connect(self.generate_error_circles)
        self.btn_gen_grid = _button(self.tr("Siatka zniekształceń"))
        self.btn_gen_grid.clicked.connect(self.generate_distortion_grid)
        self.btn_clear_main = _button(self.tr("Usuń warstwy"), "/mActionRemoveLayer.svg",
                                      self.tr("Usuń z projektu warstwy wynikowe mapy głównej"))
        self.btn_clear_main.clicked.connect(self.clear_main_layers)
        g.addWidget(self.btn_gen_points, 0, 0)
        g.addWidget(self.btn_gen_vectors, 0, 1)
        g.addWidget(self.btn_gen_circles, 1, 0)
        g.addWidget(self.btn_gen_grid, 1, 1)
        g.addWidget(self.btn_clear_main, 2, 0, 1, 2)
        lay.addWidget(main_grp)

        old_grp = QGroupBox(self.tr("Mapa dawna (podgląd powyżej)"))
        g = QGridLayout(old_grp)
        self.btn_gen_old_vectors = _button(self.tr("Wektory przesunięć"))
        self.btn_gen_old_vectors.clicked.connect(self.generate_old_vectors)
        self.btn_gen_old_circles = _button(self.tr("Okręgi błędów"))
        self.btn_gen_old_circles.clicked.connect(self.generate_old_circles)
        self.btn_gen_historic_grid = _button(self.tr("Siatka układu odniesienia"))
        self.btn_gen_historic_grid.clicked.connect(self.generate_historic_grid)
        self.btn_gen_iso_scale = _button(self.tr("Izolinie skali"))
        self.btn_gen_iso_scale.clicked.connect(lambda: self.generate_isolines("scale"))
        self.btn_gen_iso_rot = _button(self.tr("Izolinie rotacji"))
        self.btn_gen_iso_rot.clicked.connect(lambda: self.generate_isolines("rot"))
        self.btn_gen_iso_omega = _button(self.tr("Izolinie zniekształceń kątowych"))
        self.btn_gen_iso_omega.setToolTip(self.tr("Maksymalne zniekształcenie kąta 2ω z elipsy Tissota lokalnego odwzorowania"))
        self.btn_gen_iso_omega.clicked.connect(lambda: self.generate_isolines("omega"))
        g.addWidget(self.btn_gen_old_vectors, 0, 0)
        g.addWidget(self.btn_gen_old_circles, 0, 1)
        g.addWidget(self.btn_gen_historic_grid, 1, 0, 1, 2)
        g.addWidget(self.btn_gen_iso_scale, 2, 0)
        g.addWidget(self.btn_gen_iso_rot, 2, 1)
        g.addWidget(self.btn_gen_iso_omega, 3, 0, 1, 2)
        lay.addWidget(old_grp)
        lay.addStretch()

        self._result_buttons = [
            self.btn_gen_points, self.btn_gen_vectors, self.btn_gen_circles, self.btn_gen_grid,
            self.btn_gen_old_vectors, self.btn_gen_old_circles, self.btn_gen_historic_grid,
            self.btn_gen_iso_scale, self.btn_gen_iso_rot, self.btn_gen_iso_omega,
        ]
        return tab

    # --- eksport --------------------------------------------------------
    def _build_export_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        grp = QGroupBox(self.tr("Eksport wyników i raportów"))
        v = QVBoxLayout(grp)
        self.file_export_dir = QgsFileWidget()
        self.file_export_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.file_export_dir.setDialogTitle(self.tr("Wybierz folder zapisu eksportu"))
        v.addWidget(QLabel(self.tr("Folder docelowy:")))
        v.addWidget(self.file_export_dir)
        self.chk_exp_pdf = QCheckBox(self.tr("Raport PDF (statystyki, odwzorowanie, strony z nakładkami)"))
        self.chk_exp_pdf.setChecked(True)
        self.chk_exp_tiff = QCheckBox(self.tr("Widok mapy dawnej z nakładkami (TIFF)"))
        self.chk_exp_tiff.setChecked(True)
        self.chk_exp_csv = QCheckBox(self.tr("Tabela punktów z resztami (CSV)"))
        self.chk_exp_csv.setChecked(True)
        self.chk_exp_proj = QCheckBox(self.tr("Ranking odwzorowań (CSV)"))
        self.chk_exp_proj.setChecked(True)
        for w in (self.chk_exp_pdf, self.chk_exp_tiff, self.chk_exp_csv, self.chk_exp_proj):
            v.addWidget(w)
        self.btn_execute_export = _button(self.tr("Eksportuj"), "/mActionFileSave.svg")
        self.btn_execute_export.clicked.connect(self.execute_export)
        v.addWidget(self.btn_execute_export)
        lay.addWidget(grp)
        lay.addStretch()
        return tab

    # ------------------------------------------------------------------
    #  Narzędzia map
    # ------------------------------------------------------------------
    def setup_map_tools(self):
        # Uwaga: nie używamy QgsMapTool.setButton() – narzędzie trzyma wtedy surowy wskaźnik
        # do przycisku, a destruktor kanwy odwołuje się do niego po jego usunięciu (crash QGIS
        # przy wyłączaniu wtyczki). Stan przycisków synchronizujemy przez sygnał mapToolSet.
        self.tool_pan_old = QgsMapToolPan(self.old_canvas)
        self.tool_zoom_in = QgsMapToolZoom(self.old_canvas, False)
        self.tool_zoom_out = QgsMapToolZoom(self.old_canvas, True)
        self.old_canvas.mapToolSet.connect(self.sync_tool_buttons)
        self.btn_pan_old.clicked.connect(lambda: self.set_old_tool(self.tool_pan_old))
        self.btn_zoom_in.clicked.connect(lambda: self.set_old_tool(self.tool_zoom_in))
        self.btn_zoom_out.clicked.connect(lambda: self.set_old_tool(self.tool_zoom_out))

        self.tool_click_old = QgsMapToolEmitPoint(self.old_canvas)
        self.tool_click_old.canvasClicked.connect(self.on_old_canvas_clicked)
        self.tool_click_ref = QgsMapToolEmitPoint(self.main_canvas)
        self.tool_click_ref.canvasClicked.connect(self.on_ref_canvas_clicked)
        self.tool_click_ref.deactivated.connect(self.on_ref_tool_deactivated)

        self.old_canvas.setMapTool(self.tool_pan_old)
        self.old_canvas.extentsChanged.connect(self.sync_main_canvas_to_old)
        self.main_canvas.extentsChanged.connect(self.sync_old_canvas_to_main)
        self.btn_sync_views.toggled.connect(self.on_sync_toggled)
        self.old_canvas.xyCoordinates.connect(self.on_old_mouse_move)
        self.main_canvas.destinationCrsChanged.connect(self.update_ref_markers)

    def set_old_tool(self, tool):
        if self.btn_capture.isChecked():
            self.btn_capture.setChecked(False)
        self.old_canvas.setMapTool(tool)
        self.sync_tool_buttons()

    def sync_tool_buttons(self, *args):
        current = self.old_canvas.mapTool()
        for btn, tool in ((self.btn_pan_old, self.tool_pan_old), (self.btn_zoom_in, self.tool_zoom_in),
                          (self.btn_zoom_out, self.tool_zoom_out)):
            btn.setChecked(current is tool)

    def on_old_mouse_move(self, pt):
        self.lbl_coords.setText(f"{pt.x():.1f}, {pt.y():.1f} px")

    def update_enabled_state(self):
        has_raster = self.old_raster_layer is not None
        has_res = self.analysis is not None
        for b in self._result_buttons:
            b.setEnabled(has_res)
        for b in (self.btn_gen_old_vectors, self.btn_gen_old_circles, self.btn_gen_historic_grid,
                  self.btn_gen_iso_scale, self.btn_gen_iso_rot, self.btn_gen_iso_omega):
            b.setEnabled(has_res and has_raster)
        self.btn_sync_views.setEnabled(has_res)
        self.btn_graticule.setEnabled(self.projection is not None and has_raster)
        self.btn_save_gcp.setEnabled(bool(self.gcps))
        self.btn_remove_gcp.setEnabled(bool(self.gcps))
        self.btn_toggle_gcp.setEnabled(bool(self.gcps))

    # ------------------------------------------------------------------
    #  Układ odniesienia
    # ------------------------------------------------------------------
    def canvas_crs(self):
        return self.main_canvas.mapSettings().destinationCrs()

    def _ensure_ref_crs(self):
        if self.ref_crs is None or not self.ref_crs.isValid():
            self.ref_crs = QgsCoordinateReferenceSystem(self.canvas_crs())
        return self.ref_crs

    def _ref_to_canvas(self, x, y):
        crs = self.ref_crs
        dst = self.canvas_crs()
        pt = QgsPointXY(x, y)
        if crs is None or not crs.isValid() or crs == dst:
            return pt
        try:
            return QgsCoordinateTransform(crs, dst, self.project).transform(pt)
        except Exception:
            return pt

    def _canvas_to_ref(self, pt):
        crs = self._ensure_ref_crs()
        src = self.canvas_crs()
        if crs == src:
            return pt
        return QgsCoordinateTransform(src, crs, self.project).transform(pt)

    def ref_units(self):
        crs = self.ref_crs or self.canvas_crs()
        units = crs.mapUnits()
        abbrev = QgsUnitTypes.toAbbreviatedString(units) or ""
        try:
            factor = QgsUnitTypes.fromUnitToUnitFactor(units, Qgis.DistanceUnit.Meters)
        except Exception:
            factor = 1.0
        if crs.isGeographic():
            factor = float("nan")
        return factor, abbrev

    @staticmethod
    def crs_short(crs):
        if crs is None or not crs.isValid():
            return "?"
        return crs.authid() or crs.description() or (crs.toProj()[:60] if crs.toProj() else "?")

    def ref_crs_label(self):
        crs = self.ref_crs or self.canvas_crs()
        return f"{crs.authid()} {crs.description()}".strip() or self.tr("nieznany")

    # ------------------------------------------------------------------
    #  Mapa dawna i nakładki
    # ------------------------------------------------------------------
    @guarded
    def load_old_raster(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Wybierz skan mapy dawnej"), "",
            self.tr("Obrazy (*.tif *.tiff *.jpg *.jpeg *.png *.jp2 *.bmp);;Wszystkie pliki (*.*)"))
        if not path:
            return
        layer = QgsRasterLayer(path, os.path.splitext(os.path.basename(path))[0])
        if not layer.isValid():
            self.set_status(self.tr("Nie udało się wczytać pliku rastrowego."), "error")
            return
        self.set_old_raster(layer)

    def set_old_raster(self, layer):
        self.registry.clear_old()
        self.old_raster_layer = layer
        self.old_canvas.setDestinationCrs(layer.crs())
        self.refresh_old_canvas_layers()
        self.old_canvas.setExtent(layer.extent())
        self.old_canvas.refresh()
        self.update_enabled_state()
        self.set_status(self.tr("Skan wczytany: {w} × {h} px. Dodaj lub wczytaj punkty GCP.").format(
            w=layer.width(), h=layer.height()), "ok")

    def zoom_full_old(self):
        if self.old_raster_layer:
            self.old_canvas.setExtent(self.old_raster_layer.extent())
            self.old_canvas.refresh()

    def rebuild_overlay_list(self):
        checked = {}
        for i in range(self.lst_old_layers.count()):
            it = self.lst_old_layers.item(i)
            checked[it.data(Qt.ItemDataRole.UserRole)] = it.checkState()
        self.lst_old_layers.blockSignals(True)
        self.lst_old_layers.clear()
        for key, lyr in self.registry.old_layers():
            it = QListWidgetItem(lyr.name())
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(checked.get(lyr.id(), Qt.CheckState.Checked))
            it.setData(Qt.ItemDataRole.UserRole, lyr.id())
            self.lst_old_layers.addItem(it)
        self.lst_old_layers.blockSignals(False)
        self.lst_old_layers.setVisible(self.lst_old_layers.count() > 0)
        self.refresh_old_canvas_layers()

    def refresh_old_canvas_layers(self, *args):
        layers = []
        for i in range(self.lst_old_layers.count()):
            it = self.lst_old_layers.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                lyr = self.project.mapLayer(it.data(Qt.ItemDataRole.UserRole))
                if lyr is not None and lyr.isValid():
                    layers.append(lyr)
        layers.reverse()  # najnowsze na wierzchu
        if self.old_raster_layer is not None and self.old_raster_layer.isValid():
            layers.append(self.old_raster_layer)
        self.old_canvas.setLayers(layers)
        self.old_canvas.refresh()

    def old_overlay_layers(self, only_visible=True):
        out = []
        for i in range(self.lst_old_layers.count()):
            it = self.lst_old_layers.item(i)
            if only_visible and it.checkState() != Qt.CheckState.Checked:
                continue
            lyr = self.project.mapLayer(it.data(Qt.ItemDataRole.UserRole))
            if lyr is not None and lyr.isValid():
                out.append(lyr)
        return out

    def clear_old_overlays(self):
        self.registry.clear_old()
        self.set_status(self.tr("Usunięto nakładki z mapy dawnej."))

    def clear_main_layers(self):
        self.registry.clear_main()
        self.set_status(self.tr("Usunięto warstwy wynikowe z mapy głównej."))

    # ------------------------------------------------------------------
    #  Synchronizacja widoków (dwukierunkowa, środek + skala)
    # ------------------------------------------------------------------
    def _sync_active(self):
        return (self.btn_sync_views.isChecked() and self.analysis is not None
                and not self._sync_lock and self.analysis.scale > 0)

    def _set_extent_locked(self, canvas, rect):
        self._sync_lock = True
        try:
            canvas.setExtent(rect)
            canvas.refresh()
        finally:
            self._sync_lock = False

    def on_sync_toggled(self, checked):
        if checked:
            self.sync_main_canvas_to_old()

    def sync_main_canvas_to_old(self):
        """Mapa dawna -> mapa główna."""
        if not self._sync_active():
            return
        try:
            ext = self.old_canvas.extent()
            c = np.array([[ext.center().x(), ext.center().y()]])
            rc = self.analysis.fit.predict(c)[0]
            s = self.analysis.scale
            hw, hh = ext.width() * s / 2.0, ext.height() * s / 2.0
            pts = [self._ref_to_canvas(float(rc[0] + dx), float(rc[1] + dy))
                   for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
            xs = [p.x() for p in pts]
            ys = [p.y() for p in pts]
            self._set_extent_locked(self.main_canvas, QgsRectangle(min(xs), min(ys), max(xs), max(ys)))
        except Exception as e:  # noqa: BLE001
            QgsMessageLog.logMessage(f"sync old->main: {e}", LOG_TAG, Qgis.MessageLevel.Warning)

    def sync_old_canvas_to_main(self):
        """Mapa główna -> mapa dawna."""
        if not self._sync_active() or self.old_raster_layer is None:
            return
        try:
            ext = self.main_canvas.extent()
            corners = [QgsPointXY(ext.xMinimum(), ext.yMinimum()), QgsPointXY(ext.xMaximum(), ext.yMaximum())]
            src = self.canvas_crs()
            if self.ref_crs is not None and self.ref_crs.isValid() and src != self.ref_crs:
                ct = QgsCoordinateTransform(src, self.ref_crs, self.project)
                corners = [ct.transform(p) for p in corners]
            (x0, y0), (x1, y1) = [(p.x(), p.y()) for p in corners]
            center = np.array([[(x0 + x1) / 2.0, (y0 + y1) / 2.0]])
            oc = self.analysis.inverse_fit.predict(center)[0]
            s = self.analysis.scale
            hw, hh = abs(x1 - x0) / s / 2.0, abs(y1 - y0) / s / 2.0
            self._set_extent_locked(self.old_canvas, QgsRectangle(oc[0] - hw, oc[1] - hh, oc[0] + hw, oc[1] + hh))
        except Exception as e:  # noqa: BLE001
            QgsMessageLog.logMessage(f"sync main->old: {e}", LOG_TAG, Qgis.MessageLevel.Warning)

    def predicted_ref_point(self, x, y):
        """Przewidywane położenie punktu mapy dawnej w układzie odniesienia (TPS, gdy dostępny)."""
        st = self.analysis
        if st is None:
            return None
        pt = np.array([[x, y]])
        try:
            p = st.warp.predict(pt)[0] if st.warp.ok else st.fit.predict(pt)[0]
        except Exception:  # noqa: BLE001
            p = st.fit.predict(pt)[0]
        return float(p[0]), float(p[1])

    def _remove_pred_marker(self):
        if self._pred_marker is not None:
            try:
                self.main_canvas.scene().removeItem(self._pred_marker)
            except RuntimeError:
                pass
            self._pred_marker = None

    # ------------------------------------------------------------------
    #  Zbieranie punktów GCP
    # ------------------------------------------------------------------
    @guarded
    def toggle_capture(self, checked):
        if checked:
            if self.old_raster_layer is None:
                self.set_status(self.tr("Najpierw wczytaj skan mapy dawnej."), "warn")
                self.btn_capture.setChecked(False)
                return
            for b in (self.btn_pan_old, self.btn_zoom_in, self.btn_zoom_out):
                b.setChecked(False)
            self.old_canvas.setMapTool(self.tool_click_old)
            self.set_status(self.tr("KROK 1/2: kliknij punkt na MAPIE DAWNEJ (w panelu)…"))
        else:
            self._remove_temp_marker()
            self._remove_pred_marker()
            self._temp_old_point = None
            self._restore_main_tool()
            self.old_canvas.setMapTool(self.tool_pan_old)
            self.set_status(self.tr("Zakończono dodawanie punktów. Liczba par: {n}.").format(n=len(self.gcps)))

    def _remove_temp_marker(self):
        if self._temp_marker is not None:
            self.old_canvas.scene().removeItem(self._temp_marker)
            self._temp_marker = None

    def _restore_main_tool(self):
        if self.main_canvas.mapTool() is self.tool_click_ref:
            self._restoring_tool = True
            try:
                if self._prev_main_tool is not None and self._prev_main_tool is not self.tool_click_ref:
                    self.main_canvas.setMapTool(self._prev_main_tool)
                else:
                    self.main_canvas.unsetMapTool(self.tool_click_ref)
            finally:
                self._restoring_tool = False
        self._prev_main_tool = None

    def on_ref_tool_deactivated(self):
        # Użytkownik wybrał inne narzędzie mapy głównej podczas kroku 2 – anuluj bieżący punkt
        if self._restoring_tool or not self.btn_capture.isChecked() or self._temp_old_point is None:
            return
        self._prev_main_tool = None
        self._temp_old_point = None
        self._remove_temp_marker()
        self._remove_pred_marker()
        self.btn_capture.setChecked(False)
        self.set_status(self.tr("Przerwano dodawanie punktu (zmieniono narzędzie mapy głównej)."), "warn")

    @guarded
    def on_old_canvas_clicked(self, point, button):
        if button != Qt.MouseButton.LeftButton:
            return
        self._temp_old_point = QgsPointXY(point)
        self._remove_temp_marker()
        self._temp_marker = self.create_marker(self.old_canvas, point, C_SEL, size=14)
        if self.main_canvas.mapTool() is not self.tool_click_ref:
            self._prev_main_tool = self.main_canvas.mapTool()
            self.main_canvas.setMapTool(self.tool_click_ref)
        self._remove_pred_marker()
        pred = self.predicted_ref_point(point.x(), point.y())
        if pred is not None:
            cpt = self._ref_to_canvas(*pred)
            self._pred_marker = self.create_marker(self.main_canvas, cpt, C_SEL, size=22)
            self._pred_marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
            if self.btn_sync_views.isChecked():
                self._sync_lock = True
                try:
                    self.main_canvas.setCenter(cpt)
                    self.main_canvas.refresh()
                finally:
                    self._sync_lock = False
            self.set_status(self.tr("KROK 2/2: kliknij odpowiadający punkt na MAPIE GŁÓWNEJ QGIS (żółte kółko – położenie przewidywane z transformacji)…"))
        else:
            self.set_status(self.tr("KROK 2/2: kliknij odpowiadający punkt na MAPIE GŁÓWNEJ QGIS…"))

    @guarded
    def on_ref_canvas_clicked(self, point, button):
        if button != Qt.MouseButton.LeftButton or self._temp_old_point is None:
            return
        ref = self._canvas_to_ref(point)
        old = self._temp_old_point
        self._temp_old_point = None
        self._remove_temp_marker()
        self._remove_pred_marker()
        self.add_gcp(old.x(), old.y(), ref.x(), ref.y())
        self.gcps_changed()
        if self.btn_capture.isChecked():
            self.set_status(self.tr("Dodano parę #{n}. KROK 1/2: kliknij kolejny punkt na mapie dawnej (lub wyłącz tryb dodawania).")
                            .format(n=len(self.gcps)), "ok")
        else:
            self._restore_main_tool()

    # ------------------------------------------------------------------
    #  Model punktów GCP
    # ------------------------------------------------------------------
    def create_marker(self, canvas, point, color, size=11):
        m = QgsVertexMarker(canvas)
        m.setCenter(point)
        m.setColor(color)
        m.setIconSize(size)
        m.setIconType(QgsVertexMarker.IconType.ICON_CROSS)
        m.setPenWidth(2)
        return m

    def add_gcp(self, ox, oy, rx, ry, enabled=True):
        self._ensure_ref_crs()
        g = GCP(float(ox), float(oy), float(rx), float(ry), bool(enabled))
        g.old_marker = self.create_marker(self.old_canvas, QgsPointXY(g.old_x, g.old_y), C_OLD if enabled else C_OFF)
        g.ref_marker = self.create_marker(self.main_canvas, self._ref_to_canvas(g.ref_x, g.ref_y), C_REF if enabled else C_OFF)
        self.gcps.append(g)
        return g

    def _drop_markers(self, g):
        for canvas, m in ((self.old_canvas, g.old_marker), (self.main_canvas, g.ref_marker)):
            if m is not None:
                try:
                    canvas.scene().removeItem(m)
                except RuntimeError:
                    pass
        g.old_marker = g.ref_marker = None

    def clear_all_gcp(self):
        for g in self.gcps:
            self._drop_markers(g)
        self.gcps = []

    def update_markers(self):
        selected = self.selected_rows()
        for i, g in enumerate(self.gcps):
            sel = i in selected
            for m, col in ((g.old_marker, C_OLD), (g.ref_marker, C_REF)):
                if m is None:
                    continue
                m.setColor(C_SEL if sel else (col if g.enabled else C_OFF))
                m.setIconSize(18 if sel else 11)
                m.setPenWidth(3 if sel else 2)
                m.update()

    def update_ref_markers(self):
        for g in self.gcps:
            if g.ref_marker is not None:
                g.ref_marker.setCenter(self._ref_to_canvas(g.ref_x, g.ref_y))

    def selected_rows(self):
        return sorted({i.row() for i in self.tbl_gcp.selectedIndexes()})

    def gcps_changed(self):
        """Wywoływane po każdej zmianie zbioru punktów."""
        if self.analysis is not None:
            self.analysis_stale = True
        if self.projection is not None:
            self.projection_stale = True
            self.txt_proj.setHtml(f"<p style='color:#9a6700'>{self.tr('Punkty GCP zmieniły się – wynik estymacji jest nieaktualny. Uruchom ją ponownie.')}</p>")
        self.refresh_table()
        self.update_markers()
        self.update_enabled_state()
        if self.chk_auto.isChecked():
            model = self.cmb_transform_type.currentData()
            n_active = sum(1 for g in self.gcps if g.enabled)
            if n_active >= me.MODELS[model]["min_pts"] or self.analysis is not None:
                self._recalc_timer.start()

    def refresh_table(self):
        t = self.tbl_gcp
        t.blockSignals(True)
        t.setUpdatesEnabled(False)
        try:
            t.setRowCount(len(self.gcps))
            res = {}
            fit = None
            if self.analysis is not None and not self.analysis_stale:
                fit = self.analysis.fit
                res = {row: k for k, row in enumerate(self.analysis.active_idx)}
            unit = self.analysis.unit if self.analysis else ""
            for r, g in enumerate(self.gcps):
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                chk.setCheckState(Qt.CheckState.Checked if g.enabled else Qt.CheckState.Unchecked)
                t.setItem(r, COL_ACT, chk)
                t.setItem(r, COL_OX, _num_item(g.old_x, 2, True))
                t.setItem(r, COL_OY, _num_item(g.old_y, 2, True))
                t.setItem(r, COL_RX, _num_item(g.ref_x, 2, True))
                t.setItem(r, COL_RY, _num_item(g.ref_y, 2, True))
                k = res.get(r)
                if fit is not None and k is not None:
                    w = fit.std_residuals[k]
                    out = np.isfinite(w) and w > me.OUTLIER_THRESHOLD
                    is_max = k == fit.stats["max_index"]
                    col = QColor(207, 34, 46) if (out or is_max) else None
                    t.setItem(r, COL_ERR, _num_item(float(fit.errors[k]), 2, color=col,
                                                    tip=f"vX = {fit.residuals[k, 0]:.2f}, vY = {fit.residuals[k, 1]:.2f} {unit}"))
                    t.setItem(r, COL_LOO, _num_item(float(fit.loo_errors[k]), 2))
                    t.setItem(r, COL_W, _num_item(float(w), 2, color=QColor(207, 34, 46) if out else None))
                else:
                    for c in (COL_ERR, COL_LOO, COL_W):
                        it = _num_item(None)
                        if not g.enabled:
                            it.setText(self.tr("wył."))
                            it.setForeground(C_OFF)
                        t.setItem(r, c, it)
        finally:
            t.setUpdatesEnabled(True)
            t.blockSignals(False)

    @guarded
    def on_table_item_changed(self, item):
        r, c = item.row(), item.column()
        if r >= len(self.gcps):
            return
        g = self.gcps[r]
        if c == COL_ACT:
            g.enabled = item.checkState() == Qt.CheckState.Checked
        elif c in (COL_OX, COL_OY, COL_RX, COL_RY):
            attr = {COL_OX: "old_x", COL_OY: "old_y", COL_RX: "ref_x", COL_RY: "ref_y"}[c]
            try:
                val = float(item.text().replace(",", ".").replace(" ", "").replace(" ", ""))
                if not math.isfinite(val):
                    raise ValueError
            except ValueError:
                self.set_status(self.tr("Niepoprawna wartość liczbowa – przywrócono poprzednią."), "warn")
                self.refresh_table()
                return
            setattr(g, attr, val)
            g.old_marker.setCenter(QgsPointXY(g.old_x, g.old_y))
            g.ref_marker.setCenter(self._ref_to_canvas(g.ref_x, g.ref_y))
        else:
            return
        self.gcps_changed()

    def on_table_selection_changed(self):
        self.update_markers()

    @guarded
    def zoom_to_gcp(self, row):
        if not (0 <= row < len(self.gcps)):
            return
        g = self.gcps[row]
        self.old_canvas.setCenter(QgsPointXY(g.old_x, g.old_y))
        self.old_canvas.refresh()
        self.main_canvas.setCenter(self._ref_to_canvas(g.ref_x, g.ref_y))
        self.main_canvas.refresh()

    @guarded
    def remove_selected_gcp(self):
        rows = self.selected_rows()
        if not rows:
            self.set_status(self.tr("Zaznacz wiersze do usunięcia."), "warn")
            return
        for r in reversed(rows):
            self._drop_markers(self.gcps.pop(r))
        if not self.gcps:
            self.ref_crs = None
        self.tbl_gcp.clearSelection()
        self.gcps_changed()
        self.set_status(self.tr("Usunięto {n} punktów.").format(n=len(rows)))

    @guarded
    def toggle_selected_gcp(self):
        rows = self.selected_rows()
        if not rows:
            return
        state = not all(self.gcps[r].enabled for r in rows)
        for r in rows:
            self.gcps[r].enabled = state
        self.gcps_changed()

    def active_arrays(self):
        idx = [i for i, g in enumerate(self.gcps) if g.enabled]
        old = np.array([[self.gcps[i].old_x, self.gcps[i].old_y] for i in idx], dtype=float).reshape(-1, 2)
        ref = np.array([[self.gcps[i].ref_x, self.gcps[i].ref_y] for i in idx], dtype=float).reshape(-1, 2)
        return old, ref, idx

    # ------------------------------------------------------------------
    #  Zapis / odczyt GCP
    # ------------------------------------------------------------------
    @guarded
    def save_gcp_to_file(self):
        if not self.gcps:
            self.set_status(self.tr("Brak punktów do zapisania."), "warn")
            return
        path, flt = QFileDialog.getSaveFileName(
            self, self.tr("Zapisz punkty GCP"), "",
            self.tr("Punkty Georeferencera QGIS (*.points);;Plik CSV (*.csv)"))
        if not path:
            return
        is_points = path.lower().endswith(".points") or (".points" in flt and not path.lower().endswith(".csv"))
        if is_points and not path.lower().endswith(".points"):
            path += ".points"
        crs = self._ensure_ref_crs()
        rows = [(g.old_x, g.old_y, g.ref_x, g.ref_y, g.enabled) for g in self.gcps]
        if is_points:
            gcp_io.write_file(path, gcp_io.format_points(rows, self._crs_definition(crs)))
        else:
            gcp_io.write_file(path, gcp_io.format_csv(rows))
        self.set_status(self.tr("Zapisano {n} punktów do pliku.").format(n=len(self.gcps)), "ok")

    @staticmethod
    def _crs_definition(crs):
        """Definicja CRS jak w Georeferencerze QGIS: jednoliniowy WKT (wariant Preferred)."""
        wkt = ""
        try:
            wkt = crs.toWkt(Qgis.CrsWktVariant.Preferred)
        except (AttributeError, TypeError):
            try:
                wkt = crs.toWkt(QgsCoordinateReferenceSystem.WktVariant.WKT_PREFERRED)
            except (AttributeError, TypeError):
                wkt = crs.toWkt()
        wkt = " ".join(wkt.split())
        return wkt or crs.authid()

    @staticmethod
    def _parse_crs(text):
        text = text.strip()
        if not text:
            return None
        crs = QgsCoordinateReferenceSystem(text)
        if not crs.isValid():
            crs = QgsCoordinateReferenceSystem.fromWkt(text)
        return crs if crs.isValid() else None

    @guarded
    def load_gcp_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Wczytaj punkty GCP"), "",
            self.tr("Pliki GCP (*.points *.csv *.txt);;Wszystkie pliki (*.*)"))
        if not path:
            return
        if self.gcps:
            ans = QMessageBox.question(
                self, "QMapAnalyst", self.tr("Zastąpić istniejące punkty ({n})?\n\nNie – dopisz nowe punkty do istniejących.").format(n=len(self.gcps)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.Yes:
                self.clear_all_gcp()
                self.ref_crs = None

        crs_def, rows, skipped = gcp_io.read_file(path)
        if not rows:
            raise ValueError(self.tr("Plik nie zawiera punktów."))
        file_crs = self._parse_crs(crs_def) if crs_def else None

        ct = None
        if file_crs is not None:
            if self.ref_crs is not None and self.gcps and self.ref_crs != file_crs:
                # dopisywanie punktów z innego układu – przeliczenie do układu istniejących punktów
                ct = QgsCoordinateTransform(file_crs, self.ref_crs, self.project)
            else:
                self.ref_crs = file_crs
        else:
            self._ensure_ref_crs()

        for ox, oy, rx, ry, en in rows:
            if ct is not None:
                p = ct.transform(QgsPointXY(rx, ry))
                rx, ry = p.x(), p.y()
            self.add_gcp(ox, oy, rx, ry, enabled=en)
        added = len(rows)
        self.gcps_changed()
        self.update_ref_markers()
        msg = self.tr("Wczytano {n} punktów (układ: {crs}).").format(n=added, crs=self.crs_short(self.ref_crs))
        if ct is not None:
            msg += " " + self.tr("Współrzędne przeliczono z układu {crs}.").format(crs=self.crs_short(file_crs))
        if skipped:
            msg += " " + self.tr("Pominięto {n} niepoprawnych wierszy.").format(n=skipped)
        self.set_status(msg, "ok" if not skipped else "warn")

    # ------------------------------------------------------------------
    #  Analiza
    # ------------------------------------------------------------------
    def on_slider_changed(self, val):
        _, unit = self.ref_units()
        self.lbl_slider_val.setText(f"{val} {unit}")

    def on_settings_changed(self, *args):
        if self.analysis is not None and self.chk_auto.isChecked():
            self._recalc_timer.start()

    def on_dpi_changed(self, *args):
        if self.analysis is not None:
            self.render_report()
            if self.projection is not None:
                self.fill_projection_table()

    @guarded
    def run_analysis(self, auto=False):
        old, ref, idx = self.active_arrays()
        model = self.cmb_transform_type.currentData()
        meta = me.MODELS[model]
        if len(idx) < meta["min_pts"]:
            msg = self.tr("Transformacja „{name}” wymaga minimum {k} aktywnych punktów (aktywne: {n} z {t}).").format(
                name=self.tr(meta["name"]), k=meta["min_pts"], n=len(idx), t=len(self.gcps))
            self.txt_report.setHtml(f"<p style='color:#cf222e'>{msg}</p>")
            if not auto:
                self.set_status(msg, "warn")
            self.analysis = None
            self.analysis_stale = auto
            self.refresh_table()
            self.update_enabled_state()
            return

        with self.busy(self.tr("Obliczanie transformacji…")):
            fit = me.fit_transform(model, old, ref)
            m_per_unit, unit = self.ref_units()
            st = AnalysisState(fit, idx, self._ensure_ref_crs(), [i + 1 for i in idx], m_per_unit, unit)
            st.comparison = me.compare_models(old, ref)
            if len(idx) >= 5:
                v = fit.residuals
                st.moran = {
                    "x": me.morans_i(old, v[:, 0], 499),
                    "y": me.morans_i(old, v[:, 1], 499),
                    "abs": me.morans_i(old, fit.errors, 499),
                }
            if len(idx) >= 4 and st.warp.ok:
                st.distortion = me.summarize_distortion(st.grid())
            self.analysis = st
            self.analysis_stale = False

            span = float(np.max(np.ptp(ref, axis=0)))
            if span > 0:
                lo, hi = max(1, int(span * 0.02)), max(1000, int(span))
                self.sld_confidence.blockSignals(True)
                self.sld_confidence.setRange(lo, hi)
                self.sld_confidence.setSingleStep(max(1, int(span / 100)))
                if not (lo <= self.sld_confidence.value() <= hi):
                    self.sld_confidence.setValue(int(span * 0.35))
                self.sld_confidence.blockSignals(False)
                self.on_slider_changed(self.sld_confidence.value())
            self.spin_grid_step.setSuffix(f" {unit}")

            self.refresh_table()
            self.render_report()
            self.update_enabled_state()
            self.refresh_existing_layers()

        st_ = fit.stats
        M = me.scale_denominator(fit.derived["scale"] * m_per_unit, self.spin_dpi.value())
        n_out = int(np.sum(np.nan_to_num(fit.std_residuals) > me.OUTLIER_THRESHOLD))
        msg = self.tr("Obliczono ({n} pkt): σ₀ = {s:.2f} {u}, RMSE = {r:.2f} {u}, skala ≈ {m}").format(
            n=len(idx), s=st_["sigma0"], r=st_["rmse"], u=unit, m=report.fmt_scale(M))
        if n_out:
            msg += " · " + self.tr("punkty odstające: {k}").format(k=n_out)
        if auto and self.btn_capture.isChecked():
            # w trakcie dodawania punktów zachowaj instrukcję dla użytkownika
            msg = self.tr("Para #{n} dodana ({m}). KROK 1/2: kliknij kolejny punkt na mapie dawnej.").format(
                n=len(self.gcps), m=msg)
            if not self.btn_sync_views.isChecked():
                msg += " " + self.tr("Włącz synchronizację widoków, aby mapa główna podążała za skanem.")
        self.set_status(msg, "warn" if n_out else "ok")

    def report_context(self, colors):
        st = self.analysis
        ctx = {
            "fit": st.fit, "comparison": st.comparison, "distortion": st.distortion, "moran": st.moran,
            "n_total": len(self.gcps), "crs_label": self.ref_crs_label(), "dpi": float(self.spin_dpi.value()),
            "labels": st.labels, "unit": st.unit, "m_per_unit": st.m_per_unit,
        }
        if self.projection is not None and not self.projection_stale:
            ctx["projection_html"] = report.projection_summary_html(self.projection, ctx["dpi"], colors)
        return ctx

    def render_report(self):
        if self.analysis is None:
            return
        colors = report.palette_colors(self.txt_report)
        html = report.analysis_html(self.report_context(colors), colors)
        bar = self.txt_report.verticalScrollBar()
        pos = bar.value()
        self.txt_report.setHtml(html)
        bar.setValue(pos)

    def refresh_existing_layers(self):
        """Odświeża warstwy wynikowe, które już istnieją (po przeliczeniu)."""
        jobs = [
            ("main.points", self.generate_gcp_points),
            ("main.vectors", self.generate_result_layers),
            ("main.circles", self.generate_error_circles),
            ("main.grid", self.generate_distortion_grid),
            ("old.vectors", self.generate_old_vectors),
            ("old.circles", self.generate_old_circles),
            ("old.refgrid", self.generate_historic_grid),
            ("old.iso.scale", lambda: self.generate_isolines("scale")),
            ("old.iso.rot", lambda: self.generate_isolines("rot")),
            ("old.iso.omega", lambda: self.generate_isolines("omega")),
        ]
        for key, fn in jobs:
            if self.registry.keys(key):
                fn()

    def _require(self, raster=False, min_pts=0):
        if self.analysis is None:
            self.set_status(self.tr("Najpierw oblicz transformację."), "warn")
            return False
        if raster and self.old_raster_layer is None:
            self.set_status(self.tr("Najpierw wczytaj skan mapy dawnej."), "warn")
            return False
        if self.analysis.fit.stats["n"] < min_pts:
            self.set_status(self.tr("Ta operacja wymaga minimum {k} aktywnych punktów.").format(k=min_pts), "warn")
            return False
        return True

    # ------------------------------------------------------------------
    #  Warstwy na mapie głównej
    # ------------------------------------------------------------------
    def _mag(self):
        return float(self.spin_magnification.value())

    def _suffix_mag(self):
        m = self._mag()
        return f" ×{m:g}" if m != 1 else ""

    @guarded
    def generate_gcp_points(self):
        if not self._require():
            return
        st = self.analysis
        fit = st.fit
        pos = {row: k for k, row in enumerate(st.active_idx)}
        lyr = memory_layer("Point", st.ref_crs, self.tr("QMapAnalyst – Punkty GCP"), [
            ("id", "int"), ("active", "int"), ("x_old", "double"), ("y_old", "double"),
            ("x_ref", "double"), ("y_ref", "double"), ("vx", "double"), ("vy", "double"),
            ("error", "double"), ("loo", "double"), ("w", "double"), ("outlier", "int"),
        ])
        items = []
        for i, g in enumerate(self.gcps):
            k = pos.get(i)
            if k is not None:
                w = float(fit.std_residuals[k])
                out = 1 if (math.isfinite(w) and w > me.OUTLIER_THRESHOLD) else 0
                vals = [float(fit.residuals[k, 0]), float(fit.residuals[k, 1]), float(fit.errors[k]),
                        float(fit.loo_errors[k]) if math.isfinite(fit.loo_errors[k]) else None,
                        w if math.isfinite(w) else None, out]
            else:
                vals = [None, None, None, None, None, 2]
            items.append((QgsGeometry.fromPointXY(QgsPointXY(g.ref_x, g.ref_y)),
                          [i + 1, int(g.enabled), g.old_x, g.old_y, g.ref_x, g.ref_y] + vals))
        add_features(lyr, items)
        style_points(lyr)
        set_labels(lyr, '"id"', QColor(30, 30, 30))
        self.registry.set_main("main.points", lyr)
        self.set_status(self.tr("Wygenerowano warstwę punktów GCP z resztami."), "ok")

    @guarded
    def generate_result_layers(self):
        if not self._require():
            return
        st = self.analysis
        fit = st.fit
        mag = self._mag()
        lyr = memory_layer("LineString", st.ref_crs, self.tr("QMapAnalyst – Wektory przesunięć") + self._suffix_mag(),
                           [("id", "int"), ("error", "double"), ("vx", "double"), ("vy", "double")])
        items = []
        for k, row in enumerate(st.active_idx):
            s = fit.pts_transformed[k]
            e = s + mag * fit.residuals[k]
            items.append((polyline([s, e]), [row + 1, float(fit.errors[k]),
                                             float(fit.residuals[k, 0]), float(fit.residuals[k, 1])]))
        add_features(lyr, items)
        style_arrows(lyr, QColor(220, 30, 30), 0.45)
        self.registry.set_main("main.vectors", lyr)
        self.set_status(self.tr("Wygenerowano wektory przesunięć."), "ok")

    @guarded
    def generate_error_circles(self):
        if not self._require():
            return
        st = self.analysis
        fit = st.fit
        mag = self._mag()
        lyr = memory_layer("Polygon", st.ref_crs, self.tr("QMapAnalyst – Okręgi błędów") + self._suffix_mag(),
                           [("id", "int"), ("error", "double"), ("outlier", "int")])
        items = []
        for k, row in enumerate(st.active_idx):
            r = float(fit.errors[k]) * mag
            w = fit.std_residuals[k]
            out = int(math.isfinite(w) and w > me.OUTLIER_THRESHOLD)
            geom = QgsGeometry.fromPointXY(QgsPointXY(*fit.dst[k])).buffer(max(r, 1e-9), 36)
            items.append((geom, [row + 1, float(fit.errors[k]), out]))
        add_features(lyr, items)
        style_outline(lyr, QColor(30, 136, 229), 0.4, field="outlier", alt_color=QColor(229, 57, 53))
        self.registry.set_main("main.circles", lyr)
        self.set_status(self.tr("Wygenerowano okręgi błędów."), "ok")

    def get_confidence_mask(self, pts, radius):
        buffers = [QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))).buffer(radius, 16) for x, y in pts]
        unioned = QgsGeometry.unaryUnion(buffers)
        if unioned.isEmpty():
            return unioned
        parts = unioned.asGeometryCollection() if unioned.isMultipart() else [unioned]
        hull = QgsGeometry.unaryUnion([p.convexHull() for p in parts])
        return hull if not hull.isEmpty() else unioned

    @guarded
    def generate_distortion_grid(self):
        if not self._require(min_pts=3):
            return
        st = self.analysis
        fit = st.fit
        with self.busy(self.tr("Obliczanie siatki zniekształceń…")):
            step_px = float(self.spin_grid_step.value()) / st.scale if st.scale > 0 else 100.0
            mn, mx = fit.src.min(axis=0), fit.src.max(axis=0)
            cols = int(np.clip(round((mx[0] - mn[0]) / step_px), 1, 400))
            rows = int(np.clip(round((mx[1] - mn[1]) / step_px), 1, 400))
            xs = np.linspace(mn[0], mx[0], cols + 1)
            ys = np.linspace(mn[1], mx[1], rows + 1)
            dens = 4  # zagęszczenie linii między węzłami (gładkość krzywych)
            xd = np.linspace(mn[0], mx[0], cols * dens + 1)
            yd = np.linspace(mn[1], mx[1], rows * dens + 1)
            lines_src = [np.column_stack([xd, np.full_like(xd, y)]) for y in ys] + \
                        [np.column_stack([np.full_like(yd, x), yd]) for x in xs]
            allpts = np.vstack(lines_src)
            mapped = st.warp.predict(allpts)

            mask = self.get_confidence_mask(fit.dst, float(self.sld_confidence.value()))
            rel = memory_layer("LineString", st.ref_crs, self.tr("QMapAnalyst – Siatka (obszar pewny)"), [("dir", "string")])
            unc = memory_layer("LineString", st.ref_crs, self.tr("QMapAnalyst – Siatka (ekstrapolacja)"), [("dir", "string")])
            rel_items, unc_items = [], []
            off = 0
            for i, ln in enumerate(lines_src):
                seg = mapped[off:off + len(ln)]
                off += len(ln)
                d = "E-W" if i < len(ys) else "N-S"
                geom = polyline(seg)
                for part in split_lines(geom.intersection(mask)):
                    rel_items.append((part, [d]))
                for part in split_lines(geom.difference(mask)):
                    unc_items.append((part, [d]))
            add_features(rel, rel_items)
            add_features(unc, unc_items)
            style_line(rel, QColor(103, 58, 183), 0.4)
            style_line(unc, QColor(140, 140, 140), 0.3, dashed=True)
            self.registry.set_main("main.grid.unc", unc)
            self.registry.set_main("main.grid", rel)
        self.set_status(self.tr("Siatka zniekształceń: {a} odcinków pewnych, {b} ekstrapolowanych.").format(
            a=len(rel_items), b=len(unc_items)), "ok")

    # ------------------------------------------------------------------
    #  Warstwy na mapie dawnej
    # ------------------------------------------------------------------
    def _old_layer(self, geom, name, fields):
        crs = self.old_raster_layer.crs() if self.old_raster_layer else None
        return memory_layer(geom, crs, name, fields)

    @guarded
    def generate_old_vectors(self):
        if not self._require(raster=True):
            return
        st = self.analysis
        fit = st.fit
        mag = self._mag()
        J = fit.jacobian(fit.src)
        lyr = self._old_layer("LineString", self.tr("Wektory przesunięć") + self._suffix_mag(),
                              [("id", "int"), ("error", "double")])
        items = []
        for k, row in enumerate(st.active_idx):
            try:
                v_px = np.linalg.solve(J[k], fit.residuals[k])
            except np.linalg.LinAlgError:
                continue
            s = fit.src[k]
            items.append((polyline([s, s + mag * v_px]), [row + 1, float(fit.errors[k])]))
        add_features(lyr, items)
        style_arrows(lyr, QColor(220, 30, 30), 0.45)
        self.registry.set_old("old.vectors", lyr)
        self.set_status(self.tr("Wygenerowano wektory przesunięć na mapie dawnej."), "ok")

    @guarded
    def generate_old_circles(self):
        if not self._require(raster=True):
            return
        st = self.analysis
        fit = st.fit
        mag = self._mag()
        lyr = self._old_layer("Polygon", self.tr("Okręgi błędów") + self._suffix_mag(),
                              [("id", "int"), ("error", "double"), ("outlier", "int")])
        items = []
        for k, row in enumerate(st.active_idx):
            r = float(fit.errors[k]) / st.scale * mag if st.scale > 0 else 0.0
            w = fit.std_residuals[k]
            out = int(math.isfinite(w) and w > me.OUTLIER_THRESHOLD)
            geom = QgsGeometry.fromPointXY(QgsPointXY(*fit.src[k])).buffer(max(r, 1e-9), 36)
            items.append((geom, [row + 1, float(fit.errors[k]), out]))
        add_features(lyr, items)
        style_outline(lyr, QColor(30, 136, 229), 0.4, field="outlier", alt_color=QColor(229, 57, 53))
        self.registry.set_old("old.circles", lyr)
        self.set_status(self.tr("Wygenerowano okręgi błędów na mapie dawnej."), "ok")

    @guarded
    def generate_historic_grid(self):
        if not self._require(raster=True, min_pts=3):
            return
        st = self.analysis
        fit = st.fit
        with self.busy(self.tr("Obliczanie siatki układu odniesienia na mapie dawnej…")):
            inv = st.inverse_warp
            step = float(self.spin_grid_step.value())
            mn, mx = fit.dst.min(axis=0), fit.dst.max(axis=0)
            x0, x1 = math.floor(mn[0] / step) * step, math.ceil(mx[0] / step) * step
            y0, y1 = math.floor(mn[1] / step) * step, math.ceil(mx[1] / step) * step
            nx, ny = int((x1 - x0) / step) + 1, int((y1 - y0) / step) + 1
            if nx * ny > 250000:
                self.set_status(self.tr("Zbyt gęsta siatka ({n} linii) – zwiększ oczko siatki.").format(n=nx + ny), "warn")
                return
            xs = np.linspace(x0, x1, nx)
            ys = np.linspace(y0, y1, ny)
            yd = np.linspace(y0, y1, max(40, 4 * ny))
            xd = np.linspace(x0, x1, max(40, 4 * nx))
            lines = [("E", x, np.column_stack([np.full_like(yd, x), yd])) for x in xs] + \
                    [("N", y, np.column_stack([xd, np.full_like(xd, y)])) for y in ys]
            mapped = inv.predict(np.vstack([ln for _, _, ln in lines]))

            radius_px = float(self.sld_confidence.value()) / st.scale if st.scale > 0 else 200.0
            mask = self.get_confidence_mask(fit.src, radius_px * 1.5)
            lyr = self._old_layer("LineString", self.tr("Siatka {crs}").format(crs=st.ref_crs.authid() or self.tr("układu odniesienia")),
                                  [("axis", "string"), ("value", "double")])
            items = []
            off = 0
            for axis, val, ln in lines:
                seg = mapped[off:off + len(ln)]
                off += len(ln)
                for part in split_lines(polyline(seg).intersection(mask)):
                    items.append((part, [axis, float(val)]))
            add_features(lyr, items)
            style_line(lyr, QColor(138, 43, 226), 0.35)
            set_labels(lyr, "concat(\"axis\", ' ', format_number(\"value\", 0))", QColor(110, 30, 180), on_line=True, size=7)
            self.registry.set_old("old.refgrid", lyr)
        self.set_status(self.tr("Wygenerowano siatkę układu odniesienia ({n} linii).").format(n=len(items)), "ok")

    @guarded
    def generate_isolines(self, kind="scale"):
        if not self._require(raster=True, min_pts=4):
            return
        st = self.analysis
        with self.busy(self.tr("Obliczanie izolinii…")):
            grid = st.grid()
            dpi = float(self.spin_dpi.value())
            if kind == "scale":
                if not math.isfinite(st.m_per_unit):
                    self.set_status(self.tr("Izolinie skali wymagają metrycznego układu odniesienia."), "warn")
                    return
                Z = grid["area_scale"] * st.m_per_unit * dpi / me.INCH_M
                name = self.tr("Izolinie skali (1:M, {dpi} DPI)").format(dpi=int(dpi))
                expr = "concat('1:', format_number(\"value\", 0))"
                color = QColor(0, 137, 123)
            elif kind == "rot":
                Z = grid["rotation_deg"]
                name = self.tr("Izolinie rotacji [°]")
                expr = "concat(format_number(\"value\", 2), '°')"
                color = QColor(230, 126, 34)
            else:
                Z = grid["omega_deg"]
                name = self.tr("Izolinie zniekształceń kątowych 2ω [°]")
                expr = "concat(format_number(\"value\", 2), '°')"
                color = QColor(25, 118, 210)

            levels = me.nice_levels(Z[grid["inside"]] if np.any(grid["inside"]) else Z, self.spin_iso_density.value())
            radius_px = float(self.sld_confidence.value()) / st.scale if st.scale > 0 else 200.0
            mask = self.get_confidence_mask(st.fit.src, radius_px)
            lyr = self._old_layer("LineString", name, [("value", "double")])
            items = []
            for lev in levels:
                segs = me.marching_squares(grid["x"], grid["y"], Z, float(lev))
                for geom in merge_segments(segs):
                    for part in split_lines(geom.intersection(mask)):
                        items.append((part, [float(lev)]))
            add_features(lyr, items)
            style_line(lyr, color, 0.45)
            set_labels(lyr, expr, color, on_line=True)
            self.registry.set_old(f"old.iso.{kind}", lyr)
        self.set_status(self.tr("Izolinie gotowe: {n} linii, {k} poziomów.").format(n=len(items), k=len(levels)), "ok")

    # ------------------------------------------------------------------
    #  Estymacja odwzorowania
    # ------------------------------------------------------------------
    def _lonlat(self, ref):
        crs = self._ensure_ref_crs()
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs == wgs:
            return np.array(ref, dtype=float)
        ct = QgsCoordinateTransform(crs, wgs, self.project)
        out = []
        for x, y in ref:
            p = ct.transform(QgsPointXY(float(x), float(y)))
            out.append([p.x(), p.y()])
        return np.array(out, dtype=float)

    @guarded
    def estimate_projection(self):
        old, ref, idx = self.active_arrays()
        mode = self.cmb_proj_mode.currentData()
        need = 5 if mode == "helmert" else 6
        if len(idx) < need:
            self.set_status(self.tr("Estymacja odwzorowania wymaga minimum {k} aktywnych punktów.").format(k=need), "warn")
            return
        ll = self._lonlat(ref)
        self.progress.setRange(0, len(pj.PROJECTIONS))
        self.progress.setValue(0)
        self.progress.show()

        def progress(i, n):
            self.progress.setValue(i)

        with self.busy(self.tr("Dopasowywanie odwzorowań kartograficznych…")):
            try:
                ref_label = self.tr("Układ odniesienia ({crs})").format(crs=self.crs_short(self.ref_crs))
                self.projection = pj.estimate_projections(ll, old, ref_xy=ref, ref_label=ref_label,
                                                          mode=mode, progress=progress,
                                                          labels=[i + 1 for i in idx])
            finally:
                self.progress.hide()
        self.projection_stale = False
        self.fill_projection_table()
        self.update_enabled_state()
        self.render_report()
        best = self.projection.best
        if best is not None and self.projection.conclusive:
            self.set_status(self.tr("Najlepsze dopasowanie: {name} (σ₀ = {s:.2f} px, waga {w:.0f}%).").format(
                name=self.tr(best.name), s=best.sigma0_px, w=best.weight * 100), "ok")
        elif best is not None:
            eq = self.projection.equivalent()
            self.set_status(self.tr("Wynik nierozstrzygnięty: {k} odwzorowań równorzędnych (ΔAICc < 2). "
                                    "Porównaj siatki geograficzne kandydatów z mapą.").format(k=max(1, len(eq))), "warn")

    def fill_projection_table(self):
        est = self.projection
        t = self.tbl_proj
        t.blockSignals(True)
        t.setRowCount(0)
        if est is None:
            t.blockSignals(False)
            return
        dpi = float(self.spin_dpi.value())
        best = est.best
        highlight = [best] if est.conclusive else est.equivalent()
        t.setRowCount(len(est.candidates))
        for r, c in enumerate(est.candidates):
            name = QTableWidgetItem(self.tr(c.name) + (self.tr(" (hipoteza)") if c.is_hypothesis else ""))
            name.setToolTip(f"{self.tr(c.family)} · {self.tr(c.era)}\n{self.tr(c.note)}")
            if c.is_hypothesis:
                f = name.font()
                f.setItalic(True)
                name.setFont(f)
                name.setForeground(QColor(200, 0, 120))
            if any(c is h for h in highlight):
                f = name.font()
                f.setBold(True)
                name.setFont(f)
            if c.is_reference:
                name.setForeground(C_OFF)
            if not c.success:
                name.setForeground(QColor(207, 34, 46))
            t.setItem(r, 0, name)
            t.setItem(r, 1, _num_item(c.sigma0_px, 2))
            t.setItem(r, 2, _num_item(c.delta_aicc, 1))
            t.setItem(r, 3, _num_item(c.weight * 100, 1))
            sc = QTableWidgetItem(report.fmt_scale(c.scale_denominator(dpi)))
            sc.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            t.setItem(r, 4, sc)
        t.blockSignals(False)
        if best is not None:
            t.selectRow(est.candidates.index(best))
        self.on_projection_selected()

    def selected_candidate(self):
        if self.projection is None:
            return None
        rows = sorted({i.row() for i in self.tbl_proj.selectedIndexes()})
        if rows and rows[0] < len(self.projection.candidates):
            return self.projection.candidates[rows[0]]
        return self.projection.best

    def on_projection_selected(self):
        c = self.selected_candidate()
        if c is None:
            return
        colors = report.palette_colors(self.txt_proj)
        self.txt_proj.setHtml(report.candidate_html(c, self.projection, float(self.spin_dpi.value()), colors))
        self.btn_graticule.setEnabled(not c.is_reference and self.old_raster_layer is not None)

    def _graticule_range(self, est):
        """Zasięg λ/φ siatki: punkty GCP oraz – jeśli to możliwe – cały skan."""
        lon_r, lat_r = list(est.lon_range), list(est.lat_range)
        if self.analysis is not None and self.old_raster_layer is not None:
            try:
                e = self.old_raster_layer.extent()
                xs = np.linspace(e.xMinimum(), e.xMaximum(), 5)
                ys = np.linspace(e.yMinimum(), e.yMaximum(), 5)
                border = np.array([[x, y] for x in xs for y in ys])
                ll = self._lonlat(self.analysis.fit.predict(border))
                lon_r = [min(lon_r[0], ll[:, 0].min()), max(lon_r[1], ll[:, 0].max())]
                lat_r = [min(lat_r[0], ll[:, 1].min()), max(lat_r[1], ll[:, 1].max())]
            except Exception:  # noqa: BLE001
                pass
        pad_lon = (lon_r[1] - lon_r[0]) * 0.05 + 0.02
        pad_lat = (lat_r[1] - lat_r[0]) * 0.05 + 0.02
        return (lon_r[0] - pad_lon, lon_r[1] + pad_lon), (lat_r[0] - pad_lat, lat_r[1] + pad_lat)

    @guarded
    def generate_graticule(self):
        c = self.selected_candidate()
        if c is None or c.is_reference:
            self.set_status(self.tr("Wybierz odwzorowanie z listy (nie układ odniesienia)."), "warn")
            return
        if self.old_raster_layer is None:
            self.set_status(self.tr("Najpierw wczytaj skan mapy dawnej."), "warn")
            return
        est = self.projection
        lon_r, lat_r = self._graticule_range(est)
        step = self.cmb_grat_step.currentData() or pj.auto_graticule_step(lon_r, lat_r)
        n_lines = ((lon_r[1] - lon_r[0]) + (lat_r[1] - lat_r[0])) / step
        if n_lines > 400:
            self.set_status(self.tr("Zbyt gęsta siatka – wybierz większe oczko."), "warn")
            return
        lines = pj.graticule_lines(c, lon_r, lat_r, step, samples=80)
        ext = self.old_raster_layer.extent()
        clip = QgsGeometry.fromRect(ext)
        title = self.tr("Siatka geograficzna – {name}").format(name=self.tr(c.name))
        if c.is_hypothesis:
            title += self.tr(" (hipoteza)")
        lyr = self._old_layer("LineString", title, [("kind", "string"), ("value", "double"), ("label", "string")])
        items = []
        for kind, val, xy in lines:
            hemi = ("E", "W") if kind == "meridian" else ("N", "S")
            label = pj.graticule_label(val, *hemi)
            for part in split_lines(polyline(xy).intersection(clip)):
                items.append((part, [kind, val, label]))
        add_features(lyr, items)
        color = QColor(200, 0, 120) if c.is_hypothesis else QColor(0, 96, 160)
        style_line(lyr, color, 0.35, dashed=c.is_hypothesis)
        set_labels(lyr, '"label"', color.darker(115), on_line=True, size=7)
        self.registry.set_old(f"old.graticule.{c.key}" + (".hyp" if c.is_hypothesis else ""), lyr)
        self.set_status(self.tr("Narysowano siatkę geograficzną ({n} linii, oczko {s}).").format(
            n=len(items), s=pj.step_label(step)), "ok")

    # ------------------------------------------------------------------
    #  Eksport
    # ------------------------------------------------------------------
    @guarded
    def execute_export(self):
        out_dir = self.file_export_dir.filePath()
        if not out_dir or not os.path.isdir(out_dir):
            self.set_status(self.tr("Wybierz istniejący folder eksportu."), "warn")
            return
        if self.analysis is None:
            self.set_status(self.tr("Brak wyników do eksportu – najpierw oblicz transformację."), "warn")
            return
        done = []
        self.btn_execute_export.setEnabled(False)
        try:
            with self.busy(self.tr("Eksportowanie…")):
                if self.chk_exp_csv.isChecked():
                    done.append(self.export_gcp_csv(os.path.join(out_dir, "QMapAnalyst_GCP.csv")))
                if self.chk_exp_proj.isChecked() and self.projection is not None:
                    done.append(self.export_projection_csv(os.path.join(out_dir, "QMapAnalyst_Odwzorowania.csv")))
                if self.chk_exp_tiff.isChecked() and self.old_raster_layer is not None:
                    done.append(ex.export_canvas_image(self.old_canvas, os.path.join(out_dir, "QMapAnalyst_Map.tif")))
                if self.chk_exp_pdf.isChecked():
                    done.append(self.export_pdf(os.path.join(out_dir, "QMapAnalyst_Raport.pdf")))
        finally:
            self.btn_execute_export.setEnabled(True)
        self.set_status(self.tr("Wyeksportowano {n} plików do: {d}").format(n=len(done), d=out_dir), "ok")

    def export_pdf(self, path):
        colors = dict(report.LIGHT, font_pt=8)
        pages = [(self.tr("Analiza kartometryczna"), report.analysis_html(
            {**self.report_context(colors), "projection_html": None}, colors))]
        if self.projection is not None and not self.projection_stale:
            html = report.projection_summary_html(self.projection, float(self.spin_dpi.value()), colors, with_css=True)
            best = self.projection.best
            if best is not None:
                html += report.candidate_html(best, self.projection, float(self.spin_dpi.value()), colors)
            pages.append((self.tr("Estymacja odwzorowania"), html))
        base = self.old_raster_layer
        extent = QgsRectangle(base.extent()) if base is not None else self.old_canvas.extent()
        extent.scale(1.02)
        return ex.export_report_pdf(path, pages, base, self.old_overlay_layers(), extent,
                                    self.tr("Analiza kartometryczna"))

    def export_gcp_csv(self, path):
        st = self.analysis
        fit = st.fit
        pos = {row: k for k, row in enumerate(st.active_idx)}
        rows = []
        for i, g in enumerate(self.gcps):
            k = pos.get(i)
            base = [i + 1, int(g.enabled), g.old_x, g.old_y, g.ref_x, g.ref_y]
            if k is None or self.analysis_stale:
                rows.append(base + [""] * 7)
            else:
                rows.append(base + [float(v) for v in (
                    fit.pts_transformed[k, 0], fit.pts_transformed[k, 1], fit.residuals[k, 0],
                    fit.residuals[k, 1], fit.errors[k], fit.loo_errors[k], fit.std_residuals[k])])
        header = ["id", "enable", "x_old", "y_old", "x_ref", "y_ref", "x_trans", "y_trans", "vx", "vy",
                  "error", "loo", "w"]
        return ex.write_csv(path, header, rows)

    def export_projection_csv(self, path):
        dpi = float(self.spin_dpi.value())
        rows = []
        for r, c in enumerate(self.projection.candidates, 1):
            params = "; ".join(f"{k}={v:.4f}±{c.params_std_deg.get(k, float('nan')):.4f}" for k, v in c.params_deg.items())
            rows.append([r, c.key, c.name, f"{c.sigma0_px:.4f}", f"{c.delta_aicc:.3f}", f"{c.weight:.4f}",
                         f"{c.scale_denominator(dpi):.0f}", params])
        return ex.write_csv(path, ["rank", "key", "name", "sigma0_px", "delta_aicc", "weight",
                                   f"scale_{int(dpi)}dpi", "params_deg"], rows)

    # ------------------------------------------------------------------
    #  Cykl życia
    # ------------------------------------------------------------------
    def _set_markers_visible(self, visible):
        for g in self.gcps:
            for m in (g.old_marker, g.ref_marker):
                if m is not None:
                    m.setVisible(visible)

    def showEvent(self, event):
        self._set_markers_visible(True)
        super().showEvent(event)

    def closeEvent(self, event):
        if self.btn_capture.isChecked():
            self.btn_capture.setChecked(False)
        self._restore_main_tool()
        self._set_markers_visible(False)
        super().closeEvent(event)

    def cleanup(self):
        """Pełne sprzątanie przy wyładowaniu wtyczki."""
        self._recalc_timer.stop()
        try:
            self._restore_main_tool()
            self.main_canvas.destinationCrsChanged.disconnect(self.update_ref_markers)
        except (TypeError, RuntimeError):
            pass
        self._remove_temp_marker()
        self._remove_pred_marker()
        for sig, slot in ((self.main_canvas.extentsChanged, self.sync_old_canvas_to_main),
                          (self.old_canvas.extentsChanged, self.sync_main_canvas_to_old)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.clear_all_gcp()
        self.registry.clear_old()
        self.registry.disconnect_project()
        self.old_canvas.stopRendering()
        try:
            self.old_canvas.mapToolSet.disconnect(self.sync_tool_buttons)
        except (TypeError, RuntimeError):
            pass
        if self.old_canvas.mapTool() is not None:
            self.old_canvas.unsetMapTool(self.old_canvas.mapTool())
        self.old_canvas.setLayers([])
        self.old_raster_layer = None
