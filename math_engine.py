"""
Silnik obliczeniowy QMapAnalyst.

Moduł jest niezależny od QGIS (tylko numpy / scipy), dzięki czemu można go
testować poza aplikacją. Zawiera:

* transformacje globalne (Helmert, afiniczna, wielomianowa 2. stopnia)
  wyrównywane metodą najmniejszych kwadratów z pełną statystyką
  (sigma0, błędy parametrów, liczby nadwyżkowe, reszty standaryzowane,
  walidacja krzyżowa leave-one-out, kryterium AICc),
* odwzorowanie lokalne (transformacja globalna + TPS na resztach),
* analizę zniekształceń lokalnych (skala, rotacja, elipsa Tissota),
* autokorelację przestrzenną reszt (Moran's I),
* generator izolinii (marching squares) bez zależności od matplotlib.
"""

import math
from dataclasses import dataclass, field

import numpy as np

try:  # scipy jest dostarczane z QGIS, ale zabezpieczamy się na wszelki wypadek
    from scipy.interpolate import RBFInterpolator
except Exception:  # pragma: no cover
    RBFInterpolator = None

try:
    from scipy.spatial import Delaunay
except Exception:  # pragma: no cover
    Delaunay = None


# Próg reszty standaryzowanej (test Baardy, alfa = 0.001, rozkład normalny)
OUTLIER_THRESHOLD = 3.29

MODELS = {
    "helmert": {"name": "Helmert (4 parametry)", "min_pts": 2, "n_params": 4},
    "affine": {"name": "Afiniczna (6 parametrów)", "min_pts": 3, "n_params": 6},
    "poly2": {"name": "Wielomianowa 2. stopnia (12 parametrów)", "min_pts": 6, "n_params": 12},
}

INCH_M = 0.0254


def scale_denominator(m_per_px: float, dpi: float) -> float:
    """Mianownik skali mapy papierowej dla danej rozdzielczości skanu."""
    if dpi <= 0:
        return float("nan")
    return float(m_per_px) * dpi / INCH_M


# ---------------------------------------------------------------------------
#  Transformacje globalne
# ---------------------------------------------------------------------------

def _normalization(pts: np.ndarray):
    center = pts.mean(axis=0)
    spread = float(np.sqrt(np.mean(np.sum((pts - center) ** 2, axis=1))))
    if not np.isfinite(spread) or spread <= 0:
        spread = 1.0
    return center, spread


def _basis(model: str, u: np.ndarray) -> np.ndarray:
    x, y = u[:, 0], u[:, 1]
    one = np.ones_like(x)
    if model == "poly2":
        return np.column_stack([one, x, y, x * x, x * y, y * y])
    return np.column_stack([one, x, y])


def _basis_derivatives(model: str, u: np.ndarray):
    x, y = u[:, 0], u[:, 1]
    zero = np.zeros_like(x)
    one = np.ones_like(x)
    if model == "poly2":
        dx = np.column_stack([zero, one, zero, 2 * x, y, zero])
        dy = np.column_stack([zero, zero, one, zero, x, 2 * y])
    else:
        dx = np.column_stack([zero, one, zero])
        dy = np.column_stack([zero, zero, one])
    return dx, dy


def _design(model: str, u: np.ndarray) -> np.ndarray:
    """Macierz układu równań obserwacyjnych (2n x p), obserwacje przeplatane X0, Y0, X1, Y1..."""
    n = u.shape[0]
    if model == "helmert":
        A = np.zeros((2 * n, 4))
        A[0::2, 0] = 1.0
        A[0::2, 2] = u[:, 0]
        A[0::2, 3] = -u[:, 1]
        A[1::2, 1] = 1.0
        A[1::2, 2] = u[:, 1]
        A[1::2, 3] = u[:, 0]
        return A
    B = _basis(model, u)
    m = B.shape[1]
    A = np.zeros((2 * n, 2 * m))
    A[0::2, :m] = B
    A[1::2, m:] = B
    return A


@dataclass
class FitResult:
    model: str
    src: np.ndarray
    dst: np.ndarray
    params: np.ndarray
    src_center: np.ndarray
    src_scale: float
    dst_center: np.ndarray
    pts_transformed: np.ndarray = None
    residuals: np.ndarray = None          # dst - predykcja  [jednostki dst]
    errors: np.ndarray = None             # |reszta|
    cov: np.ndarray = None                # macierz kowariancji parametrów
    sigma0: float = float("nan")
    redundancy: int = 0
    loo_errors: np.ndarray = None         # błąd walidacji krzyżowej (punkt pominięty)
    std_residuals: np.ndarray = None      # max |w| dla punktu
    redundancy_numbers: np.ndarray = None # średnia liczba nadwyżkowa punktu (0..1)
    stats: dict = field(default_factory=dict)
    derived: dict = field(default_factory=dict)

    # ---- zastosowanie ----------------------------------------------------
    @property
    def name(self) -> str:
        return MODELS[self.model]["name"]

    def _u(self, pts):
        return (np.asarray(pts, dtype=float) - self.src_center) / self.src_scale

    def predict(self, pts) -> np.ndarray:
        pts = np.atleast_2d(np.asarray(pts, dtype=float))
        u = self._u(pts)
        p = self.params
        if self.model == "helmert":
            tx, ty, a, b = p
            out = np.column_stack([tx + a * u[:, 0] - b * u[:, 1],
                                   ty + b * u[:, 0] + a * u[:, 1]])
        else:
            B = _basis(self.model, u)
            m = B.shape[1]
            out = np.column_stack([B @ p[:m], B @ p[m:]])
        return out + self.dst_center

    def jacobian(self, pts) -> np.ndarray:
        """Macierze Jacobiego d(dst)/d(src) w punktach, kształt (n, 2, 2)."""
        pts = np.atleast_2d(np.asarray(pts, dtype=float))
        n = pts.shape[0]
        p = self.params
        J = np.zeros((n, 2, 2))
        if self.model == "helmert":
            a, b = p[2], p[3]
            J[:, 0, 0] = a
            J[:, 0, 1] = -b
            J[:, 1, 0] = b
            J[:, 1, 1] = a
        else:
            u = self._u(pts)
            dx, dy = _basis_derivatives(self.model, u)
            m = dx.shape[1]
            J[:, 0, 0] = dx @ p[:m]
            J[:, 0, 1] = dy @ p[:m]
            J[:, 1, 0] = dx @ p[m:]
            J[:, 1, 1] = dy @ p[m:]
        return J / self.src_scale

    # ---- parametry pochodne ---------------------------------------------
    @staticmethod
    def _derived_from_jacobian(J: np.ndarray) -> np.ndarray:
        a, b, c, d = J[0, 0], J[0, 1], J[1, 0], J[1, 1]
        sx = math.hypot(a, c)
        sy = math.hypot(b, d)
        rot = math.degrees(math.atan2(c, a))
        shear = 0.0
        if sx > 0 and sy > 0:
            shear = math.degrees(math.asin(max(-1.0, min(1.0, (a * b + c * d) / (sx * sy)))))
        smean = math.sqrt(abs(a * d - b * c))
        return np.array([sx, sy, smean, rot, shear])

    def linear_jacobian_from_params(self, p: np.ndarray) -> np.ndarray:
        """Jacobian w centroidzie punktów (u = 0) dla zadanego wektora parametrów."""
        if self.model == "helmert":
            a, b = p[2], p[3]
            J = np.array([[a, -b], [b, a]])
        else:
            m = 6 if self.model == "poly2" else 3
            J = np.array([[p[1], p[2]], [p[m + 1], p[m + 2]]])
        return J / self.src_scale


def fit_transform(model: str, src: np.ndarray, dst: np.ndarray) -> FitResult:
    """Wyrównanie transformacji metodą najmniejszych kwadratów wraz z pełną statystyką."""
    if model not in MODELS:
        raise ValueError(f"Nieznany model transformacji: {model}")
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    n = src.shape[0]
    if n != dst.shape[0]:
        raise ValueError("Liczba punktów źródłowych i docelowych jest różna.")
    min_pts = MODELS[model]["min_pts"]
    if n < min_pts:
        raise ValueError(f"Transformacja {MODELS[model]['name']} wymaga minimum {min_pts} punktów.")
    if not (np.all(np.isfinite(src)) and np.all(np.isfinite(dst))):
        raise ValueError("Współrzędne punktów zawierają wartości nieliczbowe.")

    sc, ss = _normalization(src)
    dc = dst.mean(axis=0)
    u = (src - sc) / ss
    A = _design(model, u)
    L = (dst - dc).reshape(-1)

    p_count = A.shape[1]
    params, _, rank, _ = np.linalg.lstsq(A, L, rcond=None)
    if rank < p_count:
        raise ValueError("Układ równań jest osobliwy – punkty są współliniowe lub zdublowane.")

    res = FitResult(model=model, src=src, dst=dst, params=params,
                    src_center=sc, src_scale=ss, dst_center=dc)

    pred = (A @ params).reshape(-1, 2) + dc
    v = dst - pred
    res.pts_transformed = pred
    res.residuals = v
    res.errors = np.hypot(v[:, 0], v[:, 1])

    r = 2 * n - p_count
    res.redundancy = int(r)
    vtv = float(np.sum(v ** 2))
    res.sigma0 = math.sqrt(vtv / r) if r > 0 else float("nan")

    N_inv = np.linalg.pinv(A.T @ A)
    if r > 0:
        res.cov = (res.sigma0 ** 2) * N_inv

    # --- liczby nadwyżkowe, reszty standaryzowane, LOO (bloki 2x2 macierzy kapeluszowej)
    Ai = A.reshape(n, 2, p_count)
    H_blocks = np.einsum("nip,pq,njq->nij", Ai, N_inv, Ai)
    h_diag = np.stack([H_blocks[:, 0, 0], H_blocks[:, 1, 1]], axis=1)
    red_nums = np.clip(1.0 - h_diag, 0.0, 1.0)
    res.redundancy_numbers = red_nums.mean(axis=1)

    if r > 0 and res.sigma0 > 0:
        with np.errstate(divide="ignore", invalid="ignore"):
            w = np.abs(v) / (res.sigma0 * np.sqrt(red_nums))
        w[~np.isfinite(w)] = np.nan
        res.std_residuals = np.fmax(w[:, 0], w[:, 1])
    else:
        res.std_residuals = np.full(n, np.nan)

    loo = np.full(n, np.nan)
    if n > min_pts:
        eye = np.eye(2)
        for i in range(n):
            M = eye - H_blocks[i]
            if np.linalg.cond(M) < 1e10:
                loo[i] = float(np.linalg.norm(np.linalg.solve(M, v[i])))
    res.loo_errors = loo

    # --- parametry pochodne (skala, obrót, skos) z propagacją błędów
    J0 = res.linear_jacobian_from_params(params)
    derived = FitResult._derived_from_jacobian(J0)
    derived_std = np.full(5, np.nan)
    if res.cov is not None:
        G = np.zeros((5, p_count))
        for k in range(p_count):
            h = 1e-7 * max(1.0, abs(params[k]))
            dp = params.copy()
            dp[k] += h
            dk = FitResult._derived_from_jacobian(res.linear_jacobian_from_params(dp))
            diff = dk - derived
            diff[3] = (diff[3] + 180.0) % 360.0 - 180.0
            G[:, k] = diff / h
        derived_std = np.sqrt(np.clip(np.diag(G @ res.cov @ G.T), 0, None))
    keys = ["scale_x", "scale_y", "scale", "rotation_deg", "shear_deg"]
    res.derived = {k: float(derived[i]) for i, k in enumerate(keys)}
    res.derived.update({k + "_std": float(derived_std[i]) for i, k in enumerate(keys)})

    res.stats = residual_statistics(v, res.sigma0, p_count)
    res.stats["aicc"] = aicc(vtv, 2 * n, p_count)
    return res


def aicc(rss: float, n_obs: int, n_params: int) -> float:
    """Skorygowane kryterium informacyjne Akaike (k = parametry + wariancja)."""
    k = n_params + 1
    if n_obs - k - 1 <= 0 or rss <= 0:
        return float("nan")
    return n_obs * math.log(rss / n_obs) + 2 * k + 2 * k * (k + 1) / (n_obs - k - 1)


def residual_statistics(v: np.ndarray, sigma0: float, n_params: int) -> dict:
    e = np.hypot(v[:, 0], v[:, 1])
    n = len(e)
    stats = {
        "n": n,
        "n_params": n_params,
        "redundancy": 2 * n - n_params,
        "sigma0": sigma0,
        "mean_position_error": sigma0 * math.sqrt(2.0) if np.isfinite(sigma0) else float("nan"),
        "rmse": float(np.sqrt(np.mean(e ** 2))),
        "rmse_x": float(np.sqrt(np.mean(v[:, 0] ** 2))),
        "rmse_y": float(np.sqrt(np.mean(v[:, 1] ** 2))),
        "mean": float(np.mean(e)),
        "median": float(np.median(e)),
        "std": float(np.std(e, ddof=1)) if n > 1 else 0.0,
        "min": float(np.min(e)),
        "max": float(np.max(e)),
        "p90": float(np.percentile(e, 90)),
        "p95": float(np.percentile(e, 95)),
        "max_index": int(np.argmax(e)),
    }
    return stats


def compare_models(src: np.ndarray, dst: np.ndarray) -> list:
    """Porównanie wszystkich modeli, dla których istnieje nadwyżka obserwacji."""
    out = []
    for key, meta in MODELS.items():
        if len(src) <= meta["min_pts"]:
            continue
        try:
            fr = fit_transform(key, src, dst)
        except (ValueError, np.linalg.LinAlgError):
            continue
        out.append({
            "model": key,
            "name": meta["name"],
            "n_params": meta["n_params"],
            "sigma0": fr.sigma0,
            "rmse": fr.stats["rmse"],
            "aicc": fr.stats["aicc"],
            "loo_rmse": float(np.sqrt(np.nanmean(fr.loo_errors ** 2))) if np.any(np.isfinite(fr.loo_errors)) else float("nan"),
        })
    finite = [o["aicc"] for o in out if np.isfinite(o["aicc"])]
    best = min(finite) if finite else float("nan")
    for o in out:
        o["delta_aicc"] = o["aicc"] - best if np.isfinite(o["aicc"]) else float("nan")
    return out


# ---------------------------------------------------------------------------
#  Autokorelacja przestrzenna reszt
# ---------------------------------------------------------------------------

def morans_i(pts: np.ndarray, values: np.ndarray, permutations: int = 999, seed: int = 1):
    """Globalny Moran's I z wagami odwrotnie proporcjonalnymi do odległości.

    Zwraca (I, wartość oczekiwana, p-value testu permutacyjnego jednostronnego
    dla autokorelacji dodatniej) lub None przy zbyt małej liczbie punktów.
    """
    pts = np.asarray(pts, dtype=float)
    z = np.asarray(values, dtype=float)
    n = len(z)
    if n < 5:
        return None
    d = np.hypot(pts[:, None, 0] - pts[None, :, 0], pts[:, None, 1] - pts[None, :, 1])
    with np.errstate(divide="ignore"):
        W = np.where(d > 0, 1.0 / d, 0.0)
    np.fill_diagonal(W, 0.0)
    S0 = W.sum()
    z = z - z.mean()
    zz = float(z @ z)
    if S0 <= 0 or zz <= 0:
        return None
    I = n / S0 * float(z @ W @ z) / zz

    rng = np.random.default_rng(seed)
    Z = np.array([rng.permutation(z) for _ in range(permutations)])
    I_perm = n / S0 * np.einsum("ij,jk,ik->i", Z, W, Z) / zz
    p = (np.sum(I_perm >= I) + 1) / (permutations + 1)
    return float(I), -1.0 / (n - 1), float(p)


# ---------------------------------------------------------------------------
#  Odwzorowanie lokalne: transformacja globalna + TPS reszt
# ---------------------------------------------------------------------------

class Warp:
    """Ciągłe, interpolujące odwzorowanie src -> dst (globalny model + TPS na resztach)."""

    def __init__(self, fit: FitResult, smoothing: float = 0.0):
        self.fit = fit
        self.rbf = None
        self.ok = False
        src = fit.src
        if RBFInterpolator is None or len(src) < 3:
            return
        self._c, self._s = _normalization(src)
        u = (src - self._c) / self._s
        for sm in (smoothing, max(smoothing, 1e-6), 1e-3):
            try:
                self.rbf = RBFInterpolator(u, fit.residuals, kernel="thin_plate_spline", smoothing=sm)
                self.ok = True
                break
            except (np.linalg.LinAlgError, ValueError):
                continue

    def predict(self, pts) -> np.ndarray:
        pts = np.atleast_2d(np.asarray(pts, dtype=float))
        out = self.fit.predict(pts)
        if self.ok:
            out = out + self.rbf((pts - self._c) / self._s)
        return out

    @staticmethod
    def build(model: str, src: np.ndarray, dst: np.ndarray, smoothing: float = 0.0) -> "Warp":
        return Warp(fit_transform(model, src, dst), smoothing)


def hull_mask(pts: np.ndarray, query: np.ndarray) -> np.ndarray:
    """True dla punktów leżących wewnątrz otoczki wypukłej punktów kontrolnych."""
    if Delaunay is None or len(pts) < 3:
        return np.ones(len(query), dtype=bool)
    try:
        tri = Delaunay(pts)
        return tri.find_simplex(query) >= 0
    except Exception:
        return np.ones(len(query), dtype=bool)


def _singular_values_2x2(p, q, r, s):
    E = (p + s) / 2.0
    F = (p - s) / 2.0
    G = (r + q) / 2.0
    H = (r - q) / 2.0
    Q = np.hypot(E, H)
    R = np.hypot(F, G)
    return Q + R, np.abs(Q - R)


def local_distortion_grid(warp: Warp, nx: int = 45, ny: int = 45, margin: float = 0.0) -> dict:
    """Siatka lokalnych parametrów zniekształceń w przestrzeni mapy dawnej.

    Zwraca słownik z siatkami (ny, nx): skala powierzchniowa [dst/src],
    skale osiowe, rotacja [deg], półosie elipsy Tissota (a, b),
    maksymalne zniekształcenie kątowe 2ω [deg] oraz maskę otoczki punktów.
    """
    src = warp.fit.src
    mn = src.min(axis=0)
    mx = src.max(axis=0)
    span = mx - mn
    mn = mn - span * margin
    mx = mx + span * margin
    x = np.linspace(mn[0], mx[0], nx)
    y = np.linspace(mn[1], mx[1], ny)
    X, Y = np.meshgrid(x, y)
    grid = np.column_stack([X.ravel(), Y.ravel()])
    F = warp.predict(grid)
    FX = F[:, 0].reshape(ny, nx)
    FY = F[:, 1].reshape(ny, nx)

    dFX_dy, dFX_dx = np.gradient(FX, y, x)
    dFY_dy, dFY_dx = np.gradient(FY, y, x)

    det = dFX_dx * dFY_dy - dFX_dy * dFY_dx
    area_scale = np.sqrt(np.abs(det))
    sx = np.hypot(dFX_dx, dFY_dx)
    sy = np.hypot(dFX_dy, dFY_dy)

    rot_global = warp.fit.derived.get("rotation_deg", 0.0)
    rot = np.degrees(np.arctan2(dFY_dx, dFX_dx))
    rot = (rot - rot_global + 180.0) % 360.0 - 180.0 + rot_global

    a, b = _singular_values_2x2(dFX_dx, dFX_dy, dFY_dx, dFY_dy)
    with np.errstate(divide="ignore", invalid="ignore"):
        omega = np.degrees(2.0 * np.arcsin(np.clip((a - b) / (a + b), 0.0, 1.0)))
        aniso = np.where(b > 0, a / b, np.nan)

    inside = hull_mask(src, grid).reshape(ny, nx)
    return {
        "x": x, "y": y, "X": X, "Y": Y, "FX": FX, "FY": FY,
        "area_scale": area_scale, "scale_x": sx, "scale_y": sy,
        "rotation_deg": rot, "tissot_a": a, "tissot_b": b,
        "omega_deg": omega, "anisotropy": aniso, "inside": inside,
    }


def summarize_distortion(grid: dict) -> dict:
    m = grid["inside"]
    if not np.any(m):
        m = np.ones_like(m, dtype=bool)

    def q(name):
        vals = grid[name][m]
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            return dict(min=np.nan, max=np.nan, mean=np.nan, std=np.nan, p05=np.nan, p95=np.nan)
        return dict(min=float(vals.min()), max=float(vals.max()), mean=float(vals.mean()),
                    std=float(vals.std()), p05=float(np.percentile(vals, 5)),
                    p95=float(np.percentile(vals, 95)))

    out = {k: q(k) for k in ("area_scale", "rotation_deg", "omega_deg", "anisotropy", "scale_x", "scale_y")}
    s = out["area_scale"]
    out["scale_variation_pct"] = (s["p95"] / s["p05"] - 1.0) * 100.0 if s["p05"] > 0 else float("nan")
    out["rotation_range_deg"] = out["rotation_deg"]["p95"] - out["rotation_deg"]["p05"]
    return out


# ---------------------------------------------------------------------------
#  Izolinie
# ---------------------------------------------------------------------------

# Odcinki dla każdego z 16 przypadków. Krawędzie: 0 dół, 1 prawo, 2 góra, 3 lewo.
_MS_CASES = {
    1: [(3, 0)], 2: [(0, 1)], 3: [(3, 1)], 4: [(1, 2)], 5: [(3, 0), (1, 2)],
    6: [(0, 2)], 7: [(3, 2)], 8: [(2, 3)], 9: [(0, 2)], 10: [(0, 1), (2, 3)],
    11: [(1, 2)], 12: [(3, 1)], 13: [(0, 1)], 14: [(3, 0)],
}


def marching_squares(x: np.ndarray, y: np.ndarray, Z: np.ndarray, level: float) -> np.ndarray:
    """Odcinki izolinii poziomu `level` dla siatki Z (ny, nx). Zwraca tablicę (m, 2, 2)."""
    z00 = Z[:-1, :-1]
    z10 = Z[:-1, 1:]
    z11 = Z[1:, 1:]
    z01 = Z[1:, :-1]
    valid = np.isfinite(z00) & np.isfinite(z10) & np.isfinite(z11) & np.isfinite(z01)
    case = ((z00 > level).astype(np.int8) | ((z10 > level).astype(np.int8) << 1) |
            ((z11 > level).astype(np.int8) << 2) | ((z01 > level).astype(np.int8) << 3))
    case[~valid] = 0

    X0, Y0 = np.meshgrid(x[:-1], y[:-1])
    X1, Y1 = np.meshgrid(x[1:], y[1:])

    def interp(za, zb):
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (level - za) / (zb - za)
        return np.clip(np.nan_to_num(t, nan=0.5), 0.0, 1.0)

    edges = [
        np.stack([X0 + interp(z00, z10) * (X1 - X0), Y0], axis=-1),   # dół
        np.stack([X1, Y0 + interp(z10, z11) * (Y1 - Y0)], axis=-1),   # prawo
        np.stack([X0 + interp(z01, z11) * (X1 - X0), Y1], axis=-1),   # góra
        np.stack([X0, Y0 + interp(z00, z01) * (Y1 - Y0)], axis=-1),   # lewo
    ]

    segs = []
    for c, pairs in _MS_CASES.items():
        sel = case == c
        if not np.any(sel):
            continue
        for e1, e2 in pairs:
            segs.append(np.stack([edges[e1][sel], edges[e2][sel]], axis=1))
    if not segs:
        return np.zeros((0, 2, 2))
    return np.concatenate(segs, axis=0)


def nice_step(span: float, target: int = 8) -> float:
    if not np.isfinite(span) or span <= 0:
        return 1.0
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def nice_levels(values: np.ndarray, target: int = 8) -> np.ndarray:
    vals = np.asarray(values, dtype=float)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return np.array([])
    lo, hi = np.percentile(vals, 1), np.percentile(vals, 99)
    if hi <= lo:
        return np.array([lo])
    step = nice_step(hi - lo, target)
    start = math.ceil(lo / step) * step
    return np.arange(start, hi + step * 0.5, step)
