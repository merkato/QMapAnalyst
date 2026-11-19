"""
Wyrównanie blokowe arkuszy wydawnictwa wieloarkuszowego (#8) – część analityczna.

Transformacja na arkusz (Helmert, afiniczna albo biliniowa w ramce), obserwacje:
* punkty dopasowania (GCP) każdego arkusza – w układzie biblioteki,
* punkty wiążące – ten sam obiekt na dwóch arkuszach (T_a(p_a) = T_b(p_b)),
* warunki zszycia – wspólne narożniki ramek sąsiednich arkuszy (T_a(c_a) = T_b(c_b)) z wagą z suwaka
  „niezależne ↔ zszyte”.
Wyniki: σ₀ całości i arkuszy, reszty standaryzowane (test Baardy) dla GCP i punktów wiążących,
szczeliny na stykach, arkusz odstający, krzywa RMSE ↔ szczelina.

Układ arkuszy w siatce (wiersz, kolumna) wykrywany po wstępnym wpasowaniu afinicznym (sąsiedztwo ramek).

Moduł bez zależności od QGIS.
"""

import math
from dataclasses import dataclass, field

import numpy as np

from . import math_engine as me
from . import rectify as rc

BLOCK_MODELS = {"helmert": 4, "affine": 6, "bilinear": 8}
TIE_WEIGHT = 1.0
SEAM_SLIDER_DEFAULT = 75


class BlockError(ValueError):
    pass


def slider_to_weight(pos):
    """Suwak 0…100 → waga warunków zszycia: 0 – arkusze niezależne, 100 – styki domknięte (w = 100)."""
    pos = min(max(float(pos), 0.0), 100.0)
    return 0.0 if pos <= 0 else float(10 ** (-2 + 4 * pos / 100.0))


@dataclass
class BlockSheet:
    key: int                                   # analysis_id (albo indeks)
    label: str
    frame: list                                # 4 rogi ramki na skanie (dowolna kolejność)
    src: np.ndarray                            # GCP na skanie (n×2)
    dst: np.ndarray                            # GCP w układzie biblioteki (n×2)
    uids: list = field(default_factory=list)
    names: list = field(default_factory=list)
    grid: tuple = None                         # (wiersz, kolumna) – wykryty albo zapisany
    analysis: object = None                    # library.Analysis (skan, priorytet, zapisana siatka)
    grid_saved: bool = False                   # siatka z biblioteki (nie wykryta)

    def __post_init__(self):
        self.frame = rc.order_corners(self.frame)
        f = np.asarray(self.frame, float)
        self.c = f.mean(axis=0)
        self.s = float(np.sqrt(((f - self.c) ** 2).sum(axis=1).mean())) or 1.0
        self.src = np.asarray(self.src, float).reshape(-1, 2)
        self.dst = np.asarray(self.dst, float).reshape(-1, 2)


def _basis(model, sheet, pts):
    """Wiersze bazowe (dla X i Y) w lokalnych parametrach arkusza: (n, 2, k)."""
    t = (np.atleast_2d(np.asarray(pts, float)) - sheet.c) / sheet.s
    n = len(t)
    k = BLOCK_MODELS[model]
    B = np.zeros((n, 2, k))
    x, y = t[:, 0], t[:, 1]
    if model == "helmert":                     # X = tx + a x − b y ; Y = ty + b x + a y
        B[:, 0, 0], B[:, 0, 2], B[:, 0, 3] = 1, x, -y
        B[:, 1, 1], B[:, 1, 2], B[:, 1, 3] = 1, y, x
    else:
        terms = [np.ones(n), x, y] + ([x * y] if model == "bilinear" else [])
        h = len(terms)
        for j, tj in enumerate(terms):
            B[:, 0, j] = tj
            B[:, 1, h + j] = tj
    return B


@dataclass
class SeamGap:
    a: int                                     # indeks arkusza
    b: int
    kind: str                                  # "corner" | "tie"
    at: tuple                                  # położenie (układ biblioteki)
    vec: tuple                                 # T_a − T_b
    gap: float                                 # długość [jednostki układu]
    name: str = ""


@dataclass
class BlockResult:
    model: str
    seam_weight: float
    sheets: list
    params: list                               # parametry na arkusz
    sigma0: float = float("nan")
    rmse: float = float("nan")
    redundancy: int = 0
    sheet_sigma0: list = field(default_factory=list)
    sheet_rmse: list = field(default_factory=list)
    gcp_residuals: list = field(default_factory=list)       # na arkusz: (n×2) T(src) − dst
    gcp_std: list = field(default_factory=list)             # na arkusz: maks. |w| punktu
    ties: list = field(default_factory=list)                # (ia, pa, ib, pb, nazwa)
    tie_std: list = field(default_factory=list)
    gaps: list = field(default_factory=list)                # SeamGap
    outlier_sheets: list = field(default_factory=list)
    sheet_offset: list = field(default_factory=list)       # przesunięcie arkusza względem sąsiadów [m]

    def transform(self, i, pts):
        B = _basis(self.model, self.sheets[i], pts)
        return np.einsum("nck,k->nc", B, self.params[i])

    def corner_gaps(self):
        return [g for g in self.gaps if g.kind == "corner"]

    def gap_stats(self):
        g = [x.gap for x in self.corner_gaps()]
        return (float(np.mean(g)), float(np.max(g))) if g else (float("nan"), float("nan"))

    def footprint(self, i):
        return self.transform(i, rc.frame_ring(self.sheets[i].frame, 24))


# ---------------------------------------------------------------------------
#  Układ arkuszy
# ---------------------------------------------------------------------------
def _initial(sheet):
    if len(sheet.src) < 3:
        return None
    try:
        return me.fit_transform("affine", sheet.src, sheet.dst)
    except me.EngineError:
        return None


def detect_grid(sheets):
    """(wiersz, kolumna) arkuszy z wstępnego wpasowania afinicznego: środki ramek w układzie obróconym
    o średni obrót arkuszy, podzielone przez medianę szerokości i wysokości. Arkusze bez ≥ 3 GCP – None."""
    fits = [_initial(s) for s in sheets]
    centers, sizes, rots = {}, [], []
    for i, (s, f) in enumerate(zip(sheets, fits)):
        if f is None:
            continue
        q = f.predict(np.asarray(s.frame, float))
        centers[i] = q.mean(axis=0)
        sizes.append((np.hypot(*(q[1] - q[0])), np.hypot(*(q[3] - q[0]))))
        rots.append(math.atan2(q[1][1] - q[0][1], q[1][0] - q[0][0]))
    if not centers:
        return {}
    rot = math.atan2(np.mean(np.sin(rots)), np.mean(np.cos(rots)))
    w, h = float(np.median([s[0] for s in sizes])), float(np.median([s[1] for s in sizes]))
    R = np.array([[math.cos(-rot), -math.sin(-rot)], [math.sin(-rot), math.cos(-rot)]])
    local = {i: R @ c for i, c in centers.items()}
    x0 = min(v[0] for v in local.values())
    y0 = max(v[1] for v in local.values())
    return {i: (int(round((y0 - v[1]) / h)), int(round((v[0] - x0) / w))) for i, v in local.items()}


def nodes_from_grid(sheets):
    """Węzły siatki: {(wiersz, kol.): [(indeks arkusza, narożnik)]} – narożniki TL, TR, BR, BL."""
    nodes = {}
    for i, s in enumerate(sheets):
        if s.grid is None:
            continue
        r, c = s.grid
        for k, (dr, dc) in enumerate(((0, 0), (0, 1), (1, 1), (1, 0))):
            nodes.setdefault((r + dr, c + dc), []).append((i, k))
    return nodes


CORNER_TOL = 0.25                              # zszywane narożniki: do 25 % mediany boku arkusza


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, a):
        while self.parent.setdefault(a, a) != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)

    def groups(self):
        out = {}
        for key in list(self.parent):
            out.setdefault(self.find(key), []).append(key)
        return list(out.values())


def _initial_corners(sheets):
    """Narożniki ramek po wstępnym wpasowaniu afinicznym {arkusz: 4×2} i mediana boku arkusza."""
    corners = {}
    for i, s in enumerate(sheets):
        f = _initial(s)
        if f is not None:
            corners[i] = f.predict(np.asarray(s.frame, float))
    sides = [np.hypot(*(q[1] - q[0])) for q in corners.values()] + [np.hypot(*(q[3] - q[0])) for q in corners.values()]
    return corners, (float(np.median(sides)) if sides else 0.0)


def _link_close(uf, a, ca, b, cb, limit):
    """Łączy narożniki dwóch arkuszy, które są dla siebie nawzajem najbliższe i bliższe niż limit."""
    d = np.hypot(*(ca[:, None, :] - cb[None, :, :]).transpose(2, 0, 1))      # 4×4
    for k in range(4):
        m = int(np.argmin(d[k]))
        if d[k, m] < limit and int(np.argmin(d[:, m])) == k:
            uf.union((a, k), (b, m))


def corner_nodes(sheets, tol=CORNER_TOL):
    """Węzły zszycia: narożniki ramek, które po wstępnym wpasowaniu afinicznym każdego arkusza leżą blisko
    siebie (< tol × mediana boku arkusza) – niezależnie od siatki, więc działa też dla arkuszy zachodzących
    na siebie, przesuniętych o pół arkusza („cegiełka”) i źle przypisanych do siatki. Arkusz bez wstępnego
    wpasowania (< 3 GCP) łączy się wg siatki (nodes_from_grid). Zwraca {węzeł: [(arkusz, narożnik)]}."""
    corners, side = _initial_corners(sheets)
    uf = _UnionFind()
    keys = sorted(corners)
    for x, a in enumerate(keys):
        for b in keys[x + 1:]:
            _link_close(uf, a, corners[a], b, corners[b], tol * side)
    for owners in nodes_from_grid(sheets).values():                          # arkusze bez wpasowania
        for o in owners[1:]:
            if o[0] not in corners or owners[0][0] not in corners:
                uf.union(owners[0], o)
    return {n: sorted(g) for n, g in enumerate(uf.groups()) if len(g) > 1}


def ties_from_uids(sheets):
    """Punkty biblioteki wskazane na dwóch arkuszach → punkty wiążące (ia, pa, ib, pb, nazwa)."""
    where = {}
    for i, s in enumerate(sheets):
        for j, u in enumerate(s.uids):
            if u:
                where.setdefault(u, []).append((i, j))
    out = []
    for u, occ in where.items():
        for x in range(len(occ)):
            for y in range(x + 1, len(occ)):
                (ia, ja), (ib, jb) = occ[x], occ[y]
                name = sheets[ia].names[ja] if ja < len(sheets[ia].names) else u
                out.append((ia, tuple(sheets[ia].src[ja]), ib, tuple(sheets[ib].src[jb]), name))
    return out


# ---------------------------------------------------------------------------
#  Wyrównanie
# ---------------------------------------------------------------------------
def _pair_rows(model, sheets, offs, P, ia, pa, ib, pb):
    """Dwa wiersze warunku T_a(pa) − T_b(pb) = 0."""
    k = BLOCK_MODELS[model]
    rows = np.zeros((2, P))
    rows[:, offs[ia]:offs[ia] + k] += _basis(model, sheets[ia], [pa])[0]
    rows[:, offs[ib]:offs[ib] + k] -= _basis(model, sheets[ib], [pb])[0]
    return rows


def _design(model, sheets, ties, nodes, seam_weight, tie_weight):
    k = BLOCK_MODELS[model]
    offs = [i * k for i in range(len(sheets))]
    P = k * len(sheets)
    A, b, p, tag = [], [], [], []
    for i, s in enumerate(sheets):
        if len(s.src):
            B = _basis(model, s, s.src)
            rows = np.zeros((len(s.src) * 2, P))
            rows[:, offs[i]:offs[i] + k] = B.reshape(-1, k)
            A.append(rows)
            b.append(s.dst.reshape(-1))
            p.append(np.ones(len(s.src) * 2))
            tag += [("gcp", i, j) for j in range(len(s.src)) for _ in (0, 1)]
    for t, (ia, pa, ib, pb, _n) in enumerate(ties):
        A.append(_pair_rows(model, sheets, offs, P, ia, pa, ib, pb))
        b.append(np.zeros(2))
        p.append(np.full(2, tie_weight))
        tag += [("tie", t, 0)] * 2
    corners = [(owners[x], owners[y]) for owners in nodes.values()
               for x in range(len(owners)) for y in range(x + 1, len(owners))]
    if seam_weight > 0:
        for (ia, ka), (ib, kb) in corners:
            A.append(_pair_rows(model, sheets, offs, P, ia, sheets[ia].frame[ka], ib, sheets[ib].frame[kb]))
            b.append(np.zeros(2))
            p.append(np.full(2, seam_weight))
            tag += [("seam", 0, 0)] * 2
    return np.vstack(A), np.concatenate(b), np.concatenate(p), tag, offs, corners


def adjust(sheets, model="bilinear", seam_weight=None, ties=None, nodes=None, tie_weight=TIE_WEIGHT, unit_m=1.0,
           check_sheets=True):
    """Wyrównanie blokowe. seam_weight=None – z domyślnej pozycji suwaka; ties/nodes=None – z uid i siatki.
    check_sheets – test arkusza odstającego (każdy arkusz przewidziany z pozostałych)."""
    if model not in BLOCK_MODELS:
        raise BlockError(f"unknown model {model}")
    seam_weight = slider_to_weight(SEAM_SLIDER_DEFAULT) if seam_weight is None else float(seam_weight)
    ties = ties_from_uids(sheets) if ties is None else ties
    nodes = corner_nodes(sheets) if nodes is None else nodes
    A, b, p, tag, offs, corners = _design(model, sheets, ties, nodes, seam_weight, tie_weight)
    N = A.T @ (A * p[:, None])
    if np.linalg.matrix_rank(N) < N.shape[0]:
        raise BlockError("układ nieoznaczony – arkusz bez punktów dopasowania, punktów wiążących i sąsiadów")
    Qxx = np.linalg.inv(N)
    x = Qxx @ (A.T @ (b * p))
    k = BLOCK_MODELS[model]
    res = BlockResult(model, seam_weight, sheets, [x[o:o + k] for o in offs])
    _statistics(res, A, b, p, tag, Qxx, x, unit_m)
    res.ties = ties
    _gaps(res, corners, ties)
    if check_sheets:
        res.sheet_offset = sheet_offsets(sheets, model, seam_weight, ties, nodes, tie_weight, unit_m)
        res.outlier_sheets = find_outlier_sheets(sheets, res.sheet_offset, res.sheet_rmse, model, seam_weight, ties,
                                                 nodes, tie_weight, unit_m)
    return res


def _offset(sheets, i, removed, model, seam_weight, ties, nodes, tie_weight, unit_m):
    """Średnie przesunięcie GCP arkusza i względem położenia przewidzianego bez punktów arkuszy `removed`."""
    s = sheets[i]
    if not len(s.src):
        return float("nan")
    reduced = list(sheets)
    for k in removed:
        t = sheets[k]
        reduced[k] = BlockSheet(t.key, t.label, t.frame, np.zeros((0, 2)), np.zeros((0, 2)), grid=t.grid)
    try:
        r = adjust(reduced, model, seam_weight, ties, nodes, tie_weight, unit_m, check_sheets=False)
    except BlockError:
        return float("nan")
    d = (r.transform(i, s.src) - s.dst).mean(axis=0)
    return float(np.hypot(*d)) * unit_m


def sheet_offsets(sheets, model, seam_weight, ties, nodes, tie_weight=TIE_WEIGHT, unit_m=1.0):
    """Średnie przesunięcie GCP arkusza względem jego położenia przewidzianego z pozostałych arkuszy
    (punkty arkusza pominięte, warunki zszycia i punkty wiążące zostają) [m]; nan – arkusz bez punktów
    albo bez połączenia z sąsiadami."""
    if seam_weight <= 0 and not ties:
        return [float("nan")] * len(sheets)
    return [_offset(sheets, i, [i], model, seam_weight, ties, nodes, tie_weight, unit_m) for i in range(len(sheets))]


def find_outlier_sheets(sheets, offsets, rmse, model, seam_weight, ties, nodes, tie_weight=TIE_WEIGHT, unit_m=1.0,
                        factor=3.0):
    """Arkusze odstające, wykrywane po kolei: kandydat o największym przesunięciu jest odstający, gdy przekracza
    factor × max(mediana przesunięć pozostałych arkuszy policzonych BEZ kandydata, RMSE kandydata)."""
    out = []
    good = [i for i, o in enumerate(offsets) if math.isfinite(o)]
    while len(good) - len(out) >= 3 and len(out) < max(1, len(good) // 3):
        cand = max((i for i in good if i not in out), key=lambda i: offsets[i])
        others = [_offset(sheets, j, [j, cand] + out, model, seam_weight, ties, nodes, tie_weight, unit_m)
                  for j in good if j != cand and j not in out]
        others = [o for o in others if math.isfinite(o)]
        if not others:
            break
        ref = max(float(np.median(others)), rmse[cand] if math.isfinite(rmse[cand]) else 0.0)
        if offsets[cand] <= factor * ref:
            break
        out.append(cand)
    return out


def _statistics(res, A, b, p, tag, Qxx, x, unit_m):
    v = A @ x - b
    nobs = int((p > 0).sum())
    res.redundancy = nobs - len(x)
    vpv = float((p * v * v).sum())
    res.sigma0 = math.sqrt(vpv / res.redundancy) if res.redundancy > 0 else float("nan")
    qvv = 1.0 / p - np.einsum("ij,jk,ik->i", A, Qxx, A)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = v / (res.sigma0 * np.sqrt(np.clip(qvv, 1e-12, None)))
        rn = np.clip(qvv * p, 0, 1)
    n = len(res.sheets)
    res.gcp_residuals = [np.zeros((len(s.src), 2)) for s in res.sheets]
    res.gcp_std = [np.zeros(len(s.src)) for s in res.sheets]
    res.tie_std = []
    sheet_vpv, sheet_r = np.zeros(n), np.zeros(n)
    for row, (kind, i, j) in enumerate(tag):
        if kind == "gcp":
            res.gcp_residuals[i][j, row % 2] = v[row]
            res.gcp_std[i][j] = max(res.gcp_std[i][j], abs(w[row]))
            sheet_vpv[i] += p[row] * v[row] ** 2
            sheet_r[i] += rn[row]
        elif kind == "tie":
            if len(res.tie_std) <= i:
                res.tie_std.append(0.0)
            res.tie_std[i] = max(res.tie_std[i], abs(w[row]))
    all_v = np.vstack([r for r in res.gcp_residuals if len(r)]) if any(len(r) for r in res.gcp_residuals) else np.zeros((0, 2))
    res.rmse = float(np.sqrt((all_v ** 2).sum(1).mean())) * unit_m if len(all_v) else float("nan")
    res.sheet_rmse = [float(np.sqrt((r ** 2).sum(1).mean())) * unit_m if len(r) else float("nan") for r in res.gcp_residuals]
    res.sheet_sigma0 = [math.sqrt(sheet_vpv[i] / sheet_r[i]) * unit_m if sheet_r[i] > 0.5 else float("nan") for i in range(n)]
    res.sigma0 *= unit_m


def _gaps(res, corners, ties):
    out = []
    for (ia, ka), (ib, kb) in corners:
        pa, pb = res.transform(ia, [res.sheets[ia].frame[ka]])[0], res.transform(ib, [res.sheets[ib].frame[kb]])[0]
        d = pa - pb
        out.append(SeamGap(ia, ib, "corner", tuple((pa + pb) / 2), tuple(d), float(np.hypot(*d))))
    for ia, pa, ib, pb, name in ties:
        qa, qb = res.transform(ia, [pa])[0], res.transform(ib, [pb])[0]
        d = qa - qb
        out.append(SeamGap(ia, ib, "tie", tuple((qa + qb) / 2), tuple(d), float(np.hypot(*d)), name))
    res.gaps = out


def tradeoff(sheets, model="bilinear", positions=(0, 25, 40, 50, 60, 75, 90, 100), unit_m=1.0, **kw):
    """Krzywa „RMSE ↔ szczelina”: [(pozycja suwaka, waga, RMSE, szczelina średnia, maks.)] [m]."""
    out = []
    for pos in positions:
        try:
            r = adjust(sheets, model, slider_to_weight(pos), unit_m=unit_m, **kw)
        except BlockError:
            continue
        g_mean, g_max = r.gap_stats()
        out.append((pos, r.seam_weight, r.rmse, g_mean * unit_m, g_max * unit_m))
    return out


# ---------------------------------------------------------------------------
#  Arkusze z biblioteki
# ---------------------------------------------------------------------------
def sheets_from_library(lib, map_id, current=None):
    """Arkusze mapy map_id z ramką (bez ramki – cały skan nieznany, arkusz pominięty) i punktami obliczeniowymi
    w układzie biblioteki. current – stan bieżący (jak w coverage.build_coverage). Zwraca (arkusze, pominięte)."""
    from . import coverage as cv
    from . import library as lb
    entries, _cur = cv._entries(lib, current)
    lib_crs = lib.crs_wkt
    transformers, sheets, skipped = {}, [], []
    for a, links in entries:
        if a.map_id != map_id:
            continue
        frame = lb.frame_points(a.frame)
        if not frame or len(frame) != 4:
            skipped.append(a)
            continue
        if links is None:
            links = lib.links(a.analysis_id)
        if a.ref_crs not in transformers:
            transformers[a.ref_crs] = lb.crs_transformer(a.ref_crs, lib_crs)
        to_lib = transformers[a.ref_crs]
        use = [ln for ln in links if ln.computable and ln.enabled]
        dst = [to_lib(ln.ref_x, ln.ref_y) if to_lib else (ln.ref_x, ln.ref_y) for ln in use]
        label = a.sheet or f"#{a.analysis_id}"
        sheets.append(BlockSheet(a.analysis_id, label, frame, [(ln.x_old, ln.y_old) for ln in use], dst,
                                 [ln.uid or "" for ln in use], [ln.name or "" for ln in use], analysis=a))
    grid = detect_grid(sheets)
    saved = {i: (s.analysis.grid_row, s.analysis.grid_col) for i, s in enumerate(sheets)
             if s.analysis.grid_row is not None and s.analysis.grid_col is not None}
    shift = _grid_shift(grid, saved)
    for i, s in enumerate(sheets):
        if i in saved:
            s.grid, s.grid_saved = saved[i], True
        elif i in grid:
            s.grid = (grid[i][0] + shift[0], grid[i][1] + shift[1])
    return sheets, skipped


def _grid_shift(detected, saved):
    """Przesunięcie (wiersz, kolumna) wykrytej siatki względem zapisanej – najczęstsza różnica dla arkuszy, które
    mają oba położenia (siatka wykryta na nowo, gdy doszły arkusze, zaczyna się od innego wiersza/kolumny)."""
    diffs = [(saved[i][0] - detected[i][0], saved[i][1] - detected[i][1]) for i in saved if i in detected]
    if not diffs:
        return (0, 0)
    return max(set(diffs), key=diffs.count)
