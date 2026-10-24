import csv
import numpy as np

from qgis.PyQt.QtCore import Qt, QMetaType, QCoreApplication
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QPushButton, QTextEdit, QGroupBox, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter, QLabel,
    QTabWidget, QSlider, QGridLayout, QSpinBox, QListWidget, QListWidgetItem
)
from qgis.gui import (
    QgsMapCanvas, QgsMapToolPan, QgsMapToolEmitPoint, QgsVertexMarker
)
from qgis.core import (
    QgsRasterLayer, QgsPointXY, QgsVectorLayer, 
    QgsFeature, QgsGeometry, QgsProject, QgsField,
    QgsSymbol, QgsSingleSymbolRenderer, QgsWkbTypes
)

from .math_engine import MathEngine


class QMapAnalystDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("QMapAnalyst", parent)
        self.iface = iface
        self.main_canvas = self.iface.mapCanvas()
        self.setObjectName("QMapAnalystDockWidget")

        self.old_raster_layer = None
        self.old_overlay_layers = []
        self.old_markers = []
        self.ref_markers = []
        self.temp_old_point = None
        self.temp_old_marker = None
        
        self.last_results = None
        self._updating_table = False

        self.setup_ui()
        self.setup_map_tools()

    def tr(self, message):
        return QCoreApplication.translate('QMapAnalystDockWidget', message)

    def setup_ui(self):
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # --- 1. SEKCJA MAPY DAWNEJ ---
        canvas_group = QGroupBox("Mapa dawna (Skan / Piksele)")
        canvas_layout = QVBoxLayout()

        toolbar_layout = QHBoxLayout()
        self.btn_load_raster = QPushButton("Wczytaj skan...")
        self.btn_load_raster.clicked.connect(self.load_old_raster)

        self.btn_pan_old = QPushButton("Przesuwaj")
        self.btn_pan_old.setCheckable(True)
        self.btn_pan_old.clicked.connect(self.activate_pan_old)

        self.btn_zoom_full = QPushButton("Pełny widok")
        self.btn_zoom_full.clicked.connect(self.zoom_full_old)

        self.btn_clear_overlays = QPushButton("🧹 Wyczyść nakładki")
        self.btn_clear_overlays.clicked.connect(self.clear_old_overlays)
        
        self.btn_sync_views = QPushButton("🔗 Synchronizuj widok")
        self.btn_sync_views.setCheckable(True)
        self.btn_sync_views.setEnabled(False)
        
        toolbar_layout.addWidget(self.btn_load_raster)
        toolbar_layout.addWidget(self.btn_pan_old)
        toolbar_layout.addWidget(self.btn_zoom_full)
        toolbar_layout.addWidget(self.btn_sync_views)  # <-- DODANO
        toolbar_layout.addWidget(self.btn_clear_overlays)
        toolbar_layout.addWidget(self.btn_load_raster)
        toolbar_layout.addWidget(self.btn_pan_old)
        toolbar_layout.addWidget(self.btn_zoom_full)
        toolbar_layout.addWidget(self.btn_clear_overlays)
        canvas_layout.addLayout(toolbar_layout)

        self.old_canvas = QgsMapCanvas()
        self.old_canvas.setCanvasColor(Qt.GlobalColor.white)
        self.old_canvas.setMinimumHeight(320)
        canvas_layout.addWidget(self.old_canvas)

        self.lst_old_layers = QListWidget()
        self.lst_old_layers.setMaximumHeight(80)
        self.lst_old_layers.itemChanged.connect(self.refresh_old_canvas_layers)
        canvas_layout.addWidget(QLabel("Widoczność nakładek:"))
        canvas_layout.addWidget(self.lst_old_layers)

        canvas_group.setLayout(canvas_layout)
        splitter.addWidget(canvas_group)

        # --- 2. SEKCJA ZAKŁADEK (QTabWidget) ---
        self.tab_widget = QTabWidget()

        # === ZAKŁADKA 1: Punkty GCP ===
        tab_gcp = QWidget()
        gcp_layout = QVBoxLayout(tab_gcp)

        gcp_btn_layout = QHBoxLayout()
        self.btn_add_gcp = QPushButton("➕ Dodaj parę GCP")
        self.btn_add_gcp.setCheckable(True)
        self.btn_add_gcp.clicked.connect(self.start_gcp_capture)

        self.btn_remove_gcp = QPushButton("🗑️ Usuń zaznaczony")
        self.btn_remove_gcp.clicked.connect(self.remove_selected_gcp)

        gcp_btn_layout.addWidget(self.btn_add_gcp)
        gcp_btn_layout.addWidget(self.btn_remove_gcp)
        gcp_layout.addLayout(gcp_btn_layout)

        gcp_io_layout = QHBoxLayout()
        self.btn_save_gcp = QPushButton("💾 Zapisz GCP...")
        self.btn_save_gcp.clicked.connect(self.save_gcp_to_file)

        self.btn_load_gcp = QPushButton("📂 Wczytaj GCP...")
        self.btn_load_gcp.clicked.connect(self.load_gcp_from_file)

        gcp_io_layout.addWidget(self.btn_save_gcp)
        gcp_io_layout.addWidget(self.btn_load_gcp)
        gcp_layout.addLayout(gcp_io_layout)

        self.lbl_status = QLabel("Gotowy. Wczytaj skan mapy dawnej.")
        self.lbl_status.setStyleSheet("color: #2b579a; font-weight: bold;")
        gcp_layout.addWidget(self.lbl_status)

        self.tbl_gcp = QTableWidget(0, 6)
        self.tbl_gcp.setHorizontalHeaderLabels([
            "Akt.", "X (Dawna)", "Y (Dawna)", "X (Ref)", "Y (Ref)", "Błąd [m]"
        ])
        header = self.tbl_gcp.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(1, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.tbl_gcp.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tbl_gcp.itemChanged.connect(self.on_table_item_changed)
        gcp_layout.addWidget(self.tbl_gcp)

        self.tab_widget.addTab(tab_gcp, "📍 Punkty GCP")

        # === ZAKŁADKA 2: Analiza i Transformacja ===
        tab_calc = QWidget()
        calc_layout = QVBoxLayout(tab_calc)

        form_layout = QFormLayout()
        self.cmb_transform_type = QComboBox()
        self.cmb_transform_type.addItems([
            "Helmert (4 parametry - Skala, Obrót, Translacja)",
            "Afiniczna (6 parametrów)"
        ])
        form_layout.addRow("Transformacja:", self.cmb_transform_type)
        calc_layout.addLayout(form_layout)

        self.btn_calculate = QPushButton("🧮 Oblicz transformację")
        self.btn_calculate.clicked.connect(self.run_analysis)
        calc_layout.addWidget(self.btn_calculate)

        self.txt_report = QTextEdit()
        self.txt_report.setReadOnly(True)
        self.txt_report.setPlaceholderText("Wyniki analizy...")
        calc_layout.addWidget(self.txt_report)

        self.tab_widget.addTab(tab_calc, "🧮 Analiza")

        # === ZAKŁADKA 3: Warstwy wynikowe ===
        tab_layers = QWidget()
        layers_layout = QVBoxLayout(tab_layers)

        conf_group = QGroupBox("Parametry klastrów pewności i geometrii")
        conf_form = QFormLayout()

        slider_layout = QHBoxLayout()
        self.sld_confidence = QSlider(Qt.Orientation.Horizontal)
        self.sld_confidence.setRange(100, 25000)
        self.sld_confidence.setSingleStep(250)
        self.sld_confidence.setValue(3000)
        self.sld_confidence.valueChanged.connect(self.on_slider_changed)

        self.lbl_slider_val = QLabel("3000 m")
        self.lbl_slider_val.setMinimumWidth(65)
        slider_layout.addWidget(self.sld_confidence)
        slider_layout.addWidget(self.lbl_slider_val)
        conf_form.addRow("Próg pewności (promień):", slider_layout)

        self.spin_grid_step = QSpinBox()
        self.spin_grid_step.setRange(10, 100000)
        self.spin_grid_step.setSingleStep(100)
        self.spin_grid_step.setValue(1000)
        conf_form.addRow("Oczko siatek [m]:", self.spin_grid_step)

        self.cmb_dpi = QComboBox()
        self.cmb_dpi.addItems(["300 DPI", "600 DPI", "150 DPI"])
        conf_form.addRow("Bazowe DPI (izolinie):", self.cmb_dpi)

        conf_group.setLayout(conf_form)
        layers_layout.addWidget(conf_group)

        main_map_group = QGroupBox("Generuj na mapie głównej")
        main_map_layout = QGridLayout()

        self.btn_gen_vectors = QPushButton("📏 Wektory przesunięć")
        self.btn_gen_vectors.clicked.connect(self.generate_result_layers)
        
        self.btn_gen_circles = QPushButton("⭕ Okręgi błędów")
        self.btn_gen_circles.clicked.connect(self.generate_error_circles)

        self.btn_gen_grid = QPushButton("🌐 Siatka zniekształceń")
        self.btn_gen_grid.clicked.connect(self.generate_distortion_grid)

        main_map_layout.addWidget(self.btn_gen_vectors, 0, 0)
        main_map_layout.addWidget(self.btn_gen_circles, 0, 1)
        main_map_layout.addWidget(self.btn_gen_grid, 0, 2)
        main_map_group.setLayout(main_map_layout)
        layers_layout.addWidget(main_map_group)

        # --- 3. ZAKŁADKA WARSTW WYNIKOWYCH (Układ siatki dla mapy dawnej) ---
        old_map_group = QGroupBox("Generuj na mapie dawnej (w oknie powyżej)")
        old_map_layout = QGridLayout()

        self.btn_gen_old_vectors = QPushButton("📏 Wektory przesunięć")
        self.btn_gen_old_vectors.clicked.connect(self.generate_old_vectors)

        self.btn_gen_old_circles = QPushButton("⭕ Okręgi błędów")
        self.btn_gen_old_circles.clicked.connect(self.generate_old_circles)

        self.btn_gen_historic_grid = QPushButton("🛜 Siatka PUWG92")
        self.btn_gen_historic_grid.clicked.connect(self.generate_historic_grid)

        self.btn_gen_iso_scale = QPushButton("〰️ Izolinie skali")
        self.btn_gen_iso_scale.clicked.connect(lambda: self.generate_isolines(type="scale"))

        self.btn_gen_iso_rot = QPushButton("〰️ Izolinie rotacji")
        self.btn_gen_iso_rot.clicked.connect(lambda: self.generate_isolines(type="rot"))

        # Układ: wiersz 0 (wektory, okręgi, siatka), wiersz 1 (izolinie skali, izolinie rotacji)
        old_map_layout.addWidget(self.btn_gen_old_vectors, 0, 0)
        old_map_layout.addWidget(self.btn_gen_old_circles, 0, 1)
        old_map_layout.addWidget(self.btn_gen_historic_grid, 0, 2)
        old_map_layout.addWidget(self.btn_gen_iso_scale, 1, 0)
        old_map_layout.addWidget(self.btn_gen_iso_rot, 1, 1)
        old_map_group.setLayout(old_map_layout)
        layers_layout.addWidget(old_map_group)

        layers_layout.addStretch()
        self.tab_widget.addTab(tab_layers, "🗺️ Warstwy wynikowe")

        splitter.addWidget(self.tab_widget)
        main_layout.addWidget(splitter)
        self.setWidget(main_widget)

    def on_slider_changed(self, val):
        self.lbl_slider_val.setText(f"{val} m")

    def setup_map_tools(self):
        self.tool_pan_old = QgsMapToolPan(self.old_canvas)
        self.tool_pan_old.setButton(self.btn_pan_old)

        self.tool_click_old = QgsMapToolEmitPoint(self.old_canvas)
        self.tool_click_old.canvasClicked.connect(self.on_old_canvas_clicked)

        self.tool_click_ref = QgsMapToolEmitPoint(self.main_canvas)
        self.tool_click_ref.canvasClicked.connect(self.on_ref_canvas_clicked)

        self.old_canvas.setMapTool(self.tool_pan_old)
        self.old_canvas.extentsChanged.connect(self.sync_main_canvas_to_old)

    def create_old_canvas_vector_layer(self, name: str, fields: list, geom_type: str = "LineString") -> QgsVectorLayer:
        if self.old_raster_layer and self.old_raster_layer.crs().isValid():
            crs_id = self.old_raster_layer.crs().authid()
            uri = f"{geom_type}?crs={crs_id}"
        else:
            uri = f"{geom_type}?crs="

        lyr = QgsVectorLayer(uri, name, "memory")
        if self.old_raster_layer:
            lyr.setCrs(self.old_raster_layer.crs())

        prov = lyr.dataProvider()
        prov.addAttributes(fields)
        lyr.updateFields()
        return lyr

    def add_layer_to_old_canvas(self, layer):
        """Dodaje warstwę na listę UI (zapisuje bezpiecznie tylko ID)."""
        item = QListWidgetItem(layer.name())
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked)
        item.setData(Qt.ItemDataRole.UserRole, layer.id())
        self.lst_old_layers.addItem(item)
        self.refresh_old_canvas_layers()
    
    def refresh_old_canvas_layers(self, *args):
        layers_to_render = []
        project = QgsProject.instance()

        for i in range(self.lst_old_layers.count()):
            item = self.lst_old_layers.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                lyr_id = item.data(Qt.ItemDataRole.UserRole)
                lyr = project.mapLayer(lyr_id)  # Pobranie świeżego obiektu po ID
                if lyr and lyr.isValid():
                    layers_to_render.append(lyr)

        if self.old_raster_layer and self.old_raster_layer.isValid():
            layers_to_render.append(self.old_raster_layer)

        self.old_canvas.setLayers(layers_to_render)
        self.old_canvas.refresh()

    def remove_plugin_layers_by_prefix(self, prefix: str):
        """Usuwa z projektu QGIS oraz z UI wszystkie warstwy zaczynające się od prefiksu."""
        project = QgsProject.instance()
        layers_to_remove = []
        for lyr_id, lyr in project.mapLayers().items():
            if lyr.name().startswith(prefix):
                layers_to_remove.append(lyr_id)
                
        if layers_to_remove:
            # 1. NAJPIERW zdejmujemy obiekty z listy UI (zanim usuniemy warstwy w C++)
            items_to_remove = []
            for i in range(self.lst_old_layers.count()):
                item = self.lst_old_layers.item(i)
                if item.data(Qt.ItemDataRole.UserRole) in layers_to_remove:
                    items_to_remove.append(item)
                    
            for item in items_to_remove:
                row = self.lst_old_layers.row(item)
                self.lst_old_layers.takeItem(row)

            if hasattr(self, 'old_overlay_layers'):
                self.old_overlay_layers = [] 
                
            project.removeMapLayers(layers_to_remove)
            self.refresh_old_canvas_layers()
    
    def clear_old_overlays(self):
        """Aktualizujemy czyszczenie, żeby opróżniało też listę UI."""
        self.remove_plugin_layers_by_prefix("QMapAnalyst -")
        self.lst_old_layers.clear()
        if hasattr(self, 'old_overlay_layers'):
            self.old_overlay_layers.clear()
        self.refresh_old_canvas_layers()
        self.lbl_status.setText("Wyczyszczono wszystkie wygenerowane warstwy z projektu.")

    def load_old_raster(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Wybierz skan mapy dawnej", "", 
            "Obrazy (*.tif *.tiff *.jpg *.jpeg *.png);;Wszystkie pliki (*.*)"
        )
        if not file_path:
            return

        layer = QgsRasterLayer(file_path, "Mapa dawna")
        if not layer.isValid():
            self.lbl_status.setText("BŁĄD: Nie udało się wczytać pliku rastrowego!")
            return

        self.old_raster_layer = layer
        self.old_overlay_layers.clear()
        self.old_canvas.setDestinationCrs(layer.crs())
        self.refresh_old_canvas_layers()
        self.old_canvas.setExtent(layer.extent())
        self.lbl_status.setText("Skan wczytany. Możesz dodawać lub wczytać punkty GCP.")

    def activate_pan_old(self):
        self.btn_add_gcp.setChecked(False)
        self.old_canvas.setMapTool(self.tool_pan_old)
        self.lbl_status.setText("Tryb przesuwania mapy dawnej.")

    def zoom_full_old(self):
        if self.old_raster_layer:
            self.old_canvas.setExtent(self.old_raster_layer.extent())
            self.old_canvas.refresh()
    def forward_transform_xy(self, x: float, y: float):
        """Szybka globalna transformacja pikseli na współrzędne mapy dla nawigacji (bez RBF)."""
        res = self.last_results
        if "Helmert" in res["type"]:
            p = res["params"]
            tx, ty, a, b = p["tx"], p["ty"], p["a"], p["b"]
            gx = tx + a * x - b * y
            gy = ty + b * x + a * y
        else:
            p = res["params"]
            tx, ty, a, b, c, d = p["tx"], p["ty"], p["a"], p["b"], p["c"], p["d"]
            gx = tx + a * x + b * y
            gy = ty + c * x + d * y
        return gx, gy

    def sync_main_canvas_to_old(self):
        """Synchronizuje widok mapy głównej na podstawie aktualnego obszaru mapy dawnej."""
        if not self.btn_sync_views.isChecked() or not self.last_results:
            return

        # Pobieramy obecny widok mapy dawnej (w pikselach)
        ext = self.old_canvas.extent()
        
        # Ekstrahujemy 4 narożniki ekranu
        pts = [
            (ext.xMinimum(), ext.yMinimum()),
            (ext.xMaximum(), ext.yMinimum()),
            (ext.xMaximum(), ext.yMaximum()),
            (ext.xMinimum(), ext.yMaximum())
        ]

        # Przekształcamy je na współrzędne referencyjne (współczesne metry)
        trans_pts = [self.forward_transform_xy(x, y) for x, y in pts]
        
        x_coords = [p[0] for p in trans_pts]
        y_coords = [p[1] for p in trans_pts]
        
        # Tworzymy nowy zakres graniczny (Bounding Box) z przekształconych narożników
        from qgis.core import QgsRectangle
        new_ext = QgsRectangle(min(x_coords), min(y_coords), max(x_coords), max(y_coords))
        
        # Ustawiamy widok w głównej mapie i odświeżamy
        self.main_canvas.setExtent(new_ext)
        self.main_canvas.refresh()
    
    def start_gcp_capture(self, checked):
        if checked:
            if not self.old_raster_layer:
                self.lbl_status.setText("Najpierw wczytaj skan mapy dawnej!")
                self.btn_add_gcp.setChecked(False)
                return
            self.btn_pan_old.setChecked(False)
            self.old_canvas.setMapTool(self.tool_click_old)
            self.lbl_status.setText("KROK 1/2: Kliknij punkt na MAPIE DAWNEJ (w panelu)...")
        else:
            self.old_canvas.setMapTool(self.tool_pan_old)
            if self.temp_old_marker:
                self.old_canvas.scene().removeItem(self.temp_old_marker)
                self.temp_old_marker = None
            self.lbl_status.setText("Anulowano dodawanie punktu.")

    def on_old_canvas_clicked(self, point: QgsPointXY, button):
        if button != Qt.MouseButton.LeftButton:
            return

        self.temp_old_point = point
        if self.temp_old_marker:
            self.old_canvas.scene().removeItem(self.temp_old_marker)
        self.temp_old_marker = self.create_marker(self.old_canvas, point, QColor(255, 0, 0))

        self.old_canvas.setMapTool(self.tool_pan_old)
        self.previous_main_tool = self.main_canvas.mapTool()
        self.main_canvas.setMapTool(self.tool_click_ref)
        self.lbl_status.setText("KROK 2/2: Teraz kliknij odpowiadający punkt na GŁÓWNEJ MAPIE...")

    def on_ref_canvas_clicked(self, point: QgsPointXY, button):
        if button != Qt.MouseButton.LeftButton or not self.temp_old_point:
            return

        if hasattr(self, 'previous_main_tool') and self.previous_main_tool:
            self.main_canvas.setMapTool(self.previous_main_tool)
        else:
            self.main_canvas.unsetMapTool(self.tool_click_ref)

        self.add_gcp_row(self.temp_old_point, point, enabled=True)

        if self.temp_old_marker:
            self.old_canvas.scene().removeItem(self.temp_old_marker)
            self.temp_old_marker = None
        self.temp_old_point = None

        self.btn_add_gcp.setChecked(False)
        self.lbl_status.setText(f"Dodano parę GCP #{self.tbl_gcp.rowCount()}. Gotowy.")

    def add_gcp_row(self, old_pt: QgsPointXY, ref_pt: QgsPointXY, enabled: bool = True):
        self._updating_table = True
        row = self.tbl_gcp.rowCount()
        self.tbl_gcp.insertRow(row)

        chk_item = QTableWidgetItem()
        chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk_item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        self.tbl_gcp.setItem(row, 0, chk_item)

        self.tbl_gcp.setItem(row, 1, QTableWidgetItem(f"{old_pt.x():.2f}"))
        self.tbl_gcp.setItem(row, 2, QTableWidgetItem(f"{old_pt.y():.2f}"))
        self.tbl_gcp.setItem(row, 3, QTableWidgetItem(f"{ref_pt.x():.2f}"))
        self.tbl_gcp.setItem(row, 4, QTableWidgetItem(f"{ref_pt.y():.2f}"))
        self.tbl_gcp.setItem(row, 5, QTableWidgetItem("-" if enabled else "Wył."))

        old_color = QColor(255, 0, 0) if enabled else QColor(150, 150, 150)
        ref_color = QColor(0, 180, 0) if enabled else QColor(150, 150, 150)

        old_marker = self.create_marker(self.old_canvas, old_pt, old_color)
        self.old_markers.append(old_marker)

        ref_marker = self.create_marker(self.main_canvas, ref_pt, ref_color)
        self.ref_markers.append(ref_marker)
        self._updating_table = False

    def on_table_item_changed(self, item: QTableWidgetItem):
        if self._updating_table or item.column() != 0:
            return

        row = item.row()
        is_checked = (item.checkState() == Qt.CheckState.Checked)

        if row < len(self.old_markers):
            self.old_markers[row].setColor(QColor(255, 0, 0) if is_checked else QColor(150, 150, 150))
            self.old_canvas.refresh()
        if row < len(self.ref_markers):
            self.ref_markers[row].setColor(QColor(0, 180, 0) if is_checked else QColor(150, 150, 150))
            self.main_canvas.refresh()

        if self.last_results is not None:
            self.run_analysis()

    def create_marker(self, canvas, point: QgsPointXY, color: QColor):
        marker = QgsVertexMarker(canvas)
        marker.setCenter(point)
        marker.setColor(color)
        marker.setIconSize(10)
        marker.setIconType(QgsVertexMarker.IconType.ICON_CROSS)
        marker.setPenWidth(2)
        return marker

    def remove_selected_gcp(self):
        selected_rows = sorted(set(index.row() for index in self.tbl_gcp.selectedIndexes()), reverse=True)
        for row in selected_rows:
            self.tbl_gcp.removeRow(row)
            if row < len(self.old_markers):
                self.old_canvas.scene().removeItem(self.old_markers.pop(row))
            if row < len(self.ref_markers):
                self.main_canvas.scene().removeItem(self.ref_markers.pop(row))
        self.lbl_status.setText("Usunięto zaznaczone punkty.")

    def clear_all_gcp(self):
        self.tbl_gcp.setRowCount(0)
        for m in self.old_markers:
            self.old_canvas.scene().removeItem(m)
        for m in self.ref_markers:
            self.main_canvas.scene().removeItem(m)
        self.old_markers.clear()
        self.ref_markers.clear()

    def is_row_enabled(self, row: int) -> bool:
        item = self.tbl_gcp.item(row, 0)
        return item is not None and item.checkState() == Qt.CheckState.Checked

    def save_gcp_to_file(self):
        if self.tbl_gcp.rowCount() == 0:
            self.lbl_status.setText("Brak punktów do zapisania!")
            return

        file_path, selected_filter = QFileDialog.getSaveFileName(
            self, "Zapisz punkty GCP", "",
            "Plik CSV (*.csv);;Punkty Georeferencera QGIS (*.points)"
        )
        if not file_path:
            return

        try:
            with open(file_path, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if file_path.endswith(".points") or "points" in selected_filter:
                    crs_authid = self.main_canvas.mapSettings().destinationCrs().authid()
                    f.write(f"#CRS: {crs_authid}\n")
                    writer.writerow(["mapX", "mapY", "sourceX", "sourceY", "enable", "dX", "dY", "residual"])
                    for row in range(self.tbl_gcp.rowCount()):
                        en = 1 if self.is_row_enabled(row) else 0
                        x_old = float(self.tbl_gcp.item(row, 1).text())
                        y_old = float(self.tbl_gcp.item(row, 2).text())
                        x_ref = float(self.tbl_gcp.item(row, 3).text())
                        y_ref = float(self.tbl_gcp.item(row, 4).text())
                        writer.writerow([x_ref, y_ref, x_old, y_old, en, 0, 0, 0])
                else:
                    writer.writerow(["x_old", "y_old", "x_ref", "y_ref", "enable"])
                    for row in range(self.tbl_gcp.rowCount()):
                        en = 1 if self.is_row_enabled(row) else 0
                        writer.writerow([
                            self.tbl_gcp.item(row, 1).text(),
                            self.tbl_gcp.item(row, 2).text(),
                            self.tbl_gcp.item(row, 3).text(),
                            self.tbl_gcp.item(row, 4).text(),
                            en
                        ])
            self.lbl_status.setText(f"Zapisano {self.tbl_gcp.rowCount()} pkt do pliku.")
        except Exception as e:
            self.lbl_status.setText(f"Błąd zapisu: {e}")

    def load_gcp_from_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Wczytaj punkty GCP", "",
            "Pliki GCP (*.csv *.points);;Wszystkie pliki (*.*)"
        )
        if not file_path:
            return

        try:
            self.clear_all_gcp()
            with open(file_path, mode='r', encoding='utf-8') as f:
                lines = [line for line in f if not line.startswith('#') and line.strip()]
                reader = csv.reader(lines)
                header = [h.strip() for h in next(reader)]

                is_qgis_points = "mapX" in header and "sourceX" in header
                has_enable = "enable" in header

                for row in reader:
                    if not row:
                        continue
                    enabled = True
                    if has_enable:
                        idx_en = header.index("enable")
                        if idx_en < len(row):
                            enabled = str(row[idx_en]).strip() not in ("0", "false", "False")

                    if is_qgis_points:
                        idx_map_x = header.index("mapX")
                        idx_map_y = header.index("mapY")
                        idx_src_x = header.index("sourceX")
                        idx_src_y = header.index("sourceY")
                        ref_pt = QgsPointXY(float(row[idx_map_x]), float(row[idx_map_y]))
                        old_pt = QgsPointXY(float(row[idx_src_x]), float(row[idx_src_y]))
                    else:
                        old_pt = QgsPointXY(float(row[0]), float(row[1]))
                        ref_pt = QgsPointXY(float(row[2]), float(row[3]))

                    self.add_gcp_row(old_pt, ref_pt, enabled=enabled)

            self.lbl_status.setText(f"Wczytano {self.tbl_gcp.rowCount()} punktów GCP.")
        except Exception as e:
            self.lbl_status.setText(f"Błąd odczytu pliku: {e}")

    def get_active_gcp_data(self):
        active_old = []
        active_ref = []
        active_rows = []

        for r in range(self.tbl_gcp.rowCount()):
            if self.is_row_enabled(r):
                active_old.append([
                    float(self.tbl_gcp.item(r, 1).text()),
                    float(self.tbl_gcp.item(r, 2).text())
                ])
                active_ref.append([
                    float(self.tbl_gcp.item(r, 3).text()),
                    float(self.tbl_gcp.item(r, 4).text())
                ])
                active_rows.append(r)

        if not active_rows:
            return np.zeros((0, 2)), np.zeros((0, 2)), []

        return np.array(active_old, dtype=float), np.array(active_ref, dtype=float), active_rows

    def get_gcp_arrays(self):
        pts_old, pts_ref, _ = self.get_active_gcp_data()
        return pts_old, pts_ref

    def run_analysis(self):
        pts_old, pts_ref, active_rows = self.get_active_gcp_data()
        active_count = len(active_rows)
        total_count = self.tbl_gcp.rowCount()

        is_helmert = self.cmb_transform_type.currentIndex() == 0
        min_pts = 2 if is_helmert else 3

        if active_count < min_pts:
            self.txt_report.setText(
                f"BŁĄD: Wybrana transformacja wymaga minimum {min_pts} aktywnych par punktów "
                f"(obecnie aktywnych: {active_count} z {total_count})!"
            )
            return

        try:
            if is_helmert:
                res = MathEngine.helmert_transform(pts_old, pts_ref)
            else:
                res = MathEngine.affine_transform(pts_old, pts_ref)

            self.last_results = res
            self.btn_sync_views.setEnabled(True)

            span = float(np.max(np.ptp(pts_ref, axis=0)))
            if span > 0:
                max_slider = max(1000, int(span))
                min_slider = max(50, int(span * 0.02))
                self.sld_confidence.setRange(min_slider, max_slider)
                if self.sld_confidence.value() < min_slider or self.sld_confidence.value() > max_slider:
                    self.sld_confidence.setValue(int(span * 0.35))

            max_err = res["max_error"]
            err_map = {active_rows[i]: res["errors"][i] for i in range(active_count)}

            self._updating_table = True
            for r in range(total_count):
                if r in err_map:
                    err = err_map[r]
                    item = QTableWidgetItem(f"{err:.2f}")
                    if err == max_err and active_count > min_pts:
                        item.setForeground(QColor(200, 0, 0))
                    self.tbl_gcp.setItem(r, 5, item)
                else:
                    item = QTableWidgetItem("Wył.")
                    item.setForeground(QColor(150, 150, 150))
                    self.tbl_gcp.setItem(r, 5, item)
            self._updating_table = False

            mean_scale_m_px = (res["scale_x"] + res["scale_y"]) / 2.0
            m_150 = int(round(mean_scale_m_px * 150.0 / 0.0254, -2))
            m_300 = int(round(mean_scale_m_px * 300.0 / 0.0254, -2))
            m_600 = int(round(mean_scale_m_px * 600.0 / 0.0254, -2))

            crs_authid = self.main_canvas.mapSettings().destinationCrs().authid()
            report = [
                f"=== {res['type']} ===",
                f"Aktywne punkty GCP: {active_count} / {total_count} (Układ: {crs_authid})",
                f"RMSE (Błąd średniokwadratowy): {res['rmse']:.2f} m",
                f"Błąd Min / Max: {res['min_error']:.2f} / {res['max_error']:.2f} m",
                f"Kąt obrotu: {res['rotation_deg']:.3f}°",
            ]
            if is_helmert:
                report.append(f"Skala pikselowa: {res['scale_x']:.4f} [m / px]")
            else:
                report.append(f"Skala X / Y: {res['scale_x']:.4f} / {res['scale_y']:.4f} [m / px]")
                report.append(f"Skos osi (Shear): {res['shear_deg']:.3f}°")

            report.extend([
                "",
                "=== SZACOWANA SKALA MIANOWANA (PAPIEROWA) ===",
                f"• dla 150 DPI:   1 : {m_150:,}".replace(",", " "),
                f"• dla 300 DPI:   1 : {m_300:,}".replace(",", " "),
                f"• dla 600 DPI:   1 : {m_600:,}".replace(",", " "),
            ])

            self.txt_report.setText("\n".join(report))
            self.lbl_status.setText(f"Obliczono ({active_count} pkt)! RMSE: {res['rmse']:.1f}m | Skala (300 DPI): ~1:{m_300}")

            project = QgsProject.instance()
            existing_names = [lyr.name() for lyr in project.mapLayers().values()]

            if any(n.startswith("QMapAnalyst - Wektory") for n in existing_names):
                self.generate_result_layers()
            if any(n.startswith("QMapAnalyst - Okręgi") for n in existing_names):
                self.generate_error_circles()
            if any(n.startswith("QMapAnalyst - Siatka (") for n in existing_names):
                self.generate_distortion_grid()
            if any(n.startswith("QMapAnalyst - Siatka PUWG92") for n in existing_names):
                self.generate_historic_grid()
            if any(n.startswith("QMapAnalyst - Izolinie") for n in existing_names):
                self.generate_isolines()

        except Exception as e:
            self._updating_table = False
            self.txt_report.setText(f"Błąd obliczeń: {e}")
            self.lbl_status.setText(f"Obliczono ({active_count} pkt)! RMSE: {res['rmse']:.1f}m | Skala (300 DPI): ~1:{m_300}")

        except Exception as e:
            self._updating_table = False
            self.txt_report.setText(f"Błąd obliczeń: {e}")

    def get_confidence_mask(self, pts: np.ndarray, radius: float) -> QgsGeometry:
        buffers = [
            QgsGeometry.fromPointXY(QgsPointXY(float(p[0]), float(p[1]))).buffer(radius, 24)
            for p in pts
        ]
        unioned = QgsGeometry.unaryUnion(buffers)
        if unioned.isEmpty():
            return unioned

        hulls = []
        if unioned.isMultipart():
            multi_poly = unioned.asMultiPolygon()
            for poly in multi_poly:
                part_geom = QgsGeometry.fromPolygonXY(poly)
                hulls.append(part_geom.convexHull())
        else:
            hulls.append(unioned.convexHull())

        mask = QgsGeometry.unaryUnion(hulls)
        return mask if not mask.isEmpty() else unioned

    def extract_lines_from_geom(self, geom: QgsGeometry) -> list:
        if geom.isEmpty():
            return []
        lines = []
        parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
        for part in parts:
            if not part.isEmpty() and part.type() == QgsWkbTypes.GeometryType.LineGeometry:
                lines.append(part)
        return lines

    def style_line_layer(self, layer: QgsVectorLayer, color: QColor, width_mm: float = 1.0, dashed: bool = False):
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        symbol.setColor(color)
        symbol.setWidth(width_mm)
        sl = symbol.symbolLayer(0)
        if sl:
            sl.setColor(color)
            sl.setWidth(width_mm)
            if dashed and hasattr(sl, 'setPenStyle'):
                sl.setPenStyle(Qt.PenStyle.DashLine)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()

    def generate_result_layers(self):
        if not self.last_results:
            self.lbl_status.setText("Najpierw wykonaj obliczenia!")
            return

        # Ciche czyszczenie starych wektorów przed wygenerowaniem nowych
        self.remove_plugin_layers_by_prefix("QMapAnalyst - Wektory")

        res = self.last_results
        crs_authid = self.main_canvas.mapSettings().destinationCrs().authid()
        
        vec_layer = QgsVectorLayer(f"LineString?crs={crs_authid}", "QMapAnalyst - Wektory przesunięć", "memory")
        vec_provider = vec_layer.dataProvider()
        
        vec_provider.addAttributes([
            QgsField("id", QMetaType.Type.Int),
            QgsField("error", QMetaType.Type.Double)
        ])
        vec_layer.updateFields()

        _, pts_ref, active_rows = self.get_active_gcp_data()
        pts_trans = res["pts_transformed"]
        errors = res["errors"]

        features = []
        for i in range(len(pts_ref)):
            feat = QgsFeature()
            pt_start = QgsPointXY(pts_trans[i, 0], pts_trans[i, 1])
            pt_end = QgsPointXY(pts_ref[i, 0], pts_ref[i, 1])
            feat.setGeometry(QgsGeometry.fromPolylineXY([pt_start, pt_end]))
            feat.setAttributes([active_rows[i] + 1, float(errors[i])])
            features.append(feat)

        vec_provider.addFeatures(features)
        vec_layer.updateExtents()
        self.style_line_layer(vec_layer, QColor(220, 30, 30), width_mm=1.0, dashed=False)

        QgsProject.instance().addMapLayer(vec_layer)
        self.lbl_status.setText("Wygenerowano warstwę wektorów przesunięć!")

    def generate_error_circles(self):
        if not self.last_results:
            self.lbl_status.setText("Najpierw wykonaj obliczenia!")
            return

        # Ciche czyszczenie starych okręgów przed wygenerowaniem nowych
        self.remove_plugin_layers_by_prefix("QMapAnalyst - Okręgi")

        res = self.last_results
        crs_authid = self.main_canvas.mapSettings().destinationCrs().authid()
        
        circle_layer = QgsVectorLayer(f"Polygon?crs={crs_authid}", "QMapAnalyst - Okręgi błędów", "memory")
        circle_provider = circle_layer.dataProvider()
        
        circle_provider.addAttributes([
            QgsField("id", QMetaType.Type.Int),
            QgsField("error", QMetaType.Type.Double)
        ])
        circle_layer.updateFields()

        _, pts_ref, active_rows = self.get_active_gcp_data()
        errors = res["errors"]

        features = []
        for i in range(len(pts_ref)):
            feat = QgsFeature()
            center = QgsPointXY(pts_ref[i, 0], pts_ref[i, 1])
            radius = float(errors[i])
            geom = QgsGeometry.fromPointXY(center).buffer(radius, 36)
            feat.setGeometry(geom)
            feat.setAttributes([active_rows[i] + 1, radius])
            features.append(feat)

        circle_provider.addFeatures(features)
        circle_layer.updateExtents()

        symbol = QgsSymbol.defaultSymbol(circle_layer.geometryType())
        sl = symbol.symbolLayer(0)
        if sl:
            sl.setFillColor(QColor(0, 0, 0, 0))
            if hasattr(sl, 'setBrushStyle'):
                sl.setBrushStyle(Qt.BrushStyle.NoBrush)
            sl.setStrokeColor(QColor(220, 50, 50))
            sl.setStrokeWidth(1.0)
        circle_layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        circle_layer.triggerRepaint()

        QgsProject.instance().addMapLayer(circle_layer)
        self.lbl_status.setText("Wygenerowano warstwę okręgów błędów (obrys 1mm).")

    def generate_distortion_grid(self):
        if not self.last_results:
            self.lbl_status.setText("Najpierw wykonaj obliczenia!")
            return

        # Ciche czyszczenie starych siatek RBF
        self.remove_plugin_layers_by_prefix("QMapAnalyst - Siatka (")

        res = self.last_results
        crs_authid = self.main_canvas.mapSettings().destinationCrs().authid()
        
        pts_old, pts_ref = self.get_gcp_arrays()
        residuals = res["residuals"]

        if len(pts_ref) < 3:
            self.lbl_status.setText("Siatka zniekształceń wymaga minimum 3 aktywnych punktów!")
            return

        radius_m = float(self.sld_confidence.value())
        mask_geom = self.get_confidence_mask(pts_ref, radius_m)

        reliable_layer = QgsVectorLayer(f"LineString?crs={crs_authid}", "QMapAnalyst - Siatka (Obszar pewny)", "memory")
        uncertain_layer = QgsVectorLayer(f"LineString?crs={crs_authid}", "QMapAnalyst - Siatka (Ekstrapolacja)", "memory")
        
        rel_prov = reliable_layer.dataProvider()
        unc_prov = uncertain_layer.dataProvider()
        
        rel_prov.addAttributes([QgsField("type", QMetaType.Type.QString)])
        unc_prov.addAttributes([QgsField("type", QMetaType.Type.QString)])
        reliable_layer.updateFields()
        uncertain_layer.updateFields()

        old_min_x, old_min_y = np.min(pts_old, axis=0)
        old_max_x, old_max_y = np.max(pts_old, axis=0)
        
        old_w = old_max_x - old_min_x
        old_h = old_max_y - old_min_y
        
        grid_step_m = float(self.spin_grid_step.value())
        mean_scale = (res["scale_x"] + res["scale_y"]) / 2.0
        step_px = grid_step_m / mean_scale if mean_scale > 0 else 100
        
        cols = max(1, int(old_w / step_px))
        rows = max(1, int(old_h / step_px))

        odx = old_w / cols if cols > 0 else 1
        ody = old_h / rows if rows > 0 else 1

        def transform_point_global(x, y):
            if "Helmert" in res["type"]:
                p = res["params"]
                tx, ty, a, b = p["tx"], p["ty"], p["a"], p["b"]
                return tx + a * x - b * y, ty + b * x + a * y
            else:
                p = res["params"]
                tx, ty, a, b, c, d = p["tx"], p["ty"], p["a"], p["b"], p["c"], p["d"]
                return tx + a * x + b * y, ty + c * x + d * y

        grid_nodes_old = []
        node_indices = []

        for r in range(rows + 1):
            for c in range(cols + 1):
                x_val = old_min_x + c * odx
                y_val = old_min_y + r * ody
                grid_nodes_old.append([x_val, y_val])
                node_indices.append((r, c))

        grid_nodes_old = np.array(grid_nodes_old)
        global_trans = np.array([transform_point_global(pt[0], pt[1]) for pt in grid_nodes_old])
        local_deformations = MathEngine.calculate_local_grid_deformation(pts_old, residuals, grid_nodes_old)
        final_nodes = global_trans + local_deformations

        node_dict = {node_indices[i]: QgsPointXY(final_nodes[i, 0], final_nodes[i, 1]) for i in range(len(node_indices))}

        rel_features = []
        unc_features = []

        def process_line(line_points, line_type):
            if len(line_points) < 2:
                return
            line_geom = QgsGeometry.fromPolylineXY(line_points)
            
            inside_geom = line_geom.intersection(mask_geom)
            for geom in self.extract_lines_from_geom(inside_geom):
                feat = QgsFeature()
                feat.setGeometry(geom)
                feat.setAttributes([f"{line_type}_reliable"])
                rel_features.append(feat)

            outside_geom = line_geom.difference(mask_geom)
            for geom in self.extract_lines_from_geom(outside_geom):
                feat = QgsFeature()
                feat.setGeometry(geom)
                feat.setAttributes([f"{line_type}_uncertain"])
                unc_features.append(feat)

        for r in range(rows + 1):
            line_points = [node_dict[(r, c)] for c in range(cols + 1) if (r, c) in node_dict]
            process_line(line_points, "horizontal")

        for c in range(cols + 1):
            line_points = [node_dict[(r, c)] for r in range(rows + 1) if (r, c) in node_dict]
            process_line(line_points, "vertical")

        if rel_features:
            rel_prov.addFeatures(rel_features)
            reliable_layer.updateExtents()
            self.style_line_layer(reliable_layer, QColor(100, 50, 200), width_mm=1.0, dashed=False)
            QgsProject.instance().addMapLayer(reliable_layer)

        if unc_features:
            unc_prov.addFeatures(unc_features)
            uncertain_layer.updateExtents()
            self.style_line_layer(uncertain_layer, QColor(130, 130, 130), width_mm=1.0, dashed=True)
            QgsProject.instance().addMapLayer(uncertain_layer)

        self.lbl_status.setText(f"Siatka wygenerowana! Pewna: {len(rel_features)} odc., Ekstrapolacja: {len(unc_features)} odc.")

    def generate_historic_grid(self):
        if not self.last_results or not self.old_raster_layer:
            self.lbl_status.setText("Najpierw wykonaj obliczenia i wczytaj skan dawnej mapy!")
            return

        pts_old, pts_ref = self.get_gcp_arrays()
        if len(pts_ref) < 3:
            self.lbl_status.setText("Generowanie siatki historycznej wymaga minimum 3 aktywnych punktów!")
            return

        # Ciche czyszczenie starej siatki dawnej
        self.remove_plugin_layers_by_prefix("QMapAnalyst - Siatka PUWG92")

        min_x, min_y = np.min(pts_ref, axis=0)
        max_x, max_y = np.max(pts_ref, axis=0)

        grid_step = float(self.spin_grid_step.value())

        start_x = np.floor(min_x / grid_step) * grid_step
        end_x = np.ceil(max_x / grid_step) * grid_step
        start_y = np.floor(min_y / grid_step) * grid_step
        end_y = np.ceil(max_y / grid_step) * grid_step

        try:
            from scipy.interpolate import RBFInterpolator
            rbf_back_x = RBFInterpolator(pts_ref, pts_old[:, 0], kernel='thin_plate_spline')
            rbf_back_y = RBFInterpolator(pts_ref, pts_old[:, 1], kernel='thin_plate_spline')
        except Exception as e:
            self.lbl_status.setText(f"Błąd inicjalizacji RBF wstecznego: {e}")
            return

        def inverse_transform(rx, ry):
            pt_arr = np.array([[rx, ry]])
            return float(rbf_back_x(pt_arr)[0]), float(rbf_back_y(pt_arr)[0])

        historic_grid_layer = self.create_old_canvas_vector_layer(
            "QMapAnalyst - Siatka PUWG92 na mapie dawnej",
            [QgsField("grid_id", QMetaType.Type.QString)]
        )
        HG_prov = historic_grid_layer.dataProvider()

        features = []
        x_coords = np.arange(start_x, end_x + grid_step, grid_step)
        y_dense = np.linspace(start_y, end_y, 40)

        for x in x_coords:
            line_pts = [QgsPointXY(*inverse_transform(x, y)) for y in y_dense]
            if len(line_pts) > 1:
                feat = QgsFeature()
                feat.setGeometry(QgsGeometry.fromPolylineXY(line_pts))
                feat.setAttributes([f"E_{int(x)}"])
                features.append(feat)

        y_coords = np.arange(start_y, end_y + grid_step, grid_step)
        x_dense = np.linspace(start_x, end_x, 40)

        for y in y_coords:
            line_pts = [QgsPointXY(*inverse_transform(x, y)) for x in x_dense]
            if len(line_pts) > 1:
                feat = QgsFeature()
                feat.setGeometry(QgsGeometry.fromPolylineXY(line_pts))
                feat.setAttributes([f"N_{int(y)}"])
                features.append(feat)

        if features:
            HG_prov.addFeatures(features)
            historic_grid_layer.updateExtents()
            self.style_line_layer(historic_grid_layer, QColor(138, 43, 226), width_mm=0.5, dashed=False)

            QgsProject.instance().addMapLayer(historic_grid_layer)
            self.add_layer_to_old_canvas(historic_grid_layer)
            self.refresh_old_canvas_layers()

            self.lbl_status.setText(f"Wygenerowano siatkę PUWG92 na mapie dawnej ({len(features)} linii).")

    def generate_isolines(self, type="scale"):
        if not self.last_results or not self.old_raster_layer:
            self.lbl_status.setText("Najpierw wykonaj obliczenia i wczytaj skan dawnej mapy!")
            return

        pts_old, pts_ref = self.get_gcp_arrays()
        if len(pts_old) < 4:
            self.lbl_status.setText("Izolinie wymagają minimum 4 aktywnych punktów kontrolnych!")
            return

        X_old, Y_old, _, _, scale_grid, rot_grid = MathEngine.calculate_local_scale_and_rotation_grids(
            pts_old, self.last_results, nx=45, ny=45
        )

        dpi_text = self.cmb_dpi.currentText()
        dpi_val = float(dpi_text.split()[0])

        # Rozdzielona logika w zależności od wciśniętego przycisku
        if type == "scale":
            self.remove_plugin_layers_by_prefix("QMapAnalyst - Izolinie skali")
            scale_denom_grid = scale_grid * dpi_val / 0.0254
            n_lines = self.build_isoline_layer(
                scale_denom_grid,
                f"QMapAnalyst - Izolinie skali (1:M dla {int(dpi_val)} DPI)",
                "scale_M",
                QColor(0, 150, 136),
                is_scale_layer=True
            )
            self.lbl_status.setText(f"Izolinie skali gotowe ({n_lines} odcinków).")
            
        elif type == "rot":
            self.remove_plugin_layers_by_prefix("QMapAnalyst - Izolinie rotacji")
            n_lines = self.build_isoline_layer(
                rot_grid,
                "QMapAnalyst - Izolinie rotacji [deg]",
                "rot_deg",
                QColor(230, 126, 34),
                is_scale_layer=False
            )
            self.lbl_status.setText(f"Izolinie rotacji gotowe ({n_lines} odcinków).")


    def build_isoline_layer(self, Z_grid, layer_name, attr_name, color: QColor, is_scale_layer: bool):
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            return 0

        X_old, Y_old, _, _, _, _ = MathEngine.calculate_local_scale_and_rotation_grids(
            self.get_gcp_arrays()[0], self.last_results, nx=45, ny=45
        )

        layer = self.create_old_canvas_vector_layer(
            layer_name,
            [QgsField(attr_name, QMetaType.Type.Double)]
        )
        prov = layer.dataProvider()

        fig, ax = plt.subplots()
        cs = ax.contour(X_old, Y_old, Z_grid, levels=8)
        plt.close(fig)

        pts_old, pts_ref = self.get_gcp_arrays()
        mean_scale = (self.last_results["scale_x"] + self.last_results["scale_y"]) / 2.0
        radius_px = float(self.sld_confidence.value()) / mean_scale if mean_scale > 0 else 200.0
        mask_old = self.get_confidence_mask(pts_old, radius_px)

        feats = []
        for level_idx, level_val in enumerate(cs.levels):
            segs = cs.allsegs[level_idx]
            for seg in segs:
                if len(seg) < 2:
                    continue
                pts = [QgsPointXY(float(pt[0]), float(pt[1])) for pt in seg]
                geom = QgsGeometry.fromPolylineXY(pts)
                
                clipped = geom.intersection(mask_old)
                for part in self.extract_lines_from_geom(clipped):
                    f = QgsFeature()
                    f.setGeometry(part)
                    f.setAttributes([round(float(level_val), 2)])
                    feats.append(f)

        if feats:
            prov.addFeatures(feats)
            layer.updateExtents()
            self.style_line_layer(layer, color, width_mm=0.8, dashed=False)

            from qgis.core import (
                QgsPalLayerSettings, QgsVectorLayerSimpleLabeling,
                QgsTextFormat, QgsTextBufferSettings, Qgis
            )
            
            setting = QgsPalLayerSettings()
            setting.enabled = True
            setting.isExpression = True 
            
            if is_scale_layer:
                setting.fieldName = "concat('1 : ', format_number(\"scale_M\", 0))"
            else:
                setting.fieldName = "concat(format_number(\"rot_deg\", 1), '°')"

            text_format = QgsTextFormat()
            text_format.setSize(9)
            text_format.setColor(color)
            
            buffer_settings = QgsTextBufferSettings()
            buffer_settings.setEnabled(True)
            buffer_settings.setSize(1.0)
            buffer_settings.setColor(QColor(255, 255, 255, 220))
            text_format.setBuffer(buffer_settings)
            
            setting.setFormat(text_format)
            # Nowy, bezpieczny sposób na ustawienie etykiety na linii:
            setting.placement = Qgis.LabelPlacement.Line 
            setting.dist = 1.5 

            layer.setLabeling(QgsVectorLayerSimpleLabeling(setting))
            layer.setLabelsEnabled(True)

            QgsProject.instance().addMapLayer(layer)
            self.add_layer_to_old_canvas(layer)

        return len(feats)
    
    def generate_old_vectors(self):
        if not self.last_results:
            return
        self.remove_plugin_layers_by_prefix("QMapAnalyst - Dawne Wektory")
        
        pts_old, pts_ref = self.get_gcp_arrays()
        pts_trans = self.last_results["pts_transformed"]
        errors = self.last_results["errors"]
        mean_scale = (self.last_results["scale_x"] + self.last_results["scale_y"]) / 2.0
        mag = self.get_magnification_factor()

        layer = self.create_old_canvas_vector_layer("QMapAnalyst - Dawne Wektory", [QgsField("error", QMetaType.Type.Double)])
        prov = layer.dataProvider()

        feats = []
        for i in range(len(pts_old)):
            # Wektor błędu w metrach (różnica między transformowanym old a ref)
            dx_m = pts_ref[i, 0] - pts_trans[i, 0]
            dy_m = pts_ref[i, 1] - pts_trans[i, 1]
            
            # Przeskalowanie błędu do przestrzeni pikselowej dawnej mapy
            dx_px = (dx_m / mean_scale) * mag if mean_scale > 0 else 0
            dy_px = (dy_m / mean_scale) * mag if mean_scale > 0 else 0
            
            pt_start = QgsPointXY(pts_old[i, 0], pts_old[i, 1])
            pt_end = QgsPointXY(pts_old[i, 0] + dx_px, pts_old[i, 1] + dy_px)
            
            feat = QgsFeature()
            feat.setGeometry(QgsGeometry.fromPolylineXY([pt_start, pt_end]))
            feat.setAttributes([float(errors[i])])
            feats.append(feat)

        if feats:
            prov.addFeatures(feats)
            self.style_line_layer(layer, QColor(220, 30, 30), width_mm=1.0)
            QgsProject.instance().addMapLayer(layer)
            self.add_layer_to_old_canvas(layer)

    def generate_old_circles(self):
        if not self.last_results:
            return
        self.remove_plugin_layers_by_prefix("QMapAnalyst - Dawne Okręgi")
        
        pts_old, _ = self.get_gcp_arrays()
        errors = self.last_results["errors"]
        mean_scale = (self.last_results["scale_x"] + self.last_results["scale_y"]) / 2.0
        mag = self.get_magnification_factor()

        layer = self.create_old_canvas_vector_layer(
            "QMapAnalyst - Dawne Okręgi", 
            [QgsField("error", QMetaType.Type.Double)],
            geom_type="Polygon"
        )
        prov = layer.dataProvider()

        feats = []
        for i in range(len(pts_old)):
            # Promień w pikselach
            radius_px = (float(errors[i]) / mean_scale) * mag if mean_scale > 0 else 10
            
            geom = QgsGeometry.fromPointXY(QgsPointXY(pts_old[i, 0], pts_old[i, 1])).buffer(radius_px, 36)
            feat = QgsFeature()
            feat.setGeometry(geom)
            feat.setAttributes([float(errors[i])])
            feats.append(feat)

        if feats:
            prov.addFeatures(feats)
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            sl = symbol.symbolLayer(0)
            sl.setFillColor(QColor(0, 0, 0, 0))
            sl.setStrokeColor(QColor(220, 50, 50))
            sl.setStrokeWidth(1.0)
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            
            QgsProject.instance().addMapLayer(layer)
            self.add_layer_to_old_canvas(layer)

    def get_magnification_factor(self) -> float:
        """Zwraca mnożnik skali dla wektorów i okręgów błędów. Zwraca 1.0, jeśli brak opcji w UI."""
        if hasattr(self, 'cmb_magnification'):
            text = self.cmb_magnification.currentText().replace("x", "")
            try:
                return float(text)
            except ValueError:
                return 1.0
        return 1.0
    
    def closeEvent(self, event):
        for marker in self.ref_markers:
            self.main_canvas.scene().removeItem(marker)
        super().closeEvent(event)