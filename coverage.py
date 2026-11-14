"""
Macierz pokrycia punkt × mapa dla zestawu punktów biblioteki (pierwszy element trybu serii).

Komórka opisuje punkt na danej mapie:
    sure / uncertain  – zidentyfikowany (pewny / niepewny) i aktywny,
    outlier           – zidentyfikowany, ale odstający w wyrównaniu tej mapy (test Baardy),
    illegible         – nieczytelny, disabled – wyłączony przez użytkownika,
    absent            – brak na mapie (informacja, nie błąd),
    None              – jeszcze nie wskazany na tej mapie.
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
CODES = ("sure", "uncertain", "outlier", "illegible", "disabled", "absent")
CURRENT = 0                                      # analysis_id kolumny bieżącej analizy jeszcze nie zapisanej


@dataclass
class Coverage:
    set_id: int
    set_name: str
    model: str
    points: list = field(default_factory=list)       # RefPoint (kolejność wierszy)
    analyses: list = field(default_factory=list)     # Analysis (kolejność kolumn)
    cells: dict = field(default_factory=dict)        # (uid, analysis_id) -> kod
    fitted: dict = field(default_factory=dict)       # analysis_id -> True, gdy wyrównano (wykrywanie odstających)
    current_id: int = None                           # kolumna ze stanem bieżącym (panel GCP), gdy podano `current`

    def cell(self, uid, analysis_id):
        return self.cells.get((uid, analysis_id))

    def common(self):
        """uid punktów obecnych na wszystkich mapach (wspólne minimum do porównań)."""
        if not self.analyses:
            return []
        return [p.uid for p in self.points
                if all(self.cell(p.uid, a.analysis_id) in PRESENT for a in self.analyses)]

    def row_count(self, uid):
        return sum(1 for a in self.analyses if self.cell(uid, a.analysis_id) in PRESENT)

    def column_count(self, analysis_id):
        return sum(1 for p in self.points if self.cell(p.uid, analysis_id) in PRESENT)

    def repeated_outliers(self, minimum=2):
        """uid punktów odstających na co najmniej `minimum` mapach (podejrzenie błędnej identyfikacji)."""
        return [p.uid for p in self.points
                if sum(1 for a in self.analyses if self.cell(p.uid, a.analysis_id) == "outlier") >= minimum]


def _year_key(a):
    year = a.year_from or a.year_to
    return (year is None, year or 0, (a.title or "").lower(), a.analysis_id)


def _link_code(ln):
    if ln.status == "absent":
        return "absent"
    if ln.status == "illegible":
        return "illegible"
    if not ln.enabled:
        return "disabled"
    return ln.status if ln.status in ("sure", "uncertain") else "sure"


def _outlier_indices(links, model):
    """Indeksy (w links) punktów odstających w wyrównaniu mapy; None – za mało punktów."""
    idx = [k for k, ln in enumerate(links) if ln.computable and ln.enabled]
    if len(idx) <= me.MODELS[model]["min_pts"]:
        return None
    src = np.array([[links[k].x_old, links[k].y_old] for k in idx])
    dst = np.array([[links[k].ref_x, links[k].ref_y] for k in idx])
    try:
        fit = me.fit_transform(model, src, dst)
    except (ValueError, np.linalg.LinAlgError):
        return None
    w = np.nan_to_num(fit.std_residuals)
    return {idx[j] for j in range(len(idx)) if w[j] > me.OUTLIER_THRESHOLD}


def _entries(lib, current):
    """(Analysis, punkty lub None – z pliku) dla każdej kolumny; stan bieżący zastępuje zapisany. (wpisy, current_id)"""
    cur_a, cur_links = current if current is not None else (None, None)
    cur_id = cur_a.analysis_id if cur_a is not None else None
    entries = []
    for a in lib.analyses():
        if cur_links is not None and cur_id is not None and a.analysis_id == cur_id:
            entries.append((cur_a, cur_links))
        else:
            entries.append((a, None))
    if cur_links is not None and cur_id is None:
        a = dataclasses.replace(cur_a, analysis_id=CURRENT) if cur_a is not None else lb.Analysis(analysis_id=CURRENT)
        entries.append((a, cur_links))
        cur_id = CURRENT
    return entries, (cur_id if cur_links is not None else None)


def build_coverage(lib, set_id, model="affine", analysis_ids=None, current=None):
    """Macierz pokrycia zestawu. analysis_ids – ograniczenie kolumn (domyślnie: analizy z punktami zestawu).
    current = (Analysis lub None, [Link]) – bieżący, być może niezapisany stan analizy otwartej w panelu GCP:
    zastępuje zapisaną wersję tej analizy albo – gdy analizy nie zapisano – dochodzi jako kolumna CURRENT."""
    sets = {sid: name for sid, name, _d, _n in lib.sets()}
    if set_id not in sets:
        raise lb.LibraryError(f"no set {set_id}")
    points = sorted(lib.points(set_id), key=lambda p: lb.normalize(p.name))
    members = {p.uid for p in points}
    cov = Coverage(set_id, sets[set_id], model, points)
    entries, cov.current_id = _entries(lib, current)
    for a, links in sorted(entries, key=lambda e: _year_key(e[0])):
        if analysis_ids is not None and a.analysis_id not in analysis_ids and a.analysis_id != cov.current_id:
            continue
        if links is None:
            links = lib.links(a.analysis_id)
        if not any(ln.uid in members for ln in links):
            continue
        cov.analyses.append(a)
        outliers = _outlier_indices(links, model)
        cov.fitted[a.analysis_id] = outliers is not None
        for k, ln in enumerate(links):
            if ln.uid in members:
                code = _link_code(ln)
                if outliers and k in outliers and code in ("sure", "uncertain"):
                    code = "outlier"
                cov.cells[(ln.uid, a.analysis_id)] = code
    return cov


# ----------------------------------------------------------------------
#  Eksport
# ----------------------------------------------------------------------
def analysis_label(a):
    years = a.year_from if not a.year_to or a.year_to == a.year_from else f"{a.year_from or '?'}–{a.year_to}"
    title = a.title or ("current" if a.analysis_id == CURRENT else f"#{a.analysis_id}")
    return title + (f" ({years})" if years else "")


def to_csv(cov):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["uid", "name"] + [analysis_label(a) for a in cov.analyses] + ["present"])
    for p in cov.points:
        w.writerow([p.uid, p.name] + [cov.cell(p.uid, a.analysis_id) or "" for a in cov.analyses]
                   + [cov.row_count(p.uid)])
    return out.getvalue()


def to_json(cov):
    data = {
        "set": {"id": cov.set_id, "name": cov.set_name},
        "model": cov.model,
        "codes": list(CODES),
        "analyses": [{"id": a.analysis_id, "title": a.title, "year_from": a.year_from, "year_to": a.year_to,
                      "scale": a.scale, "sheet": a.sheet, "outliers_tested": cov.fitted.get(a.analysis_id, False)}
                     for a in cov.analyses],
        "points": [{"uid": p.uid, "name": p.name,
                    "cells": {str(a.analysis_id): cov.cell(p.uid, a.analysis_id) for a in cov.analyses}}
                   for p in cov.points],
        "common": cov.common(),
        "repeated_outliers": cov.repeated_outliers(),
    }
    return json.dumps(data, ensure_ascii=False, indent=1)
