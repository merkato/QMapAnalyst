"""
Eksport wyników: obraz mapy dawnej, wielostronicowy raport PDF, tabele CSV.
"""

import csv
import os
from datetime import datetime

from qgis.PyQt.QtCore import QCoreApplication, QMarginsF, QRectF, QSize, QSizeF, Qt
from qgis.PyQt.QtGui import (
    QAbstractTextDocumentLayout, QColor, QFont, QImage, QPageLayout, QPageSize, QPainter, QPalette,
    QPdfWriter, QPen, QTextDocument,
)
from qgis.core import (
    Qgis, QgsMapRendererCustomPainterJob, QgsMapSettings, QgsMessageLog, QgsRectangle,
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
    from qgis.core import QgsMapRendererParallelJob, QgsMapSettingsUtils
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
    except OSError as e:  # plik georeferencji jest opcjonalny – obraz już zapisano
        log(tr("Nie zapisano pliku georeferencji .tfw: {err}").format(err=e), Qgis.MessageLevel.Warning)
    return path


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


_ACCENT = QColor(43, 87, 154)
_MUTED = QColor(89, 99, 110)
_RULE = QColor(208, 215, 222)
_ALIGN_L = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
_ALIGN_R = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


class _PdfPages:
    """Wspólna oprawa stron PDF A4: nagłówek (logo, tytuł, podtytuł) i stopka z numeracją."""

    DPI = 300

    def __init__(self, path, title, subtitle, landscape=False, margins=(16, 12, 16, 12)):
        self.mm = self.DPI / 25.4
        self.writer = QPdfWriter(path)
        self.writer.setResolution(self.DPI)
        self.writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        self.writer.setPageOrientation(QPageLayout.Orientation.Landscape if landscape
                                       else QPageLayout.Orientation.Portrait)
        self.writer.setPageMargins(QMarginsF(*margins), QPageLayout.Unit.Millimeter)
        self.writer.setTitle(title)
        self.writer.setCreator("QMapAnalyst")
        area = self.writer.pageLayout().paintRectPixels(self.DPI)
        self.width, self.height = float(area.width()), float(area.height())
        self.header_h, self.footer_h = 15 * self.mm, 9 * self.mm
        self.body_h = self.height - self.header_h - self.footer_h
        self.title, self.subtitle = title, subtitle
        self.logo = QImage(os.path.join(os.path.dirname(__file__), "icon.png"))
        self.stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.path = path

    def body_rect(self):
        return QRectF(0, self.header_h, self.width, self.body_h)

    def _font(self, size, bold=False):
        f = QFont("Arial")
        f.setPointSizeF(size)
        f.setBold(bold)
        return f

    def _rule(self, painter, y):
        painter.setPen(QPen(_RULE, 0.3 * self.mm))
        painter.drawLine(QRectF(0, y, self.width, 0).topLeft(), QRectF(0, y, self.width, 0).topRight())

    def frame(self, painter, i, n, caption=""):
        mm = self.mm
        if not self.logo.isNull():
            painter.drawImage(QRectF(0, 0, 9 * mm, 9 * mm), self.logo)
        painter.setFont(self._font(12, True))
        painter.setPen(_ACCENT)
        painter.drawText(QRectF(12 * mm, 0, self.width - 12 * mm, 6 * mm), _ALIGN_L, self.title)
        painter.setFont(self._font(8))
        painter.setPen(_MUTED)
        sub = f"{self.subtitle}  ·  {caption}" if caption and self.subtitle else (caption or self.subtitle)
        painter.drawText(QRectF(12 * mm, 5.5 * mm, self.width - 12 * mm, 5 * mm), _ALIGN_L, sub)
        self._rule(painter, self.header_h - 3 * mm)

        y = self.height - self.footer_h + 3 * mm
        self._rule(painter, y)
        painter.setFont(self._font(8))
        painter.setPen(_MUTED)
        painter.drawText(QRectF(0, y + 1 * mm, self.width / 2, 5 * mm), _ALIGN_L,
                         f"QMapAnalyst · {tr('Wygenerowano:')} {self.stamp}")
        painter.drawText(QRectF(self.width / 2, y + 1 * mm, self.width / 2, 5 * mm), _ALIGN_R,
                         tr("Strona {i} z {n}").format(i=i + 1, n=n))

    def begin(self):
        painter = QPainter()
        if not painter.begin(self.writer):
            raise RuntimeError(tr("Nie można utworzyć pliku PDF: {p}").format(p=self.path))
        return painter


def export_html_pdf(path, body_html, title, subtitle=""):
    """Wielostronicowy PDF A4 z raportem HTML: nagłówek, stopka z numeracją stron, automatyczne łamanie."""
    pdf = _PdfPages(path, title, subtitle)
    width, body_h = pdf.width, pdf.body_h

    doc = QTextDocument()
    doc.documentLayout().setPaintDevice(pdf.writer)
    font = QFont("Arial")
    font.setPointSizeF(9)
    doc.setDefaultFont(font)
    doc.setDocumentMargin(0)
    doc.setHtml(body_html)
    doc.setPageSize(QSizeF(width, body_h))
    pages = max(1, doc.pageCount())

    painter = pdf.begin()
    try:
        for i in range(pages):
            if i:
                pdf.writer.newPage()
            pdf.frame(painter, i, pages)
            # treść rysowana z jawną paletą (czarny tekst) – niezależnie od motywu systemu/QGIS
            painter.save()
            painter.translate(0, pdf.header_h - i * body_h)
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
    finally:
        painter.end()
    log(f"PDF ({pages} str.): {path}")
    return path


def export_maps_pdf(path, base_settings: QgsMapSettings, pages, base_layer, extent: QgsRectangle,
                    title, subtitle="", dpi=250):
    """PDF A4 poziomy – jedna strona na pozycję `pages` = [(podpis, [warstwy…]), …], każda na tle skanu.

    Renderowane bezpośrednio z ustawień kanwy mapy dawnej (QgsMapSettings), a nie przez układ wydruku:
    skan bez georeferencji ma nieokreślony układ współrzędnych, a mapa układu wydruku zastępuje go
    układem projektu i przelicza nakładki poza arkusz (stąd puste strony w wersji 0.2.0).
    """
    if not pages:
        raise RuntimeError(tr("Brak widocznych nakładek mapy dawnej do eksportu."))
    pdf = _PdfPages(path, title, subtitle, landscape=True, margins=(10, 8, 10, 8))
    rect = pdf.body_rect().adjusted(0, 0, 0, -1 * pdf.mm)
    scale = dpi / pdf.DPI  # mapa renderowana w dpi, potem skalowana do rozdzielczości PDF
    out = QSize(max(1, int(rect.width() * scale)), max(1, int(rect.height() * scale)))
    base = [base_layer] if base_layer is not None and base_layer.isValid() else []

    painter = pdf.begin()
    try:
        for i, (caption, layers) in enumerate(pages):
            if i:
                pdf.writer.newPage()
            pdf.frame(painter, i, len(pages), caption)
            settings = QgsMapSettings(base_settings)
            settings.setLayers(list(layers) + base)
            settings.setOutputSize(out)
            settings.setOutputDpi(dpi)
            settings.setDevicePixelRatio(1.0)
            settings.setBackgroundColor(QColor(255, 255, 255))
            settings.setExtent(extent)
            painter.save()
            painter.translate(rect.topLeft())
            painter.scale(1 / scale, 1 / scale)
            painter.setClipRect(QRectF(0, 0, out.width(), out.height()))
            job = QgsMapRendererCustomPainterJob(settings, painter)
            job.start()
            job.waitForFinished()
            painter.restore()
            painter.setPen(QPen(_RULE, 0.3 * pdf.mm))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
    finally:
        painter.end()
    log(f"PDF ({len(pages)} str.): {path}")
    return path
