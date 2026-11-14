"""
Tryb serii: porównanie map zestawu punktów biblioteki – liczone na żądanie z zapisanych arkuszy
(bez przechowywania wyników).

Dla każdego arkusza (analizy) z punktami zestawu: wyrównanie jednym modelem, statystyki (σ₀, RMSE, błąd
maksymalny, skala, obrót, różnica skal osi, odstające) i wektory błędu położenia punktów:
e = T(skan) − odniesienie, czyli gdzie mapa umieszcza obiekt względem jego prawdziwego położenia (po
usunięciu transformacji). Wektory są przeliczone do układu biblioteki, długości – w metrach.

Podobieństwo reszt pary arkuszy (ślad kopiowania treści i jej błędów): współczynnik korelacji wektorowej
    r = Re Σ zA·conj(zB) / √(Σ|zA|² · Σ|zB|²)   (z – wektor błędu jako liczba zespolona),
r ∈ [−1, 1]; r ≈ 1 – błędy punktów wspólnych mają ten sam kierunek i proporcje. Istotność – test
permutacyjny (losowe przyporządkowanie punktów). Wspólny błąd współrzędnych odniesienia punktu (lub
przesunięcie obiektu) daje podobieństwo wszystkich par – wtedy dotyczy on wszystkich map, nie jednej pary.

Moduł bez zależności od QGIS.
"""

import csv
import io
import json
import math
from dataclasses import dataclass, field

import numpy as np

from . import coverage as cv
from . import library as lb
from . import math_engine as me

MIN_COMMON = 4            # minimalna liczba punktów wspólnych do porównania pary arkuszy
SIMILAR_R = 0.5           # próg podobieństwa reszt
SIMILAR_P = 0.05          # próg istotności testu permutacyjnego
PERMUTATIONS = 999


@dataclass
class SheetResult:
    analysis: lb.Analysis
    column: object                                   # coverage.Column (etykieta, lata, arkusz)
    n: int = 0                                       # punkty obliczeniowe
    fitted: bool = False
    m_per_unit: float = float("nan")                 # metry na jednostkę układu analizy (nan – geograficzny)
    sigma0_m: float = float("nan")
    rmse_m: float = float("nan")
    max_m: float = float("nan")
    scale_den: float = float("nan")                  # mianownik skali (gdy znane DPI)
    rotation_deg: float = float("nan")
    axis_ratio: float = float("nan")                 # skala X / skala Y
    shear_deg: float = float("nan")
    outliers: list = field(default_factory=list)     # uid punktów odstających (test Baardy)
    pos: dict = field(default_factory=dict)          # uid -> (x, y) odniesienia w układzie biblioteki
    vec: dict = field(default_factory=dict)          # uid -> (dx, dy) błąd położenia w jednostkach biblioteki
    err_m: dict = field(default_factory=dict)        # uid -> |błąd| [m]

    @property
    def key(self):
        return self.analysis.analysis_id


@dataclass
class PairResult:
    a: int                                           # klucz arkusza wcześniejszego
    b: int
    n: int
    r: float
    p: float
    diff_m: float                                    # średnia |eA − eB| [m]

    @property
    def similar(self):
        return self.n >= MIN_COMMON and self.r >= SIMILAR_R and self.p < SIMILAR_P


@dataclass
class Series:
    set_id: int
    set_name: str
    model: str
    points: list = field(default_factory=list)
    sheets: list = field(default_factory=list)       # SheetResult wg roku
    pairs: list = field(default_factory=list)        # PairResult dla par arkuszy (wcześniejszy, późniejszy)
    current_id: int = None

    def sheet(self, key):
        return next((s for s in self.sheets if s.key == key), None)

    def point_summary(self, uid):
        """(liczba arkuszy, średni |błąd| [m], wypadkowy wektor średni (dx, dy)) punktu."""
        errs = [s.err_m[uid] for s in self.sheets if uid in s.err_m]
        vecs = [s.vec[uid] for s in self.sheets if uid in s.vec]
        if not errs:
            return 0, float("nan"), (float("nan"), float("nan"))
        return len(errs), float(np.mean(errs)), (float(np.mean([v[0] for v in vecs])), float(np.mean([v[1] for v in vecs])))

    def trajectory(self, uid):
        """[(SheetResult, (x, y) położenie odniesienia, (dx, dy) błąd)] punktu w kolejności arkuszy."""
        return [(s, s.pos[uid], s.vec[uid]) for s in self.sheets if uid in s.vec]

    def similar_pairs(self):
        return [p for p in self.pairs if p.similar]


# ----------------------------------------------------------------------
#  Obliczenia
# ----------------------------------------------------------------------
def _sheet(a, links, members, model, to_lib, lib_unit_m):
    col = cv._column_for(a, cv.BY_SHEET, {})
    res = SheetResult(a, col, m_per_unit=lb.crs_metres_per_unit(a.ref_crs) if a.ref_crs else lib_unit_m)
    fit, idx = cv._fit(links, model)
    res.n = len(idx)
    if fit is None:
        return res
    res.fitted = True
    mpu = res.m_per_unit
    res.sigma0_m = fit.sigma0 * mpu
    res.rmse_m = fit.stats["rmse"] * mpu
    res.max_m = float(np.max(fit.errors)) * mpu if len(fit.errors) else float("nan")
    d = fit.derived
    res.rotation_deg = d["rotation_deg"]
    res.axis_ratio = d["scale_x"] / d["scale_y"] if d["scale_y"] else float("nan")
    res.shear_deg = d["shear_deg"]
    if a.dpi:
        res.scale_den = me.scale_denominator(d["scale"] * mpu, a.dpi)
    outliers = cv._outliers(fit, idx, model) or set()
    for j, k in enumerate(idx):
        ln = links[k]
        if ln.uid not in members:
            continue
        ex, ey = -fit.residuals[j]                       # T(skan) − odniesienie
        try:
            x0, y0 = to_lib(ln.ref_x, ln.ref_y) if to_lib else (ln.ref_x, ln.ref_y)
            x1, y1 = to_lib(ln.ref_x + ex, ln.ref_y + ey) if to_lib else (ln.ref_x + ex, ln.ref_y + ey)
        except RuntimeError:
            continue
        res.pos[ln.uid] = (x0, y0)
        res.vec[ln.uid] = (x1 - x0, y1 - y0)
        res.err_m[ln.uid] = math.hypot(ex, ey) * mpu
        if k in outliers:
            res.outliers.append(ln.uid)
    return res


def vector_correlation(za, zb):
    """r = Re Σ zA·conj(zB) / √(Σ|zA|²·Σ|zB|²) dla wektorów zespolonych."""
    den = math.sqrt(float(np.sum(np.abs(za) ** 2) * np.sum(np.abs(zb) ** 2)))
    return float(np.real(np.sum(za * np.conj(zb)))) / den if den > 0 else float("nan")


def compare_pair(sa, sb, unit_m=1.0, rng=None, permutations=PERMUTATIONS):
    """Podobieństwo błędów punktów wspólnych pary arkuszy; unit_m – metry na jednostkę układu biblioteki."""
    common = [u for u in sa.vec if u in sb.vec]
    n = len(common)
    if n < MIN_COMMON:
        return PairResult(sa.key, sb.key, n, float("nan"), float("nan"), float("nan"))
    za = np.array([complex(*sa.vec[u]) for u in common])
    zb = np.array([complex(*sb.vec[u]) for u in common])
    r = vector_correlation(za, zb)
    rng = rng if rng is not None else np.random.default_rng(0)
    hits = sum(1 for _ in range(permutations) if vector_correlation(za, rng.permutation(zb)) >= r)
    p = (hits + 1) / (permutations + 1)
    return PairResult(sa.key, sb.key, n, r, p, float(np.mean(np.abs(za - zb))) * unit_m)


def build_series(lib, set_id, model="affine", analysis_ids=None, current=None, permutations=PERMUTATIONS):
    """Seria map zestawu: arkusze z punktami zestawu (wg roku), ich statystyki, błędy punktów i podobieństwo par.
    current = (Analysis lub None, [Link]) – stan bieżący otwartej analizy (jak w coverage.build_coverage)."""
    sets = {sid: name for sid, name, _d, _n in lib.sets()}
    if set_id not in sets:
        raise lb.LibraryError(f"no set {set_id}")
    points = sorted(lib.points(set_id), key=lambda p: lb.normalize(p.name))
    members = {p.uid for p in points}
    ser = Series(set_id, sets[set_id], model, points)
    entries, ser.current_id = cv._entries(lib, current)
    lib_crs = lib.crs_wkt
    lib_unit_m = lb.crs_metres_per_unit(lib_crs)
    transformers = {}
    for a, links in sorted(entries, key=lambda e: cv._year_key(cv._column_for(e[0], cv.BY_SHEET, {}))):
        if analysis_ids is not None and a.analysis_id not in analysis_ids and a.analysis_id != ser.current_id:
            continue
        if links is None:
            links = lib.links(a.analysis_id)
        if not any(ln.uid in members for ln in links):
            continue
        if a.ref_crs not in transformers:
            transformers[a.ref_crs] = lb.crs_transformer(a.ref_crs, lib_crs)
        ser.sheets.append(_sheet(a, links, members, model, transformers[a.ref_crs], lib_unit_m))
    rng = np.random.default_rng(12345)
    for i, sa in enumerate(ser.sheets):
        for sb in ser.sheets[i + 1:]:
            ser.pairs.append(compare_pair(sa, sb, lib_unit_m, rng, permutations))
    return ser


# ----------------------------------------------------------------------
#  Eksport
# ----------------------------------------------------------------------
def _num(v, nd=2):
    return "" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:.{nd}f}"


def sheets_csv(ser):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["analysis_id", "map", "sheet", "year_from", "year_to", "points", "sigma0_m", "rmse_m", "max_m",
                "scale_den", "rotation_deg", "axis_ratio", "shear_deg", "outliers"])
    for s in ser.sheets:
        a = s.analysis
        w.writerow([a.analysis_id, a.title, a.sheet, a.year_from or "", a.year_to or "", s.n, _num(s.sigma0_m),
                    _num(s.rmse_m), _num(s.max_m), _num(s.scale_den, 0), _num(s.rotation_deg, 3),
                    _num(s.axis_ratio, 4), _num(s.shear_deg, 3), len(s.outliers)])
    return out.getvalue()


def points_csv(ser):
    """Błędy położenia punktów [m] – wiersze: punkty, kolumny: arkusze."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["uid", "name"] + [cv.analysis_label(s.column) for s in ser.sheets] + ["maps", "mean_m"])
    for p in ser.points:
        n, mean, _v = ser.point_summary(p.uid)
        w.writerow([p.uid, p.name] + [_num(s.err_m.get(p.uid)) for s in ser.sheets] + [n, _num(mean)])
    return out.getvalue()


def to_json(ser):
    def clean(v):
        return None if isinstance(v, float) and not math.isfinite(v) else v

    data = {
        "set": {"id": ser.set_id, "name": ser.set_name},
        "model": ser.model,
        "sheets": [{"analysis_id": s.key, "title": s.analysis.title, "sheet": s.analysis.sheet,
                    "year_from": s.analysis.year_from, "year_to": s.analysis.year_to, "points": s.n,
                    "sigma0_m": clean(s.sigma0_m), "rmse_m": clean(s.rmse_m), "max_m": clean(s.max_m),
                    "scale_den": clean(s.scale_den), "rotation_deg": clean(s.rotation_deg),
                    "axis_ratio": clean(s.axis_ratio), "shear_deg": clean(s.shear_deg), "outliers": s.outliers,
                    "errors": {u: {"m": round(s.err_m[u], 3), "dx": s.vec[u][0], "dy": s.vec[u][1]} for u in s.vec}}
                   for s in ser.sheets],
        "pairs": [{"a": p.a, "b": p.b, "common": p.n, "r": clean(p.r), "p": clean(p.p), "diff_m": clean(p.diff_m),
                   "similar": p.similar} for p in ser.pairs],
    }
    return json.dumps(data, ensure_ascii=False, indent=1)
