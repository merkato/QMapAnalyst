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


def export_canvas_image(canvas, path, scale=2):
    """Synchroniczny zapis widoku kanwy (z nakładkami) do TIFF z plikiem georeferencji .tfw.

    QgsMapCanvas.saveAsImage() renderuje asynchronicznie – obraz mógł nie zostać zapisany.
    """
    from qgis.core import QgsMapRendererParallelJob, QgsMapSettings, QgsMapSettingsUtils
    from qgis.PyQt.QtCore import QSize
    src = canvas.mapSettings()
    settings = QgsMapSettings(src)
    size = src.outputSize()
    settings.setOutputSize(QSize(max(1, size.width() * scale), max(1, size.height() * scale)))
    settings.setOutputDpi(src.outputDpi() * scale)
    settings.setBackgroundColor(canvas.canvasColor())
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    img = job.renderedImage()
    if img.isNull() or not img.save(path, "TIFF"):
        raise RuntimeError(tr("Nie udało się zapisać obrazu: {p}").format(p=path))
    try:
        world = QgsMapSettingsUtils.worldFileContent(settings)
        with open(os.path.splitext(path)[0] + ".tfw", "w", encoding="ascii") as f:
            f.write(world)
    except Exception:  # noqa: BLE001 – plik georeferencji jest opcjonalny
        pass
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

        if not html_pages and not overlays:
            raise RuntimeError(tr("Brak widocznych nakładek mapy dawnej do eksportu."))
        for n_ov, lyr in enumerate(overlays):
            if not html_pages and n_ov == 0:
                layout.pageCollection().page(0).setPageSize("A4", L)
                idx = 0
            else:
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


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def write_html(path, body_html, title):
    from html import escape
    doc = (f"<!DOCTYPE html>\n<html><head><meta charset='utf-8'><title>{escape(title)}</title></head>"
           f"<body style='max-width:900px;margin:24px auto;font-family:Segoe UI,Arial,sans-serif'>"
           f"<h1 style='font-size:16pt'>{escape(title)}</h1>{body_html}</body></html>\n")
    return write_text(path, doc)


def export_html_pdf(path, body_html, title, subtitle=""):
    """Wielostronicowy PDF A4 z raportem HTML: nagłówek, stopka z numeracją stron, automatyczne łamanie."""
    from qgis.PyQt.QtCore import QMarginsF, QRectF, QSizeF, Qt
    from qgis.PyQt.QtGui import (QAbstractTextDocumentLayout, QColor, QImage, QPageLayout, QPageSize, QPainter,
                                 QPalette, QPdfWriter, QPen, QTextDocument)

    dpi = 300
    mm = dpi / 25.4
    writer = QPdfWriter(path)
    writer.setResolution(dpi)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(16, 12, 16, 12), QPageLayout.Unit.Millimeter)
    writer.setTitle(title)
    writer.setCreator("QMapAnalyst")

    area = writer.pageLayout().paintRectPixels(dpi)
    width, height = float(area.width()), float(area.height())
    header_h, footer_h = 15 * mm, 9 * mm
    body_h = height - header_h - footer_h

    doc = QTextDocument()
    doc.documentLayout().setPaintDevice(writer)
    font = QFont("Arial")
    font.setPointSizeF(9)
    doc.setDefaultFont(font)
    doc.setDocumentMargin(0)
    doc.setHtml(body_html)
    doc.setPageSize(QSizeF(width, body_h))
    pages = max(1, doc.pageCount())

    logo = QImage(os.path.join(os.path.dirname(__file__), "icon.png"))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    accent = QColor(43, 87, 154)
    muted = QColor(89, 99, 110)

    painter = QPainter()
    if not painter.begin(writer):
        raise RuntimeError(tr("Nie można utworzyć pliku PDF: {p}").format(p=path))
    try:
        for i in range(pages):
            if i:
                writer.newPage()
            # nagłówek
            if not logo.isNull():
                painter.drawImage(QRectF(0, 0, 9 * mm, 9 * mm), logo)
            f = QFont("Arial")
            f.setPointSizeF(12)
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(accent)
            painter.drawText(QRectF(12 * mm, 0, width - 12 * mm, 6 * mm),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
            f.setBold(False)
            f.setPointSizeF(8)
            painter.setFont(f)
            painter.setPen(muted)
            painter.drawText(QRectF(12 * mm, 5.5 * mm, width - 12 * mm, 5 * mm),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), subtitle)
            painter.setPen(QPen(QColor(208, 215, 222), 0.3 * mm))
            painter.drawLine(QRectF(0, header_h - 3 * mm, width, 0).topLeft(), QRectF(0, header_h - 3 * mm, width, 0).topRight())

            # treść strony
            # treść rysowana z jawną paletą (czarny tekst) – niezależnie od motywu systemu/QGIS
            painter.save()
            painter.translate(0, header_h - i * body_h)
            clip = QRectF(0, i * body_h, width, body_h)
            painter.setClipRect(clip)
            ctx = QAbstractTextDocumentLayout.PaintContext()
            pal = QPalette(ctx.palette)
            pal.setColor(QPalette.ColorRole.Text, QColor(31, 35, 40))
            pal.setColor(QPalette.ColorRole.WindowText, QColor(31, 35, 40))
            ctx.palette = pal
            ctx.clip = clip
            doc.documentLayout().draw(painter, ctx)
            painter.restore()

            # stopka
            y = height - footer_h + 3 * mm
            painter.setPen(QPen(QColor(208, 215, 222), 0.3 * mm))
            painter.drawLine(QRectF(0, y, width, 0).topLeft(), QRectF(0, y, width, 0).topRight())
            painter.setFont(f)
            painter.setPen(muted)
            painter.drawText(QRectF(0, y + 1 * mm, width / 2, 5 * mm),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             f"QMapAnalyst · {tr('Wygenerowano:')} {stamp}")
            painter.drawText(QRectF(width / 2, y + 1 * mm, width / 2, 5 * mm),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             tr("Strona {i} z {n}").format(i=i + 1, n=pages))
    finally:
        painter.end()
    log(f"PDF ({pages} str.): {path}")
    return path

