"""
Macierz pokrycia punkt × mapa dla zestawu punktów biblioteki (pierwszy element trybu serii).

Komórka opisuje punkt na danej mapie:
    sure / uncertain  – zidentyfikowany (pewny / niepewny) i aktywny,
    outlier           – zidentyfikowany, ale odstający w wyrównaniu tej mapy (test Baardy),
    illegible         – nieczytelny, disabled – wyłączony przez użytkownika,
    absent            – brak na mapie (informacja, nie błąd),
    outside           – nie wskazany i leży poza zasięgiem arkusza (ramka mapy przeliczona transformacją),
    None              – jeszcze nie wskazany na tej mapie.
Kolumny to arkusze (analizy) albo mapy – wtedy komórka łączy arkusze mapy (punkt obecny na którymkolwiek
arkuszu jest obecny na mapie; odstający na którymkolwiek – odstający).
Wyniki liczone są na żądanie z gcp_links (bez przechowywania obliczeń).
Moduł bez zależności od QGIS.
"""

import csv
import dataclasses
import io
import json
from dataclasses import dataclass, field

import numpy as np

from . import library as lb
from . import math_engine as me

PRESENT = ("sure", "uncertain", "outlier")      # punkt obecny i zidentyfikowany na mapie
CODES = ("sure", "uncertain", "outlier", "illegible", "disabled", "absent", "outside")
_TODO = "todo"                                   # wewnętrznie: w zasięgu (lub zasięg nieznany), nie wskazany
PRIORITY = ("outlier", "uncertain", "sure", "illegible", "disabled", "absent", _TODO, "outside")  # łączenie arkuszy
CURRENT = 0                                      # klucz kolumny bieżącej analizy jeszcze nie zapisanej
BY_SHEET, BY_MAP = "sheet", "map"


@dataclass
class Column:
    """Kolumna macierzy: arkusz (analiza) albo mapa (jej arkusze)."""
    key: int                                     # analysis_id (kolumny arkuszy) albo map_id (kolumny map)
    title: str = ""
    author: str = ""
    year_from: int = None
    year_to: int = None
    scale: int = None
    sheet: str = ""
    map_id: int = None
    analysis_ids: list = field(default_factory=list)

    @property
    def analysis_id(self):
        """Analiza kolumny – gdy jest dokładnie jedna (kolumna arkusza lub mapy jednoarkuszowej)."""
        return self.analysis_ids[0] if len(self.analysis_ids) == 1 else None


@dataclass
class Coverage:
    set_id: int
    set_name: str
    model: str
    points: list = field(default_factory=list)       # RefPoint (kolejność wierszy)
    columns: list = field(default_factory=list)      # Column (kolejność kolumn)
    cells: dict = field(default_factory=dict)        # (uid, klucz kolumny) -> kod
    fitted: dict = field(default_factory=dict)       # klucz -> True, gdy wyrównano (wykrywanie odstających)
    current_id: int = None                           # klucz kolumny ze stanem bieżącym (panel GCP)
    by: str = BY_SHEET

    @property
    def analyses(self):
        return self.columns

    def cell(self, uid, key):
        return self.cells.get((uid, key))

    def common(self):
        """uid punktów obecnych na wszystkich mapach (wspólne minimum do porównań)."""
        if not self.columns:
            return []
        return [p.uid for p in self.points if all(self.cell(p.uid, c.key) in PRESENT for c in self.columns)]

    def row_count(self, uid):
        return sum(1 for c in self.columns if self.cell(uid, c.key) in PRESENT)

    def column_count(self, key):
        return sum(1 for p in self.points if self.cell(p.uid, key) in PRESENT)

    def repeated_outliers(self, minimum=2):
        """uid punktów odstających na co najmniej `minimum` mapach (podejrzenie błędnej identyfikacji)."""
        return [p.uid for p in self.points
                if sum(1 for c in self.columns if self.cell(p.uid, c.key) == "outlier") >= minimum]


def _year_key(c):
    year = c.year_from or c.year_to
    return (year is None, year or 0, (c.title or "").lower(), c.sheet or "", c.key)


def _link_code(ln):
    if ln.status == "absent":
        return "absent"
    if ln.status == "illegible":
        return "illegible"
    if not ln.enabled:
        return "disabled"
    return ln.status if ln.status in ("sure", "uncertain") else "sure"


def _fit(links, model):
    """Wyrównanie arkusza z punktów obliczeniowych: (FitResult lub None, indeksy punktów w links)."""
    idx = [k for k, ln in enumerate(links) if ln.computable and ln.enabled]
    if len(idx) < me.MODELS[model]["min_pts"]:
        return None, idx
    src = np.array([[links[k].x_old, links[k].y_old] for k in idx])
    dst = np.array([[links[k].ref_x, links[k].ref_y] for k in idx])
    try:
        return me.fit_transform(model, src, dst), idx
    except (ValueError, np.linalg.LinAlgError):
        return None, idx


def _outliers(fit, idx, model):
    """Indeksy (w links) punktów odstających; None – za mało punktów (bez nadwyżki obserwacji)."""
    if fit is None or len(idx) <= me.MODELS[model]["min_pts"]:
        return None
    w = np.nan_to_num(fit.std_residuals)
    return {idx[j] for j in range(len(idx)) if w[j] > me.OUTLIER_THRESHOLD}


def _outlier_indices(links, model):
    fit, idx = _fit(links, model)
    return _outliers(fit, idx, model)


def footprint(fit, frame, steps=8):
    """Zasięg arkusza w układzie analizy: ramka (współrzędne skanu) zagęszczona i przeliczona transformacją."""
    if fit is None or not frame:
        return None
    pts = []
    for (x1, y1), (x2, y2) in zip(frame, frame[1:] + frame[:1]):
        pts += [(x1 + (x2 - x1) * t / steps, y1 + (y2 - y1) * t / steps) for t in range(steps)]
    try:
        out = fit.predict(np.array(pts, dtype=float))
    except (ValueError, np.linalg.LinAlgError, me.EngineError):
        return None
    return [(float(x), float(y)) for x, y in out] if np.all(np.isfinite(out)) else None


def _entries(lib, current):
    """(Analysis, punkty lub None – z pliku) dla każdego arkusza; stan bieżący zastępuje zapisany. (wpisy, id bieżącej)"""
    cur_a, cur_links = current if current is not None else (None, None)
    cur_id = cur_a.analysis_id if cur_a is not None else None
    entries = []
    for a in lib.analyses():
        if cur_links is not None and cur_id is not None and a.analysis_id == cur_id:
            entries.append((dataclasses.replace(cur_a, map_id=cur_a.map_id or a.map_id), cur_links))
        else:
            entries.append((a, None))
    if cur_links is not None and cur_id is None:
        a = dataclasses.replace(cur_a, analysis_id=CURRENT, map_id=None) if cur_a is not None else lb.Analysis(analysis_id=CURRENT)
        entries.append((a, cur_links))
        cur_id = CURRENT
    return entries, (cur_id if cur_links is not None else None)


def _sheet_codes(a, links, points, model, to_analysis):
    """Kody punktów zestawu na arkuszu: {uid: kod} (także „outside”/_TODO dla niewskazanych); czy zbadano odstające.
    to_analysis(x, y) – przeliczenie z układu biblioteki do układu analizy (None – ten sam)."""
    members = {p.uid for p in points}
    fit, idx = _fit(links, model)
    outliers = _outliers(fit, idx, model)
    codes = {}
    for k, ln in enumerate(links):
        if ln.uid in members:
            code = _link_code(ln)
            if outliers and k in outliers and code in ("sure", "uncertain"):
                code = "outlier"
            codes[ln.uid] = code
    if codes:
        area = footprint(fit, lb.frame_points(a.frame))
        for p in points:
            if p.uid not in codes:
                codes[p.uid] = "outside" if area is not None and not _inside(p, area, to_analysis) else _TODO
    return codes, outliers is not None


def _inside(p, area, to_analysis):
    try:
        x, y = to_analysis(p.x, p.y) if to_analysis is not None else (p.x, p.y)
    except RuntimeError:
        return True                                   # nieprzeliczalny – nie wykluczamy
    return lb.point_in_polygon(x, y, area)


def _column_for(a, by, maps):
    if by == BY_MAP and a.analysis_id != CURRENT:
        m = maps.get(a.map_id)
        src = m if m is not None else a
        return Column(key=a.map_id if m is not None else -a.analysis_id, map_id=a.map_id,
                      **{f: getattr(src, f) for f in lb.MAP_FIELDS})
    return Column(key=a.analysis_id, sheet=a.sheet, map_id=a.map_id, **{f: getattr(a, f) for f in lb.MAP_FIELDS})


def build_coverage(lib, set_id, model="affine", analysis_ids=None, current=None, by=BY_SHEET):
    """Macierz pokrycia zestawu.

    analysis_ids – ograniczenie do tych arkuszy (domyślnie: wszystkie z punktami zestawu);
    current = (Analysis lub None, [Link]) – bieżący, być może niezapisany stan analizy otwartej w panelu GCP:
    zastępuje zapisaną wersję tej analizy albo – gdy analizy nie zapisano – dochodzi jako kolumna CURRENT;
    by – kolumny według arkusza (BY_SHEET) albo mapy (BY_MAP, arkusze mapy łączone wg PRIORITY)."""
    sets = {sid: name for sid, name, _d, _n in lib.sets()}
    if set_id not in sets:
        raise lb.LibraryError(f"no set {set_id}")
    points = sorted(lib.points(set_id), key=lambda p: lb.normalize(p.name))
    cov = Coverage(set_id, sets[set_id], model, points, by=by)
    entries, cur_id = _entries(lib, current)
    maps = {m.map_id: m for m in lib.maps()} if by == BY_MAP else {}
    columns, transformers = {}, {}
    lib_crs = lib.crs_wkt
    for a, links in entries:
        if analysis_ids is not None and a.analysis_id not in analysis_ids and a.analysis_id != cur_id:
            continue
        if links is None:
            links = lib.links(a.analysis_id)
        if a.ref_crs not in transformers:
            transformers[a.ref_crs] = lb.crs_transformer(lib_crs, a.ref_crs)
        codes, fitted = _sheet_codes(a, links, points, model, transformers[a.ref_crs])
        if not any(c != _TODO and c != "outside" for c in codes.values()):
            continue
        col = _column_for(a, by, maps)
        col = columns.setdefault(col.key, col)
        col.analysis_ids.append(a.analysis_id)
        if a.analysis_id == cur_id:
            cov.current_id = col.key
        cov.fitted[col.key] = cov.fitted.get(col.key, False) or fitted
        for uid, code in codes.items():
            old = cov.cells.get((uid, col.key))
            if old is None or PRIORITY.index(code) < PRIORITY.index(old):
                cov.cells[(uid, col.key)] = code
    cov.cells = {k: v for k, v in cov.cells.items() if v != _TODO}
    cov.columns = sorted(columns.values(), key=_year_key)
    return cov


# ----------------------------------------------------------------------
#  Eksport
# ----------------------------------------------------------------------
def analysis_label(c):
    years = c.year_from if not c.year_to or c.year_to == c.year_from else f"{c.year_from or '?'}–{c.year_to}"
    key = getattr(c, "key", getattr(c, "analysis_id", None))
    title = c.title or ("current" if key == CURRENT else f"#{key}")
    return title + (f" ({years})" if years else "") + (f" [{c.sheet}]" if c.sheet else "")


def to_csv(cov):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["uid", "name"] + [analysis_label(c) for c in cov.columns] + ["present"])
    for p in cov.points:
        w.writerow([p.uid, p.name] + [cov.cell(p.uid, c.key) or "" for c in cov.columns] + [cov.row_count(p.uid)])
    return out.getvalue()


def to_json(cov):
    data = {
        "set": {"id": cov.set_id, "name": cov.set_name},
        "model": cov.model,
        "columns_by": cov.by,
        "codes": list(CODES),
        "columns": [{"key": c.key, "map_id": c.map_id, "analysis_ids": c.analysis_ids, "title": c.title,
                     "year_from": c.year_from, "year_to": c.year_to, "scale": c.scale, "sheet": c.sheet,
                     "outliers_tested": cov.fitted.get(c.key, False)} for c in cov.columns],
        "points": [{"uid": p.uid, "name": p.name, "cells": {str(c.key): cov.cell(p.uid, c.key) for c in cov.columns}}
                   for p in cov.points],
        "common": cov.common(),
        "repeated_outliers": cov.repeated_outliers(),
    }
    return json.dumps(data, ensure_ascii=False, indent=1)
