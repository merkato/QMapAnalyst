"""
Eksport wyników: obraz mapy dawnej, wielostronicowy raport PDF, tabele CSV.
"""

import csv
import os
import traceback
from datetime import datetime

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QFont
from qgis.core import (
    Qgis, QgsProject, QgsPrintLayout, QgsLayoutExporter, QgsLayoutItemMap, QgsLayoutItemLabel,
    QgsLayoutItemPicture, QgsLayoutItemPage, QgsLayoutPoint, QgsLayoutSize, QgsTextFormat,
    QgsMessageLog, QgsRectangle,
)

LOG_TAG = "QMapAnalyst"


def tr(text):
    return QCoreApplication.translate("QMapAnalystExport", text)


def log(msg, level=Qgis.MessageLevel.Info):
    QgsMessageLog.logMessage(msg, LOG_TAG, level)


def export_canvas_image(canvas, path):
    canvas.saveAsImage(path)
    return path


def _label(layout, text, size, page, x, y, w=None, h=None, bold=False, html=False):
    item = QgsLayoutItemLabel(layout)
    fmt = QgsTextFormat()
    font = QFont("Arial", int(size))
    if bold:
        font.setWeight(QFont.Weight.Bold)
    fmt.setFont(font)
    fmt.setSize(size)
    item.setTextFormat(fmt)
    if html:
        try:
            item.setMode(QgsLayoutItemLabel.Mode.ModeHtml)
        except AttributeError:  # starsze wiązania PyQt5
            item.setMode(QgsLayoutItemLabel.ModeHtml)
    item.setText(text)
    layout.addLayoutItem(item)
    if w is None:
        item.adjustSizeToText()
    else:
        item.attemptResize(QgsLayoutSize(w, h))
    item.attemptMove(QgsLayoutPoint(x, y), page=page)
    return item


def _logo(layout, path, page, x, y, size):
    if not os.path.exists(path):
        return
    pic = QgsLayoutItemPicture(layout)
    pic.setPicturePath(path)
    layout.addLayoutItem(pic)
    pic.attemptResize(QgsLayoutSize(size, size))
    pic.attemptMove(QgsLayoutPoint(x, y), page=page)


def _add_page(layout, orientation):
    page = QgsLayoutItemPage(layout)
    page.setPageSize("A4", orientation)
    layout.pageCollection().addPage(page)
    return layout.pageCollection().pageCount() - 1


def export_report_pdf(path, html_pages, base_layer, overlays, extent: QgsRectangle, title):
    """html_pages: lista (tytuł, html) – każda pozycja na osobnej stronie A4 pionowej.
    overlays: lista warstw – każda na osobnej stronie A4 poziomej, na tle skanu."""
    project = QgsProject.instance()
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName("QMapAnalyst_Export_Temp")
    logo_path = os.path.join(os.path.dirname(__file__), "icon.png")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    P = QgsLayoutItemPage.Orientation.Portrait
    L = QgsLayoutItemPage.Orientation.Landscape

    try:
        project.layoutManager().addLayout(layout)
        layout.pageCollection().page(0).setPageSize("A4", P)

        for i, (page_title, html) in enumerate(html_pages):
            idx = 0 if i == 0 else _add_page(layout, P)
            _logo(layout, logo_path, idx, 15, 12, 16)
            _label(layout, title if i == 0 else page_title, 18 if i == 0 else 14, idx, 35, 12, bold=True)
            _label(layout, f"{tr('Wygenerowano:')} {stamp}", 9, idx, 35, 22)
            _label(layout, html, 9, idx, 15, 34, 180, 250, html=True)

        for lyr in overlays:
            idx = _add_page(layout, L)
            _logo(layout, logo_path, idx, 10, 8, 12)
            _label(layout, f"{title} | {tr('Nakładka:')} {lyr.name()}", 13, idx, 25, 9, bold=True)
            item = QgsLayoutItemMap(layout)
            layout.addLayoutItem(item)
            item.attemptResize(QgsLayoutSize(277, 172))
            item.attemptMove(QgsLayoutPoint(10, 27), page=idx)
            layers = [lyr] + ([base_layer] if base_layer is not None and base_layer.isValid() else [])
            item.setLayers(layers)
            item.setKeepLayerSet(True)
            if base_layer is not None:
                item.setCrs(base_layer.crs())
            item.setFrameEnabled(True)
            item.zoomToExtent(extent)

        exporter = QgsLayoutExporter(layout)
        settings = QgsLayoutExporter.PdfExportSettings()
        settings.dpi = 200
        result = exporter.exportToPdf(path, settings)
        try:
            success = QgsLayoutExporter.ExportResult.Success
        except AttributeError:  # PyQt5
            success = QgsLayoutExporter.Success
        if result != success:
            raise RuntimeError(tr("Eksport PDF nie powiódł się (kod {code}): {msg}").format(
                code=int(result), msg=exporter.errorMessage()))
        log(f"PDF: {path}")
        return path
    except Exception:
        log(traceback.format_exc(), Qgis.MessageLevel.Critical)
        raise
    finally:
        project.layoutManager().removeLayout(layout)


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    return path
