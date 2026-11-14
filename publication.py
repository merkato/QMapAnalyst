"""
Wydawnictwo wieloarkuszowe: zgodność arkuszy jednej mapy – liczona na żądanie z zapisanych arkuszy.

* arkusze wyrównane jednym modelem: σ₀, RMSE, skala (z DPI), obrót, skale osi, odstające – i ich rozrzut
  w wydawnictwie (różne podkłady, rytownicy, skurcz papieru poszczególnych arkuszy),
* zakładki: punkt biblioteki wskazany na dwóch arkuszach – rozbieżność położeń, w których umieszczają go
  oba arkusze (po usunięciu transformacji każdego z nich) = |eA − eB|,
* zasięgi arkuszy (ramka mapy na skanie × transformacja arkusza) – skorowidz na mapie współczesnej.

Moduł bez zależności od QGIS.
"""

import csv
import io
import math
from dataclasses import dataclass, field

import numpy as np

from . import coverage as cv
from . import library as lb
from . import series as sr


@dataclass
class Overlap:
    uid: str
    name: str
    a: int                                           # analysis_id arkusza
    b: int
    diff_m: float                                    # rozbieżność położeń punktu na obu arkuszach [m]


@dataclass
class Publication:
    map: lb.MapInfo
    model: str
    sheets: list = field(default_factory=list)       # series.SheetResult (wg numeru arkusza)
    overlaps: list = field(default_factory=list)     # Overlap
    current_id: int = None                           # analysis_id arkusza ze stanem bieżącym

    def sheet(self, analysis_id):
        return next((s for s in self.sheets if s.analysis.analysis_id == analysis_id), None)

    def spread(self, attr):
        """(min, max) wartości atrybutu arkuszy wyrównanych (np. "scale_den", "rotation_deg") albo None."""
        vals = [getattr(s, attr) for s in self.sheets if s.fitted and math.isfinite(getattr(s, attr))]
        return (min(vals), max(vals)) if vals else None

    def scale_spread_pct(self):
        """Rozrzut skali arkuszy w % średniej (max − min) / średnia; nan – brak skali."""
        vals = [s.scale_den for s in self.sheets if s.fitted and math.isfinite(s.scale_den)]
        return 100.0 * (max(vals) - min(vals)) / float(np.mean(vals)) if len(vals) >= 2 else float("nan")


def _sheet_key(a):
    """Porządek arkuszy: numery liczbowe rosnąco, potem pozostałe alfabetycznie."""
    s = (a.sheet or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    return (0, int(digits), s) if digits and s[:1].isdigit() else (1, 0, s.lower())


def build_publication(lib, map_id, model="affine", current=None):
    """Arkusze mapy map_id, ich statystyki, zakładki i zasięgi. current – stan bieżący (jak w build_coverage);
    uwzględniany, gdy dotyczy arkusza tej mapy."""
    m = lib.get_map(map_id)
    if m is None:
        raise lb.LibraryError(f"no map {map_id}")
    pub = Publication(m, model)
    names = {p.uid: p.name for p in lib.points()}
    entries, cur_id = cv._entries(lib, current)
    lib_crs = lib.crs_wkt
    lib_unit_m = lb.crs_metres_per_unit(lib_crs)
    transformers = {}
    for a, links in entries:
        if a.map_id != map_id:
            continue
        if links is None:
            links = lib.links(a.analysis_id)
        if a.ref_crs not in transformers:
            transformers[a.ref_crs] = lb.crs_transformer(a.ref_crs, lib_crs)
        pub.sheets.append(sr._sheet(a, links, None, model, transformers[a.ref_crs], lib_unit_m))
        if a.analysis_id == cur_id:
            pub.current_id = a.analysis_id
    pub.sheets.sort(key=lambda s: (_sheet_key(s.analysis), s.analysis.analysis_id))
    pub.overlaps = _overlaps(pub.sheets, names, lib_unit_m)
    return pub


def _overlaps(sheets, names, unit):
    """Punkty wskazane na dwóch arkuszach: rozbieżność położeń |eA − eB| [m], od największej."""
    out = []
    for i, sa in enumerate(sheets):
        for sb in sheets[i + 1:]:
            for uid in (u for u in sa.vec if u in sb.vec):
                (ax, ay), (bx, by) = sa.vec[uid], sb.vec[uid]
                out.append(Overlap(uid, names.get(uid, ""), sa.analysis.analysis_id, sb.analysis.analysis_id,
                                   math.hypot(ax - bx, ay - by) * unit))
    out.sort(key=lambda o: -o.diff_m if math.isfinite(o.diff_m) else 0)
    return out


def sheets_csv(pub):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["analysis_id", "sheet", "points", "sigma0_m", "rmse_m", "max_m", "scale_den", "rotation_deg",
                "axis_ratio", "shear_deg", "outliers", "footprint"])
    for s in pub.sheets:
        w.writerow([s.analysis.analysis_id, s.analysis.sheet, s.n, sr._num(s.sigma0_m), sr._num(s.rmse_m),
                    sr._num(s.max_m), sr._num(s.scale_den, 0), sr._num(s.rotation_deg, 3), sr._num(s.axis_ratio, 4),
                    sr._num(s.shear_deg, 3), len(s.outliers), int(bool(s.footprint))])
    w.writerow([])
    w.writerow(["overlap_uid", "name", "sheet_a", "sheet_b", "diff_m"])
    sheet = {s.analysis.analysis_id: s.analysis.sheet for s in pub.sheets}
    for o in pub.overlaps:
        w.writerow([o.uid, o.name, sheet[o.a], sheet[o.b], sr._num(o.diff_m)])
    return out.getvalue()
