"""
Zarządzanie warstwami wynikowymi oraz ich stylizacja.

Warstwy identyfikowane są kluczem (np. "main.vectors", "old.isolines.scale"),
a nie nazwą – dzięki temu zmiana nazwy przez użytkownika nie psuje odświeżania,
a usunięcie warstwy z projektu jest bezpiecznie wykrywane.
"""

from qgis.PyQt.QtCore import QCoreApplication, QMetaType, QObject, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsProject, QgsVectorLayer, QgsField, QgsFeature, QgsGeometry, QgsPointXY,
    QgsSingleSymbolRenderer, QgsCategorizedSymbolRenderer, QgsRendererCategory,
    QgsPalLayerSettings, QgsVectorLayerSimpleLabeling, QgsTextFormat, QgsTextBufferSettings,
    QgsMarkerSymbol, QgsFillSymbol, QgsLineSymbol,
)

try:
    from qgis.core import QgsArrowSymbolLayer
except ImportError:  # pragma: no cover
    QgsArrowSymbolLayer = None

GROUP_NAME = "QMapAnalyst"


def tr(text):
    return QCoreApplication.translate("QMapAnalystLayers", text)


class LayerRegistry(QObject):
    """Rejestr warstw wygenerowanych przez wtyczkę."""

    oldLayersChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = QgsProject.instance()
        self._main = {}   # klucz -> id warstwy (mapa główna, w drzewie warstw)
        self._old = {}    # klucz -> id warstwy (mapa dawna, poza drzewem warstw)
        self.project.layersWillBeRemoved.connect(self._on_layers_removed)

    def disconnect_project(self):
        try:
            self.project.layersWillBeRemoved.disconnect(self._on_layers_removed)
        except (TypeError, RuntimeError):
            pass

    # ------------------------------------------------------------------
    def _on_layers_removed(self, ids):
        ids = set(ids)
        changed = False
        for store, is_old in ((self._main, False), (self._old, True)):
            for k in [k for k, v in store.items() if v in ids]:
                del store[k]
                changed |= is_old
        if changed:
            self.oldLayersChanged.emit()

    def _group(self):
        root = self.project.layerTreeRoot()
        grp = root.findGroup(GROUP_NAME)
        if grp is None:
            grp = root.insertGroup(0, GROUP_NAME)
        return grp

    def layer(self, key):
        lid = self._main.get(key) or self._old.get(key)
        return self.project.mapLayer(lid) if lid else None

    def has(self, key) -> bool:
        return self.layer(key) is not None

    def keys(self, prefix=""):
        return [k for k in list(self._main) + list(self._old) if k.startswith(prefix)]

    def set_main(self, key, layer):
        visible = self._replace(key, self._main)
        self.project.addMapLayer(layer, False)
        node = self._group().insertLayer(0, layer)
        if visible is not None and node is not None:
            node.setItemVisibilityChecked(visible)
        self._main[key] = layer.id()
        return layer

    def set_old(self, key, layer):
        self._replace(key, self._old)
        self.project.addMapLayer(layer, False)
        self._old[key] = layer.id()
        self.oldLayersChanged.emit()
        return layer

    def _replace(self, key, store):
        lid = store.pop(key, None)
        visible = None
        if lid and self.project.mapLayer(lid):
            node = self.project.layerTreeRoot().findLayer(lid)
            if node is not None:
                visible = node.isVisible()
            self.project.removeMapLayer(lid)
        return visible

    def remove(self, key):
        for store in (self._main, self._old):
            lid = store.pop(key, None)
            if lid and self.project.mapLayer(lid):
                self.project.removeMapLayer(lid)

    def clear_main(self):
        ids = [i for i in self._main.values() if self.project.mapLayer(i)]
        self._main.clear()
        if ids:
            self.project.removeMapLayers(ids)
        grp = self.project.layerTreeRoot().findGroup(GROUP_NAME)
        if grp is not None and not grp.children():
            grp.parent().removeChildNode(grp)

    def clear_old(self):
        ids = [i for i in self._old.values() if self.project.mapLayer(i)]
        self._old.clear()
        if ids:
            self.project.removeMapLayers(ids)
        self.oldLayersChanged.emit()

    def old_layers(self):
        """Lista (klucz, warstwa) warstw mapy dawnej w kolejności dodania."""
        out = []
        for k, lid in self._old.items():
            lyr = self.project.mapLayer(lid)
            if lyr is not None and lyr.isValid():
                out.append((k, lyr))
        return out


# ----------------------------------------------------------------------
#  Tworzenie warstw
# ----------------------------------------------------------------------

def _qtype(name):
    return {
        "int": QMetaType.Type.Int,
        "double": QMetaType.Type.Double,
        "string": QMetaType.Type.QString,
        "bool": QMetaType.Type.Bool,
    }[name]


def memory_layer(geom_type: str, crs, name: str, fields) -> QgsVectorLayer:
    """fields: lista (nazwa, typ) gdzie typ to int / double / string."""
    lyr = QgsVectorLayer(f"{geom_type}?index=yes", name, "memory")
    # Skan bez georeferencji ma nieokreślony CRS – warstwa musi go dziedziczyć (domyślny EPSG:4326
    # warstwy pamięciowej powodowałby przeliczanie nakładek przy renderowaniu poza kanwą).
    lyr.setCrs(crs if crs is not None else QgsCoordinateReferenceSystem())
    prov = lyr.dataProvider()
    prov.addAttributes([QgsField(n, _qtype(t)) for n, t in fields])
    lyr.updateFields()
    return lyr


def add_features(layer: QgsVectorLayer, items):
    """items: iterowalne (QgsGeometry, [atrybuty])."""
    feats = []
    fields = layer.fields()
    for geom, attrs in items:
        f = QgsFeature(fields)
        f.setGeometry(geom)
        f.setAttributes(list(attrs))
        feats.append(f)
    if feats:
        layer.dataProvider().addFeatures(feats)
        layer.updateExtents()
    return len(feats)


def polyline(xy) -> QgsGeometry:
    return QgsGeometry.fromPolylineXY([QgsPointXY(float(x), float(y)) for x, y in xy])


def merge_segments(segments) -> list:
    """Łączy odcinki (m, 2, 2) w możliwie długie linie (geometrie QGIS)."""
    if len(segments) == 0:
        return []
    multi = QgsGeometry.fromMultiPolylineXY(
        [[QgsPointXY(float(s[0, 0]), float(s[0, 1])), QgsPointXY(float(s[1, 0]), float(s[1, 1]))] for s in segments]
    )
    merged = multi.mergeLines()
    return split_lines(merged)


def split_lines(geom: QgsGeometry) -> list:
    if geom is None or geom.isEmpty():
        return []
    out = []
    parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
    for part in parts:
        if not part.isEmpty() and part.type() == Qgis.GeometryType.Line:
            out.append(part)
    return out


# ----------------------------------------------------------------------
#  Stylizacja
# ----------------------------------------------------------------------

def style_line(layer, color: QColor, width_mm=0.6, dashed=False):
    symbol = QgsLineSymbol.createSimple({
        "line_color": f"{color.red()},{color.green()},{color.blue()},{color.alpha()}",
        "line_width": str(width_mm),
        "line_style": "dash" if dashed else "solid",
        "capstyle": "round",
        "joinstyle": "round",
    })
    symbol.setColor(color)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))


def style_arrows(layer, color: QColor, width_mm=0.5):
    if QgsArrowSymbolLayer is None:
        return style_line(layer, color, width_mm)
    try:
        arrow = QgsArrowSymbolLayer.create({
            "arrow_width": str(width_mm), "arrow_start_width": str(width_mm),
            "head_length": str(width_mm * 4), "head_thickness": str(width_mm * 2.5),
            "is_curved": "0", "is_repeated": "0",
        })
        fill = QgsFillSymbol.createSimple({"color": color.name(), "outline_style": "no"})
        arrow.setSubSymbol(fill)
        symbol = QgsLineSymbol()
        symbol.changeSymbolLayer(0, arrow)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    except Exception:
        style_line(layer, color, width_mm)


def style_outline(layer, color: QColor, width_mm=0.5, field=None, alt_color=None):
    def sym(c):
        return QgsFillSymbol.createSimple({
            "color": "0,0,0,0", "style": "no", "outline_color": c.name(), "outline_width": str(width_mm)
        })
    if field and alt_color is not None:
        cats = [QgsRendererCategory(0, sym(color), tr("OK")),
                QgsRendererCategory(1, sym(alt_color), tr("Odstający"))]
        layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))
    else:
        layer.setRenderer(QgsSingleSymbolRenderer(sym(color)))


def style_points(layer, field="outlier"):
    def sym(c, shape="circle"):
        return QgsMarkerSymbol.createSimple({
            "name": shape, "color": c.name(), "outline_color": "255,255,255", "outline_width": "0.3", "size": "2.6"
        })
    cats = [
        QgsRendererCategory(0, sym(QColor(30, 136, 229)), tr("Aktywny")),
        QgsRendererCategory(1, sym(QColor(229, 57, 53), "triangle"), tr("Odstający (|w| > 3,29)")),
        QgsRendererCategory(2, sym(QColor(150, 150, 150), "square"), tr("Wyłączony")),
    ]
    layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))


def set_labels(layer, expression: str, color: QColor, on_line=False, size=8.0):
    s = QgsPalLayerSettings()
    s.enabled = True
    s.isExpression = True
    s.fieldName = expression
    fmt = QgsTextFormat()
    fmt.setSize(size)
    fmt.setColor(color)
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    buf.setColor(QColor(255, 255, 255, 230))
    fmt.setBuffer(buf)
    s.setFormat(fmt)
    if on_line:
        s.placement = Qgis.LabelPlacement.Line
        s.lineSettings().setPlacementFlags(Qgis.LabelLinePlacementFlag.OnLine)
        s.repeatDistance = 60
        s.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
    else:
        s.placement = Qgis.LabelPlacement.OrderedPositionsAroundPoint
        s.dist = 1.0
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)
