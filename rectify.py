"""
Silnik rektyfikacji: transformacja globalna + pole korekcji lokalnej (tylko numpy).

Rozdział „analiza ≠ rektyfikacja” (plan 0.3.2): analiza ocenia mapę modelem globalnym, a rektyfikacja
usuwa reszty w kontrolowany sposób, żeby raster pokrył się z mapą współczesną.

Pole korekcji C(p) jest określone w układzie odniesienia (terenu) na położeniach p przewidzianych przez
model globalny; wektor reszty e = p − odniesienie. Położenie po korekcji: p − C(p). Ponieważ C zależy tylko
od położenia w terenie, wspólna krawędź dwóch arkuszy (te same punkty p) przesuwa się w obu tak samo.

Metody:
* ``tps``       – wygładzony spline cienkiej płyty; wygładzanie λ z walidacji krzyżowej (LOO z macierzy
                  wpływu – jedno rozwiązanie układu) albo z suwaka,
* ``tps_exact`` – TPS interpolujący (jak Georeferencer QGIS),
* ``triangles`` – afiniczna w trójkątach Delaunaya (dokładnie w punktach, ciągła, bez fałd),
* ``mls``       – moving least squares (afiniczna ważona odległością, lokalnie sztywna),
* ``none``      – tylko model globalny.

Do przetworzenia rastra każda metoda jest zamieniana na gęstą siatkę GCP (skan → teren) i linię cięcia
wzdłuż ramki – wystarczy gdalwarp -tps.
"""

import math
from dataclasses import dataclass, field as dc_field

import numpy as np

METHODS = ("none", "tps", "tps_exact", "triangles", "mls")
LAMBDA_MIN, LAMBDA_MAX = 1e-6, 1e3          # zakres wygładzania (współrzędne znormalizowane)
AUTO_CANDIDATES = np.logspace(-6, 3, 28)


class RectifyError(ValueError):
    pass


# ---------------------------------------------------------------------------
#  Normalizacja
# ---------------------------------------------------------------------------
def _norm(pts):
    c = pts.mean(axis=0)
    s = float(np.sqrt(((pts - c) ** 2).sum(axis=1).mean())) or 1.0
    return c, s


def slider_to_lambda(pos):
    """Suwak 0…100 → λ (skala logarytmiczna): 0 – interpolacja, 100 – prawie model globalny."""
    t = min(max(float(pos), 0.0), 100.0) / 100.0
    return float(10 ** (math.log10(LAMBDA_MIN) + t * (math.log10(LAMBDA_MAX) - math.log10(LAMBDA_MIN))))


def lambda_to_slider(lam):
    lam = min(max(float(lam), LAMBDA_MIN), LAMBDA_MAX)
    return 100.0 * (math.log10(lam) - math.log10(LAMBDA_MIN)) / (math.log10(LAMBDA_MAX) - math.log10(LAMBDA_MIN))


# ---------------------------------------------------------------------------
#  Wygładzony TPS
# ---------------------------------------------------------------------------
def _tps_kernel(r2):
    with np.errstate(divide="ignore", invalid="ignore"):
        k = 0.5 * r2 * np.log(r2)
    return np.where(r2 > 0, k, 0.0)


def _sqdist(a, b):
    return ((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2)


class SmoothTPS:
    """Wygładzony TPS wektorów E (n×2) w punktach P (n×2); wagi w – większa waga = bliżej punktu."""

    def __init__(self, P, E, lam=0.0, w=None, norm=None):
        P, E = np.asarray(P, float), np.asarray(E, float)
        n = len(P)
        if n < 3:
            raise RectifyError("za mało punktów dla TPS")
        self.c, self.s = norm if norm is not None else _norm(P)
        U = (P - self.c) / self.s
        w = np.ones(n) if w is None else np.clip(np.asarray(w, float), 1e-6, None)
        K = _tps_kernel(_sqdist(U, U))
        Pm = np.column_stack([np.ones(n), U])
        A = np.zeros((n + 3, n + 3))
        A[:n, :n] = K + np.diag(lam / w)
        A[:n, n:] = Pm
        A[n:, :n] = Pm.T
        try:
            Ainv = np.linalg.inv(A)
        except np.linalg.LinAlgError:
            Ainv = np.linalg.pinv(A)
        rhs = np.vstack([E, np.zeros((3, 2))])
        coef = Ainv @ rhs
        self.U, self.a, self.b = U, coef[:n], coef[n:]
        # macierz wpływu: dopasowanie = H · E, H = [K P] · Ainv[:, :n]
        H = np.hstack([K, Pm]) @ Ainv[:, :n]
        self.fitted = H @ E
        h = np.clip(np.diag(H), None, 1 - 1e-9)
        self.loo = (E - self.fitted) / (1 - h)[:, None]       # reszty walidacji krzyżowej (dokładnie, bez n rozwiązań)
        self.lam = lam

    def __call__(self, Q):
        V = (np.atleast_2d(np.asarray(Q, float)) - self.c) / self.s
        return _tps_kernel(_sqdist(V, self.U)) @ self.a + np.column_stack([np.ones(len(V)), V]) @ self.b

    def loo_rmse(self):
        return float(np.sqrt((self.loo ** 2).sum(axis=1).mean()))


def choose_lambda(P, E, w=None, candidates=AUTO_CANDIDATES):
    """λ o najmniejszym błędzie walidacji krzyżowej; zwraca (λ, [(λ, LOO RMSE)])."""
    scores = []
    for lam in candidates:
        try:
            v = SmoothTPS(P, E, lam, w).loo_rmse()
        except (np.linalg.LinAlgError, ValueError, FloatingPointError):
            v = float("nan")                    # układ zdegenerowany dla tego λ – pomijamy
        if math.isfinite(v):
            scores.append((float(lam), v))
    if not scores:
        raise RectifyError("nie udało się dobrać wygładzania")
    best = min(scores, key=lambda t: t[1])
    return best[0], scores


# ---------------------------------------------------------------------------
#  Trójkąty Delaunaya (Bowyer–Watson) i interpolacja liniowa
# ---------------------------------------------------------------------------
def delaunay(P):
    """Lista trójkątów (i, j, k) – algorytm Bowyera–Watsona, wystarczający dla setek punktów."""
    P = np.asarray(P, float)
    n = len(P)
    if n < 3:
        return []
    c, s = _norm(P)
    U = (P - c) / s
    big = 1e3
    pts = np.vstack([U, [[-big, -big], [big, -big], [0.0, big]]])
    tris = [(n, n + 1, n + 2)]

    def circum(t):
        (ax, ay), (bx, by), (cx, cy) = pts[t[0]], pts[t[1]], pts[t[2]]
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-15:
            return (0.0, 0.0, float("inf"))
        ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
        return (ux, uy, (ax - ux) ** 2 + (ay - uy) ** 2)

    cache = {tris[0]: circum(tris[0])}
    for i in range(n):
        x, y = pts[i]
        bad = [t for t in tris if (x - cache[t][0]) ** 2 + (y - cache[t][1]) ** 2 < cache[t][2] * (1 - 1e-12)]
        edges = {}
        for t in bad:
            for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                key = tuple(sorted(e))
                edges[key] = edges.get(key, 0) + 1
        bad_set = set(bad)
        tris = [t for t in tris if t not in bad_set]
        for t in bad:
            cache.pop(t, None)
        for (a, b), cnt in edges.items():
            if cnt == 1:
                t = (a, b, i)
                tris.append(t)
                cache[t] = circum(t)
    return [t for t in tris if max(t) < n]


class Triangles:
    """Interpolacja liniowa wektorów E w trójkątach; poza otoczką – wartość z najbliższego trójkąta
    (współrzędne barycentryczne przycięte), czyli ciągłe przedłużenie bez wybuchu."""

    def __init__(self, P, E):
        self.P, self.E = np.asarray(P, float), np.asarray(E, float)
        self.tris = np.array(delaunay(self.P), int).reshape(-1, 3)
        if not len(self.tris):
            raise RectifyError("triangulacja nie powiodła się")

    def _bary(self, q):
        A, B, C = self.P[self.tris[:, 0]], self.P[self.tris[:, 1]], self.P[self.tris[:, 2]]
        v0, v1, v2 = B - A, C - A, q[None, :] - A
        d00, d01, d11 = (v0 * v0).sum(1), (v0 * v1).sum(1), (v1 * v1).sum(1)
        d20, d21 = (v2 * v0).sum(1), (v2 * v1).sum(1)
        den = d00 * d11 - d01 * d01
        den = np.where(np.abs(den) < 1e-30, 1e-30, den)
        v = (d11 * d20 - d01 * d21) / den
        w = (d00 * d21 - d01 * d20) / den
        return np.column_stack([1 - v - w, v, w])

    def __call__(self, Q):
        Q = np.atleast_2d(np.asarray(Q, float))
        out = np.zeros((len(Q), 2))
        for k, q in enumerate(Q):
            b = self._bary(q)
            inside = (b >= -1e-9).all(axis=1)
            if inside.any():
                t = int(np.argmax(inside))
                bb = b[t]
            else:
                bc = np.clip(b, 0, None)
                bc /= bc.sum(axis=1, keepdims=True)
                proj = (bc[:, :, None] * self.P[self.tris]).sum(axis=1)
                t = int(np.argmin(((proj - q) ** 2).sum(axis=1)))
                bb = bc[t]
            out[k] = bb @ self.E[self.tris[t]]
        return out


# ---------------------------------------------------------------------------
#  MLS – moving least squares (afiniczna, Schaefer i in. 2006)
# ---------------------------------------------------------------------------
class MLS:
    """Pole korekcji z lokalnie ważonej afinicznej P → P − E (alpha – wykładnik wagi 1/d^(2α))."""

    def __init__(self, P, E, alpha=1.0):
        self.P = np.asarray(P, float)
        self.Qt = self.P - np.asarray(E, float)
        self.alpha = alpha
        self.c, self.s = _norm(self.P)

    def __call__(self, Q):
        Q = np.atleast_2d(np.asarray(Q, float))
        U = (self.P - self.c) / self.s
        T = (self.Qt - self.c) / self.s
        out = np.zeros((len(Q), 2))
        for k, q in enumerate(Q):
            v = (q - self.c) / self.s
            d2 = ((U - v) ** 2).sum(1)
            if d2.min() < 1e-20:
                j = int(np.argmin(d2))
                out[k] = (q - (T[j] * self.s + self.c))
                continue
            w = 1.0 / d2 ** self.alpha
            ps, qs = (w[:, None] * U).sum(0) / w.sum(), (w[:, None] * T).sum(0) / w.sum()
            ph, qh = U - ps, T - qs
            M1 = (ph * w[:, None]).T @ ph
            M2 = (ph * w[:, None]).T @ qh
            try:
                M = np.linalg.solve(M1, M2)
            except np.linalg.LinAlgError:
                M = np.eye(2)
            f = (v - ps) @ M + qs
            out[k] = q - (f * self.s + self.c)
        return out


# ---------------------------------------------------------------------------
#  Rektyfikacja: model globalny + pole korekcji
# ---------------------------------------------------------------------------
@dataclass
class Correction:
    method: str
    func: object = None            # wywoływalne: punkty terenu → wektory korekcji
    lam: float = float("nan")
    auto: bool = False
    scores: list = dc_field(default_factory=list)
    rmse_before: float = float("nan")
    rmse_after: float = float("nan")
    loo_rmse: float = float("nan")
    after: np.ndarray = None       # reszty po korekcji (n×2)

    def __call__(self, Q):
        Q = np.atleast_2d(np.asarray(Q, float))
        if self.func is None:
            return np.zeros_like(Q)
        return self.func(Q)

    def apply(self, Q):
        Q = np.atleast_2d(np.asarray(Q, float))
        return Q - self(Q)


def _rmse(E):
    E = np.asarray(E, float)
    return float(np.sqrt((E ** 2).sum(axis=1).mean())) if len(E) else float("nan")


def build_correction(P, E, method="tps", lam=None, w=None, alpha=1.0):
    """Pole korekcji dla przewidzianych położeń P i reszt E = P − odniesienie.
    method – jedna z METHODS; lam=None przy „tps” – dobór walidacją krzyżową."""
    P, E = np.asarray(P, float), np.asarray(E, float)
    if method not in METHODS:
        raise RectifyError(f"nieznana metoda {method}")
    res = Correction(method, rmse_before=_rmse(E))
    if method == "none" or len(P) < 3:
        res.method, res.after, res.rmse_after = "none", E, res.rmse_before
        return res
    if method == "tps":
        if lam is None:
            lam, res.scores = choose_lambda(P, E, w)
            res.auto = True
        tps = SmoothTPS(P, E, lam, w)
        res.func, res.lam, res.loo_rmse = tps, float(lam), tps.loo_rmse()
    elif method == "tps_exact":
        tps = SmoothTPS(P, E, 0.0)
        res.func, res.lam, res.loo_rmse = tps, 0.0, tps.loo_rmse()
    elif method == "triangles":
        res.func = Triangles(P, E)
    else:
        res.func = MLS(P, E, alpha)
    res.after = E - res(P)
    res.rmse_after = _rmse(res.after)
    if method in ("triangles", "mls") or not math.isfinite(res.loo_rmse):
        res.loo_rmse = loo_rmse(P, E, method, alpha=alpha)
    return res


def loo_rmse(P, E, method, alpha=1.0):
    """Walidacja krzyżowa dla metod bez macierzy wpływu (n rozwiązań – punkty nieliczne)."""
    P, E = np.asarray(P, float), np.asarray(E, float)
    err = []
    for i in range(len(P)):
        idx = np.delete(np.arange(len(P)), i)
        try:
            f = Triangles(P[idx], E[idx]) if method == "triangles" else MLS(P[idx], E[idx], alpha)
            err.append(E[i] - f(P[i:i + 1])[0])
        except RectifyError:
            continue
    return _rmse(np.array(err)) if err else float("nan")


# ---------------------------------------------------------------------------
#  Odwzorowanie skan → teren, fałdy, siatka GCP, linia cięcia
# ---------------------------------------------------------------------------
class Rectifier:
    """Złożenie: base(skan) – model globalny (np. FitResult.predict), potem korekcja w terenie."""

    def __init__(self, base, correction=None):
        self.base = base
        self.correction = correction or Correction("none")

    def __call__(self, pts):
        return self.correction.apply(self.base(np.atleast_2d(np.asarray(pts, float))))


def bilinear(quad, u, v):
    q = np.asarray(quad, float)
    u, v = np.asarray(u, float)[..., None], np.asarray(v, float)[..., None]
    return (1 - u) * (1 - v) * q[0] + u * (1 - v) * q[1] + u * v * q[2] + (1 - u) * v * q[3]


def order_corners(pts):
    """Rogi ramki skanu: lewy górny, prawy górny, prawy dolny, lewy dolny (y skanu rośnie w górę)."""
    pts = sorted((tuple(p) for p in pts), key=lambda p: -p[1])
    top, bottom = sorted(pts[:2]), sorted(pts[2:])
    return [top[0], top[1], bottom[1], bottom[0]]


def fold_check(func, frame, n=48):
    """Udział komórek siatki w ramce, w których odwzorowanie zmienia orientację (fałda), i maska (n×n)."""
    frame = order_corners(frame)
    t = np.linspace(0, 1, n + 1)
    U, V = np.meshgrid(t, t)
    src = bilinear(frame, U, V).reshape(-1, 2)
    dst = func(src).reshape(n + 1, n + 1, 2)
    s = src.reshape(n + 1, n + 1, 2)

    def cross(a, b):
        return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]

    ds = cross(s[:-1, 1:] - s[:-1, :-1], s[1:, :-1] - s[:-1, :-1])
    dd = cross(dst[:-1, 1:] - dst[:-1, :-1], dst[1:, :-1] - dst[:-1, :-1])
    sign = np.sign(ds * dd)
    major = 1.0 if (sign >= 0).sum() >= (sign < 0).sum() else -1.0
    mask = sign * major < 0
    return float(mask.mean()), mask


def gcp_grid(func, frame, k=24):
    """Siatka (k+1)² GCP w ramce: (piksel, linia, X, Y); y skanu ujemne w dół → linia = −y."""
    frame = order_corners(frame)
    t = np.linspace(0, 1, k + 1)
    U, V = np.meshgrid(t, t)
    src = bilinear(frame, U, V).reshape(-1, 2)
    dst = func(src)
    return [(float(p[0]), float(-p[1]), float(d[0]), float(d[1])) for p, d in zip(src, dst)]


def frame_ring(frame, per_edge=96):
    """Zagęszczony obwód ramki na skanie (TL→TR→BR→BL); ten sam podział krawędzi u sąsiadów."""
    frame = order_corners(frame)
    ring = []
    for (u0, v0), (u1, v1) in (((0, 0), (1, 0)), ((1, 0), (1, 1)), ((1, 1), (0, 1)), ((0, 1), (0, 0))):
        f = np.arange(per_edge) / per_edge
        ring.append(bilinear(frame, u0 + (u1 - u0) * f, v0 + (v1 - v0) * f))
    return np.vstack(ring)


def cutline(func, frame, per_edge=96):
    """Linia cięcia w terenie: obraz zagęszczonego obwodu ramki."""
    return func(frame_ring(frame, per_edge))


def correction_magnitude_grid(correction, bounds, n=60):
    """Siatka wielkości korekcji [jednostki układu] w prostokącie bounds=(xmin, ymin, xmax, ymax)."""
    x = np.linspace(bounds[0], bounds[2], n)
    y = np.linspace(bounds[1], bounds[3], n)
    X, Y = np.meshgrid(x, y)
    C = correction(np.column_stack([X.ravel(), Y.ravel()]))
    return x, y, np.hypot(C[:, 0], C[:, 1]).reshape(n, n)


def inv_bilinear(quad, pts, iterations=30):
    """(u, v) punktów pts w czworokącie quad = [TL, TR, BR, BL] (Newton, wektorowo)."""
    q = np.asarray(quad, float)
    P = np.atleast_2d(np.asarray(pts, float))
    u = np.full(len(P), 0.5)
    v = np.full(len(P), 0.5)
    for _ in range(iterations):
        cur = bilinear(q, u, v)
        du = (1 - v)[:, None] * (q[1] - q[0]) + v[:, None] * (q[2] - q[3])
        dv = (1 - u)[:, None] * (q[3] - q[0]) + u[:, None] * (q[2] - q[1])
        det = du[:, 0] * dv[:, 1] - du[:, 1] * dv[:, 0]
        det = np.where(np.abs(det) < 1e-30, 1e-30, det)
        r = P - cur
        su = (r[:, 0] * dv[:, 1] - r[:, 1] * dv[:, 0]) / det
        sv = (du[:, 0] * r[:, 1] - du[:, 1] * r[:, 0]) / det
        u, v = u + su, v + sv
        if max(np.abs(su).max(), np.abs(sv).max()) < 1e-12:
            break
    return u, v
