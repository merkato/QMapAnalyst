"""
Raporty serii czasowej i wydawnictwa wieloarkuszowego: wykresy (charts.py), raport HTML (wykresy wpisane
jako SVG – ostre w każdej skali) i PDF (wykresy jako obrazy), eksport samych wykresów do SVG i PNG (slajdy).
"""

import math
import os
import re
from datetime import datetime
from html import escape

from qgis.PyQt.QtCore import QByteArray, QCoreApplication, QRectF, Qt
from qgis.PyQt.QtGui import QImage, QPainter, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import QFileDialog, QLabel, QScrollArea, QVBoxLayout, QWidget

from . import charts as ch
from . import coverage as cv
from . import export as ex
from . import math_engine as me
from . import report
from . import series as sr


def tr(text):
    return QCoreApplication.translate("QMapAnalystLibrary", text)


def chart_texts():
    """Napisy wykresów w języku interfejsu."""
    return {
        "accuracy_title": tr("Dokładność map w czasie"),
        "accuracy_sub": tr("RMSE wyrównania (skala logarytmiczna); kreska – lata wydania"),
        "year": tr("rok"), "rmse": "RMSE", "points": tr("punkty"),
        "errors_title": tr("Błędy położenia punktów na mapach"),
        "errors_sub": tr("każdy punkt to obiekt zestawu; kreska – mediana (skala logarytmiczna)"),
        "outlier": tr("odstający (test Baardy)"), "median": tr("mediana"),
        "similarity_title": tr("Podobieństwo błędów między mapami"),
        "similarity_sub": tr("korelacja wektorowa r błędów punktów wspólnych; * – istotne (p < 0,05)"),
        "similar": tr("podobne"), "unrelated": tr("bez związku"), "opposite": tr("przeciwne"),
        "too_few": tr("za mało punktów wspólnych"),
        "vectors_title": tr("Wektory błędów położenia"),
        "vectors_sub": tr("kierunek i wielkość przesunięcia obiektów; powiększenie różne w każdym panelu – patrz podziałka"),
        "error_of": tr("błędu"),
        "sheets_title": tr("Arkusze wydawnictwa"), "sheets_rmse": tr("RMSE arkusza"),
        "sheets_scale": tr("Odchylenie skali od średniej"),
        "index_title": tr("Skorowidz arkuszy"),
        "index_sub": tr("zasięgi arkuszy na mapie współczesnej; punkty – zakładki (wskazania na dwóch arkuszach)"),
        "overlap": tr("zakładka"), "overlap_worst": tr("największa rozbieżność"), "no_data": tr("brak danych"),
    }


def map_label(m):
    return cv.analysis_label(m.column) if m.key != cv.CURRENT else (m.analysis.title or tr("bieżąca analiza"))


def sheet_label(s):
    return tr("arkusz {s}").format(s=s.analysis.sheet) if s.analysis.sheet else f"#{s.analysis.analysis_id}"


# ---------------------------------------------------------------------------
#  Wykresy
# ---------------------------------------------------------------------------
def series_charts(ser, unit_m=1.0, label=map_label):
    """[(nazwa pliku, tytuł, svg)] wykresów serii czasowej."""
    texts = chart_texts()
    names = {p.uid: p.name for p in ser.points}
    out = [("dokladnosc", texts["accuracy_title"], ch.accuracy_timeline(
        [{"label": label(m), "year_from": m.column.year_from, "year_to": m.column.year_to, "rmse_m": m.rmse_m, "n": m.n}
         for m in ser.maps if m.fitted], texts))]
    out.append(("bledy_punktow", texts["errors_title"], ch.error_strip(
        [{"label": label(m), "errors": [(names.get(u, "?"), e, u in m.outliers) for u, e in m.err_m.items()]}
         for m in ser.maps], texts)))
    if len(ser.maps) >= 2:
        idx = {m.key: i for i, m in enumerate(ser.maps)}
        pairs = {(idx[p.a], idx[p.b]): (p.r, p.p, p.n, p.similar) for p in ser.pairs}
        out.append(("podobienstwo", texts["similarity_title"],
                    ch.similarity_matrix([label(m) for m in ser.maps], pairs, texts)))
    panels = [{"label": label(m), "sub": f"RMSE {ch.metres_label(round(m.rmse_m))}" if math.isfinite(m.rmse_m) else "",
               "points": [(names.get(u, "?"), *m.pos[u], *m.vec[u], m.err_m[u], u in m.outliers) for u in m.vec]}
              for m in ser.maps]
    out.append(("wektory", texts["vectors_title"], ch.vector_panels(panels, unit_m, texts)))
    return out


def publication_charts(pub, label=sheet_label):
    texts = chart_texts()
    out = [("arkusze", texts["sheets_title"], ch.sheet_bars(
        [{"label": label(s), "rmse_m": s.rmse_m, "scale_den": s.scale_den} for s in pub.sheets], texts))]
    pos = {u: s.pos[u] for s in pub.sheets for u in s.pos}
    out.append(("skorowidz", texts["index_title"], ch.sheet_index(
        [{"label": s.analysis.sheet or f"#{s.analysis.analysis_id}", "footprint": s.footprint} for s in pub.sheets],
        [{"name": o.name, "x": pos[o.uid][0], "y": pos[o.uid][1], "diff_m": o.diff_m} for o in pub.overlaps if o.uid in pos],
        texts)))
    return out


def svg_to_image(svg, width=None, scale=2.0):
    """SVG → QImage (białe tło); width – szerokość w pikselach albo scale × szerokość naturalna."""
    r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    size = r.defaultSize()
    w = int(width) if width else int(size.width() * scale)
    h = max(1, int(w * size.height() / max(size.width(), 1)))
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.white)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p, QRectF(0, 0, w, h))
    p.end()
    return img


def export_charts(folder, charts, prefix=""):
    """Zapis wykresów do SVG i PNG (2× – do slajdów); zwraca listę plików."""
    files = []
    for name, _title, svg in charts:
        base = os.path.join(folder, f"{prefix}{name}")
        with open(base + ".svg", "w", encoding="utf-8") as f:
            f.write(svg)
        svg_to_image(svg, scale=2.0).save(base + ".png")
        files += [base + ".svg", base + ".png"]
    return files


# ---------------------------------------------------------------------------
#  Raport
# ---------------------------------------------------------------------------
def _num(v, nd=1):
    return "–" if v is None or not math.isfinite(v) else report.fmt(v, nd)


def _table(head, rows, numeric_from=1):
    h = ["<table width='100%'><tr>" + "".join(f"<th>{escape(c)}</th>" for c in head) + "</tr>"]
    for r in rows:
        cells = []
        for k, (v, cls) in enumerate(r):
            css = ("n " if k >= numeric_from else "") + (cls or "")
            cells.append(f"<td class='{css.strip()}'>{v}</td>")
        h.append("<tr>" + "".join(cells) + "</tr>")
    h.append("</table>")
    return "".join(h)


def _svg_width(svg):
    m = re.search(r"viewBox='0 0 ([0-9.]+) ", svg)
    return float(m.group(1)) if m else 760.0


def _chart_block(k, svg, mode, images):
    if mode == "pdf":
        name = f"qma-chart-{k}"
        frac = min(1.0, _svg_width(svg) / 760.0)              # wąski wykres – węższy obraz
        images[name] = lambda w, svg=svg, frac=frac: svg_to_image(svg, w * frac)
        return f"<p><img src='{name}' width='__QMA_WIDTH_{frac:.3f}__'></p>"
    return f"<div style='margin:10px 0 4px 0'>{svg}</div>"


def _scale_text(v):
    return f"1 : {v:,.0f}".replace(",", " ") if v and math.isfinite(v) else "–"


def series_report(ser, mode="html", unit_m=1.0, colors=None):
    """(HTML, obrazy dla PDF) raportu serii czasowej. mode: "html" (SVG w treści) albo "pdf"."""
    images = {}
    charts = series_charts(ser, unit_m)
    by_key = {m.key: m for m in ser.maps}
    names = {p.uid: p.name for p in ser.points}
    parts = [f"<h2>{escape(tr('Seria czasowa'))}: {escape(ser.set_name)}</h2>",
             report.meta_html([(tr("Zestaw punktów"), ser.set_name), (tr("Punkty zestawu"), len(ser.points)),
                               (tr("Mapy"), len(ser.maps)),
                               (tr("Model"), QCoreApplication.translate("QMapAnalystEngine", me.MODELS[ser.model]["name"])),
                               (tr("Data"), datetime.now().strftime("%Y-%m-%d"))])]
    similar = ser.similar_pairs()
    summary = [tr("Elementem serii jest mapa: wydawnictwo wieloarkuszowe to jeden element (arkusze wyrównywane osobno, "
                  "wyniki łączone). Wszystkie mapy wyrównano tym samym modelem.")]
    if similar:
        summary.append(tr("Podobne błędy (możliwe kopiowanie): {pairs}").format(
            pairs="; ".join(f"{map_label(by_key[p.a])} → {map_label(by_key[p.b])} (r = {_num(p.r, 2)})" for p in similar)))
    parts.append("<p>" + "<br>".join(escape(s) for s in summary) + "</p>")
    chart = dict((name, svg) for name, _t, svg in charts)
    parts.append(_chart_block(0, chart["dokladnosc"], mode, images))
    parts.append(f"<h3>{escape(tr('Mapy'))}</h3>")
    parts.append(_table([tr("Mapa"), tr("Arkusze"), tr("Punkty"), "σ₀ [m]", "RMSE [m]", tr("Maks. [m]"), tr("Skala"),
                         tr("Obrót [°]"), tr("Odstające")],
                        [[(escape(map_label(m)), ""), (str(m.sheet_count), ""), (str(m.n), ""), (_num(m.sigma0_m), ""),
                          (_num(m.rmse_m), ""), (_num(m.max_m), ""), (_scale_text(m.scale_den), ""),
                          (_num(m.rotation_deg, 2), ""),
                          (escape(", ".join(names.get(u, "?") for u in m.outliers)) or "–", "bad" if m.outliers else "")]
                         for m in ser.maps]))
    parts.append(_chart_block(1, chart["bledy_punktow"], mode, images))
    if "podobienstwo" in chart:
        parts.append(f"<h3>{escape(tr('Podobieństwo reszt'))}</h3>")
        parts.append(_chart_block(2, chart["podobienstwo"], mode, images))
        rows = [[(escape(map_label(by_key[p.a])), "bad" if p.similar else ""), (escape(map_label(by_key[p.b])), ""),
                 (str(p.n), ""), (_num(p.r, 2), "bad" if p.similar else ""), (_num(p.p, 3), ""), (_num(p.diff_m), "")]
                for p in ser.pairs if p.n >= sr.MIN_COMMON]
        if rows:
            parts.append(_table([tr("Mapa wcześniejsza"), tr("Mapa późniejsza"), tr("Punkty wspólne"), "r", "p",
                                 tr("Śr. różnica [m]")], rows, numeric_from=2))
    parts.append(f"<h3>{escape(tr('Wektory błędów położenia'))}</h3>")
    parts.append(_chart_block(3, chart["wektory"], mode, images))
    parts.append(f"<h3>{escape(tr('Błędy punktów'))}</h3>")
    rows = []
    for p in sorted(ser.points, key=lambda p: -(ser.point_summary(p.uid)[1] if ser.point_summary(p.uid)[0] else -1)):
        n, mean, _v = ser.point_summary(p.uid)
        if not n:
            continue
        rows.append([(escape(p.name), "")] + [(_num(m.err_m.get(p.uid)) if p.uid in m.err_m else "",
                                               "bad" if p.uid in m.outliers else "") for m in ser.maps]
                    + [(str(n), ""), (_num(mean), "")])
    parts.append(_table([tr("Punkt")] + [map_label(m) for m in ser.maps] + [tr("Mapy"), tr("Średnio [m]")], rows))
    parts.append(f"<div class='note'>{escape(tr('Błąd położenia punktu: gdzie mapa umieszcza obiekt względem jego położenia w bibliotece, po usunięciu transformacji arkusza (arkusze wydawnictwa wieloarkuszowego wyrównywane osobno). Podobieństwo reszt: korelacja wektorowa r błędów punktów wspólnych, p – test permutacyjny.'))}</div>")
    return report.full_report_html(parts, colors or dict(report.LIGHT)), images


def publication_report(pub, mode="html", colors=None):
    """(HTML, obrazy dla PDF) raportu wydawnictwa wieloarkuszowego."""
    images = {}
    charts = dict((name, svg) for name, _t, svg in publication_charts(pub))
    m = pub.map
    parts = [f"<h2>{escape(tr('Wydawnictwo wieloarkuszowe'))}: {escape(m.title or '?')}</h2>",
             report.meta_html([(tr("Mapa"), m.title or "?"), (tr("Autor / wydawca"), m.author or "–"),
                               (tr("Rok"), str(m.year_from or "–")), (tr("Arkusze"), len(pub.sheets)),
                               (tr("Data"), datetime.now().strftime("%Y-%m-%d"))])]
    sc, rm = pub.spread("scale_den"), pub.spread("rmse_m")
    summary = []
    if sc:
        summary.append(tr("Skala arkuszy: 1 : {a} – 1 : {b} (rozrzut {p} %).").format(
            a=f"{sc[0]:,.0f}".replace(",", " "), b=f"{sc[1]:,.0f}".replace(",", " "), p=_num(pub.scale_spread_pct())))
    if rm:
        summary.append(tr("RMSE arkuszy: {a} – {b} m.").format(a=_num(rm[0]), b=_num(rm[1])))
    if summary:
        parts.append("<p>" + " ".join(escape(s) for s in summary) + "</p>")
    parts.append(_chart_block(0, charts["arkusze"], mode, images))
    parts.append(_table([tr("Arkusz"), tr("Punkty"), "σ₀ [m]", "RMSE [m]", tr("Maks. [m]"), tr("Skala"), tr("Obrót [°]"),
                         tr("Skale osi X/Y"), tr("Odstające")],
                        [[(escape(sheet_label(s)), ""), (str(s.n), ""), (_num(s.sigma0_m), ""), (_num(s.rmse_m), ""),
                          (_num(s.max_m), ""), (_scale_text(s.scale_den), ""), (_num(s.rotation_deg, 2), ""),
                          (_num(s.axis_ratio, 3), ""), (str(len(s.outliers)), "bad" if s.outliers else "")]
                         for s in pub.sheets]))
    parts.append(f"<h3>{escape(tr('Skorowidz arkuszy'))}</h3>")
    parts.append(_chart_block(1, charts["skorowidz"], mode, images))
    if pub.overlaps:
        sheets = {s.analysis.analysis_id: s for s in pub.sheets}
        parts.append(f"<h3>{escape(tr('Zakładki'))}</h3>")
        parts.append(_table([tr("Punkt"), tr("Arkusz A"), tr("Arkusz B"), tr("Rozbieżność [m]")],
                            [[(escape(o.name), ""), (escape(sheet_label(sheets[o.a])), ""),
                              (escape(sheet_label(sheets[o.b])), ""), (_num(o.diff_m), "")] for o in pub.overlaps],
                            numeric_from=3))
    return report.full_report_html(parts, colors or dict(report.LIGHT)), images


def save_report(path, html, images, title, subtitle=""):
    """Zapis raportu: .pdf (obrazy wykresów) albo .html (SVG w treści)."""
    if path.lower().endswith(".pdf"):
        return ex.export_html_pdf(path, html, title, subtitle, images)
    return ex.write_html(path, html, title)


# ---------------------------------------------------------------------------
#  Interfejs: podgląd wykresów i zapis
# ---------------------------------------------------------------------------
class ChartsView(QScrollArea):
    """Przewijana kolumna wykresów (podgląd w oknie serii / wydawnictwa)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._box = QWidget()
        self._lay = QVBoxLayout(self._box)
        self._lay.setSpacing(12)
        self.setWidget(self._box)
        self.labels = []

    def set_charts(self, charts):
        for lbl in self.labels:
            lbl.deleteLater()
        self.labels = []
        while self._lay.count():
            item = self._lay.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for _name, title, svg in charts:
            pix = QPixmap.fromImage(svg_to_image(svg, scale=2.0))
            pix.setDevicePixelRatio(2.0)                    # ostry obraz także na ekranach HiDPI
            lbl = QLabel()
            lbl.setPixmap(pix)
            lbl.setToolTip(title)
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._lay.addWidget(lbl)
            self.labels.append(lbl)
        self._lay.addStretch()


def safe_name(text):
    return re.sub(r"[^\w\-]+", "_", text or "").strip("_")[:60] or "qmapanalyst"


def ask_report_path(parent, title, base, kind):
    """Ścieżka raportu PDF lub HTML (z rozszerzeniem) albo None."""
    flt = tr("Raport PDF (*.pdf)") if kind == "pdf" else tr("Strona HTML (*.html)")
    path, _f = QFileDialog.getSaveFileName(parent, title, f"{safe_name(base)}.{kind}", flt)
    if not path:
        return None
    return path if path.lower().endswith("." + kind) else path + "." + kind


def ask_charts_folder(parent):
    folder = QFileDialog.getExistingDirectory(parent, tr("Folder na wykresy (SVG i PNG)"))
    return folder or None
