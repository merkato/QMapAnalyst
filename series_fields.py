"""
Seria czasowa a rektyfikacja (plan 0.3.2, etap 5) – bez Qt.

* Spójność punktu w serii: punkt o dużej reszcie na WSZYSTKICH mapach (względem RMSE danej mapy), zwłaszcza
  w zgodnym kierunku, sugeruje błąd współrzędnych w bibliotece albo zmianę obiektu, a nie błąd map.
* Porównanie pól korekcji dwóch map (#5): wygładzony TPS reszt każdej mapy (rectify.py) na regularnej siatce
  we wspólnym obszarze (przecięcie otoczek punktów obu map); miary: korelacja wektorowa pól, korelacja
  lokalnej dylatacji i obrotu (pochodne pola), statystyka Mantela (korelacja macierzy różnic pól między
  węzłami), wektory różnic pól. Istotność: test permutacyjny przez losowy OBRÓT pola drugiej mapy wokół
  środka wspólnego obszaru (położenia i wektory) – zachowuje autokorelację przestrzenną pola, więc nie daje
  fałszywej istotności dla dwóch gładkich, niezależnych pól (permutacja węzłów w klasycznym teście Mantela ją daje).
"""

import math
from dataclasses import dataclass, field

import numpy as np

from . import rectify as rc
from . import series as sr

MIN_FIELD_POINTS = 6
MIN_GRID_NODES = 20


# ---------------------------------------------------------------------------
#  Spójność punktu w serii
# ---------------------------------------------------------------------------
@dataclass
class SuspectPoint:
    uid: str
    name: str
    maps: int
    ratio: float                                   # średni |błąd| / RMSE mapy
    coherence: float                               # zgodność kierunku błędu (0 – losowy, 1 – ten sam)
    mean_m: float


def suspect_points(ser, min_maps=3, factor=1.5, coherence_min=0.6):
    """Punkty z błędem > factor × RMSE mapy na każdej z co najmniej min_maps map; najpierw najbardziej spójne."""
    names = {p.uid: p.name for p in ser.points}
    out = []
    for p in ser.points:
        maps = [m for m in ser.maps if p.uid in m.err_m and m.fitted and math.isfinite(m.rmse_m) and m.rmse_m > 0]
        if len(maps) < min_maps:
            continue
        ratios = [m.err_m[p.uid] / m.rmse_m for m in maps]
        if min(ratios) < factor:
            continue
        units = []
        for m in maps:
            v = np.asarray(m.vec[p.uid], float)
            n = np.hypot(*v)
            if n > 0:
                units.append(v / n)
        coh = float(np.hypot(*np.mean(units, axis=0))) if units else 0.0
        if coh < coherence_min:
            continue
        out.append(SuspectPoint(p.uid, names.get(p.uid, ""), len(maps), float(np.mean(ratios)), coh,
                                float(np.mean([m.err_m[p.uid] for m in maps]))))
    out.sort(key=lambda s: (-s.coherence * s.ratio, s.name))
    return out


# ---------------------------------------------------------------------------
#  Pola korekcji
# ---------------------------------------------------------------------------
def map_field(m):
    """(P – położenia przewidziane, E – reszty, pole korekcji) mapy serii; None – za mało punktów."""
    uids = [u for u in m.vec if u in m.pos]
    if len(uids) < MIN_FIELD_POINTS:
        return None
    E = np.array([m.vec[u] for u in uids], float)
    P = np.array([m.pos[u] for u in uids], float) + E
    try:
        return P, E, rc.build_correction(P, E, "tps")
    except rc.RectifyError:
        return None


def _hull(pts):
    p = sorted(map(tuple, np.asarray(pts, float)))

    def half(seq):
        h = []
        for q in seq:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (q[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (q[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(q)
        return h
    return np.array(half(p)[:-1] + half(p[::-1])[:-1])


def _inside_convex(hull, X, Y):
    inside = np.ones(X.shape, bool)
    for (ax, ay), (bx, by) in zip(hull, np.roll(hull, -1, axis=0)):
        inside &= (bx - ax) * (Y - ay) - (by - ay) * (X - ax) >= 0
    return inside


@dataclass
class FieldComparison:
    a: int
    b: int
    nodes: int = 0
    r_field: float = float("nan")                  # korelacja wektorowa pól korekcji w węzłach
    r_dilation: float = float("nan")               # korelacja lokalnej dylatacji (div/2)
    r_rotation: float = float("nan")               # korelacja lokalnego obrotu (rot/2)
    p_field: float = float("nan")                 # istotność r_field (obrót pola)
    mantel_r: float = float("nan")
    mantel_p: float = float("nan")                # istotność mantel_r (obrót pola)
    grid: np.ndarray = None                        # węzły (n×2) w układzie biblioteki
    diff: np.ndarray = None                        # c_A − c_B w węzłach (jednostki układu)
    warning: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.nodes >= MIN_GRID_NODES and math.isfinite(self.mantel_r)


def _pearson(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _div_rot(corr, G, h):
    """Lokalna dylatacja i obrót pola korekcji (różnice centralne, krok h)."""
    ex, ey = np.array([h, 0.0]), np.array([0.0, h])
    cxp, cxm, cyp, cym = corr(G + ex), corr(G - ex), corr(G + ey), corr(G - ey)
    dudx, dvdx = (cxp - cxm)[:, 0] / (2 * h), (cxp - cxm)[:, 1] / (2 * h)
    dudy, dvdy = (cyp - cym)[:, 0] / (2 * h), (cyp - cym)[:, 1] / (2 * h)
    return 0.5 * (dudx + dvdy), 0.5 * (dvdx - dudy)


def mantel_r(A, B):
    """Statystyka Mantela: korelacja macierzy odległości cech węzłów (górne trójkąty)."""
    iu = np.triu_indices(len(A), 1)
    DA = np.sqrt(((A[:, None, :] - A[None, :, :]) ** 2).sum(-1))[iu]
    DB = np.sqrt(((B[:, None, :] - B[None, :, :]) ** 2).sum(-1))[iu]
    return _pearson(DA, DB)


def _rotated(corr, G, center, theta):
    """Pole obrócone o kąt theta wokół center: c'(g) = R · c(R⁻¹(g − center) + center)."""
    c, s = math.cos(theta), math.sin(theta)
    R = np.array([[c, -s], [s, c]])
    src = (G - center) @ R + center                 # R⁻¹ = Rᵀ: (G − c) Rᵀᵀ…
    return corr(src) @ R.T


def _vcorr(ca, cb):
    return sr.vector_correlation(ca[:, 0] + 1j * ca[:, 1], cb[:, 0] + 1j * cb[:, 1])


def rotation_test(corr_a, corr_b, G, hull_b, permutations=199, rng=None):
    """p dla korelacji wektorowej i statystyki Mantela: pole B obracane losowo wokół środka węzłów;
    obroty, przy których mniej niż połowa węzłów trafia w otoczkę punktów B, są losowane ponownie."""
    rng = rng or np.random.default_rng(12345)
    center = G.mean(axis=0)
    ca, cb = corr_a(G), corr_b(G)
    r0, m0 = _vcorr(ca, cb), mantel_r(ca, cb)
    hits_r = hits_m = done = tries = 0
    while done < permutations and tries < permutations * 20:
        tries += 1
        theta = rng.uniform(math.radians(30), math.radians(330))
        c, s = math.cos(theta), math.sin(theta)
        src = (G - center) @ np.array([[c, -s], [s, c]]) + center
        if _inside_convex(hull_b, src[:, 0], src[:, 1]).mean() < 0.5:
            continue
        cbr = _rotated(corr_b, G, center, theta)
        hits_r += _vcorr(ca, cbr) >= r0
        hits_m += mantel_r(ca, cbr) >= m0
        done += 1
    if not done:
        return r0, float("nan"), m0, float("nan")
    return r0, (hits_r + 1) / (done + 1), m0, (hits_m + 1) / (done + 1)


def compare_fields(ma, mb, grid_n=15, permutations=199, rng=None, fields=None):
    """Porównanie pól korekcji map ma i mb (SheetResult serii) we wspólnym obszarze."""
    res = FieldComparison(ma.key, mb.key)
    fa = (fields or {}).get(ma.key) or map_field(ma)
    fb = (fields or {}).get(mb.key) or map_field(mb)
    if fa is None or fb is None:
        res.warning = "too_few_points"
        return res
    ha, hb = _hull(fa[0]), _hull(fb[0])
    lo = np.maximum(ha.min(axis=0), hb.min(axis=0))
    hi = np.minimum(ha.max(axis=0), hb.max(axis=0))
    if (hi <= lo).any():
        res.warning = "no_common_area"
        return res
    xs, ys = np.linspace(lo[0], hi[0], grid_n), np.linspace(lo[1], hi[1], grid_n)
    X, Y = np.meshgrid(xs, ys)
    keep = _inside_convex(ha, X, Y) & _inside_convex(hb, X, Y)
    G = np.column_stack([X[keep], Y[keep]])
    res.nodes, res.grid = len(G), G
    if res.nodes < MIN_GRID_NODES:
        res.warning = "small_common_area"
        return res
    res.diff = fa[2](G) - fb[2](G)
    h = 0.25 * float(min(xs[1] - xs[0], ys[1] - ys[0]))
    da, ra = _div_rot(fa[2], G, h)
    db, rb = _div_rot(fb[2], G, h)
    res.r_dilation, res.r_rotation = _pearson(da, db), _pearson(ra, rb)
    res.r_field, res.p_field, res.mantel_r, res.mantel_p = rotation_test(fa[2], fb[2], G, hb, permutations, rng)
    return res


def compare_series_fields(ser, grid_n=15, permutations=199):
    """Porównania pól korekcji dla wszystkich par map serii (wcześniejsza, późniejsza)."""
    fields = {m.key: map_field(m) for m in ser.maps}
    rng = np.random.default_rng(12345)
    out = []
    for i, ma in enumerate(ser.maps):
        for mb in ser.maps[i + 1:]:
            out.append(compare_fields(ma, mb, grid_n, permutations, rng, fields))
    return out
