"""
Estymacja odwzorowania kartograficznego mapy dawnej.

Idea (por. T. Bayer, "Estimation of an unknown cartographic projection and its
parameters from the map", GeoInformatica 18, 2014; B. Jenny, MapAnalyst):

Dla każdego odwzorowania-kandydata współrzędne geograficzne punktów
kontrolnych (λ, φ) są rzutowane na płaszczyznę (sfera jednostkowa), a następnie
dopasowywane do współrzędnych pikselowych skanu transformacją Helmerta
(lub afiniczną – uwzględnia nierównomierny skurcz papieru). Parametry kształtu
odwzorowania (południk osiowy, równoleżniki standardowe, środek odwzorowania)
są optymalizowane metodą najmniejszych kwadratów. Kandydaci są szeregowani
kryterium AICc, które karze za liczbę dopasowanych parametrów.

Reszty liczone są w pikselach mapy dawnej, dzięki czemu wszyscy kandydaci
(łącznie z układem odniesienia projektu) są porównywalni.

Moduł nie zależy od QGIS.
"""

import math
from dataclasses import dataclass, field

import numpy as np

try:
    from scipy.optimize import least_squares
except Exception:  # pragma: no cover
    least_squares = None

from .lang import decimal_sep, num
from .math_engine import EngineError, aicc

EARTH_RADIUS_M = 6371008.8
INCH_M = 0.0254

# Historyczne południki zerowe (długość względem Greenwich, stopnie)
PRIME_MERIDIANS = [
    ("Ferro", -17.0 - 40.0 / 60.0),
    ("Paryż", 2.0 + 20.0 / 60.0 + 14.025 / 3600.0),
    ("Pułkowo", 30.0 + 19.0 / 60.0 + 42.09 / 3600.0),
    ("Warszawa", 21.0 + 0.0 / 60.0 + 42.0 / 3600.0),
]

_EPS = 1e-10
_QPI = math.pi / 4.0


def _wrap(dlam):
    return (dlam + np.pi) % (2.0 * np.pi) - np.pi


# ---------------------------------------------------------------------------
#  Formuły odwzorowań (sfera jednostkowa, argumenty w radianach)
# ---------------------------------------------------------------------------

def _plate(lam, phi, p):
    return _wrap(lam - p["lon0"]), phi


def _equirect(lam, phi, p):
    return _wrap(lam - p["lon0"]) * math.cos(p["lat1"]), phi


def _mercator(lam, phi, p):
    ph = np.clip(phi, -1.5, 1.5)
    return _wrap(lam - p["lon0"]), np.log(np.tan(_QPI + ph / 2.0))


def _cassini(lam, phi, p):
    d = _wrap(lam - p["lon0"])
    x = np.arcsin(np.clip(np.cos(phi) * np.sin(d), -1.0, 1.0))
    y = np.arctan2(np.tan(phi), np.cos(d))
    return x, y


def _tmerc(lam, phi, p):
    d = _wrap(lam - p["lon0"])
    B = np.clip(np.cos(phi) * np.sin(d), -1 + 1e-12, 1 - 1e-12)
    return np.arctanh(B), np.arctan2(np.tan(phi), np.cos(d))


def _sinusoidal(lam, phi, p):
    return _wrap(lam - p["lon0"]) * np.cos(phi), phi


def _bonne(lam, phi, p):
    p1 = p["lat1"]
    d = _wrap(lam - p["lon0"])
    if abs(p1) < 1e-6:
        return d * np.cos(phi), phi
    cot1 = 1.0 / math.tan(p1)
    rho = cot1 + p1 - phi
    with np.errstate(divide="ignore", invalid="ignore"):
        E = np.where(np.abs(rho) > _EPS, d * np.cos(phi) / rho, 0.0)
    return rho * np.sin(E), cot1 - rho * np.cos(E)


def _conic_xy(rho, theta):
    return rho * np.sin(theta), -rho * np.cos(theta)


def _eqdc(lam, phi, p):
    p1, p2 = p["lat1"], p["lat2"]
    d = _wrap(lam - p["lon0"])
    n = math.sin(p1) if abs(p1 - p2) < 1e-7 else (math.cos(p1) - math.cos(p2)) / (p2 - p1)
    if abs(n) < 1e-9:
        return d * math.cos(p1), phi
    G = math.cos(p1) / n + p1
    return _conic_xy(G - phi, n * d)


def _lcc(lam, phi, p):
    p1, p2 = p["lat1"], p["lat2"]
    d = _wrap(lam - p["lon0"])
    t = lambda f: np.tan(_QPI + np.clip(f, -1.55, 1.55) / 2.0)
    if abs(p1 - p2) < 1e-7:
        n = math.sin(p1)
    else:
        n = math.log(math.cos(p1) / math.cos(p2)) / math.log(float(t(p2) / t(p1)))
    if abs(n) < 1e-9:
        return _mercator(lam, phi, p)
    F = math.cos(p1) * float(t(p1)) ** n / n
    return _conic_xy(F / t(phi) ** n, n * d)


def _albers(lam, phi, p):
    p1, p2 = p["lat1"], p["lat2"]
    d = _wrap(lam - p["lon0"])
    n = (math.sin(p1) + math.sin(p2)) / 2.0
    if abs(n) < 1e-9:
        return d, np.sin(phi)
    C = math.cos(p1) ** 2 + 2.0 * n * math.sin(p1)
    rho = np.sqrt(np.maximum(C - 2.0 * n * np.sin(phi), 0.0)) / n
    return _conic_xy(rho, n * d)


def _azimuthal(lam, phi, p, kind):
    p0 = p["lat0"]
    d = _wrap(lam - p["lon0"])
    cosc = np.clip(math.sin(p0) * np.sin(phi) + math.cos(p0) * np.cos(phi) * np.cos(d), -1.0, 1.0)
    if kind == "laea":
        k = np.sqrt(2.0 / np.maximum(1.0 + cosc, 1e-12))
    elif kind == "stere":
        k = 2.0 / np.maximum(1.0 + cosc, 1e-12)
    else:  # aeqd
        c = np.arccos(cosc)
        with np.errstate(divide="ignore", invalid="ignore"):
            k = np.where(c > 1e-9, c / np.sin(c), 1.0)
    x = k * np.cos(phi) * np.sin(d)
    y = k * (math.cos(p0) * np.sin(phi) - math.sin(p0) * np.cos(phi) * np.cos(d))
    return x, y


def _laea(lam, phi, p):
    return _azimuthal(lam, phi, p, "laea")


def _stere(lam, phi, p):
    return _azimuthal(lam, phi, p, "stere")


def _aeqd(lam, phi, p):
    return _azimuthal(lam, phi, p, "aeqd")


def _polyconic(lam, phi, p):
    d = _wrap(lam - p["lon0"])
    small = np.abs(phi) < 1e-10
    ph = np.where(small, 1e-10, phi)
    cot = 1.0 / np.tan(ph)
    E = d * np.sin(ph)
    x = np.where(small, d, cot * np.sin(E))
    y = np.where(small, phi, ph + cot * (1.0 - np.cos(E)))
    return x, y


def _trapezoidal(lam, phi, p):
    p1, p2 = p["lat1"], p["lat2"]
    d = _wrap(lam - p["lon0"])
    if abs(p2 - p1) < 1e-7:
        k = math.cos(p1)
    else:
        k = math.cos(p1) + (phi - p1) * (math.cos(p2) - math.cos(p1)) / (p2 - p1)
    return d * k, phi


@dataclass
class ProjectionDef:
    key: str
    name: str
    family: str
    era: str
    note: str
    func: object
    free: tuple            # parametry optymalizowane
    fixed: dict = field(default_factory=dict)  # parametry ustalone (np. lat1 = 0)


PROJECTIONS = [
    ProjectionDef("plate", "Plate carrée (walcowe równoodległościowe)", "walcowe", "XV–XVIII w.",
                  "‘Carte plate’ – siatka prostokątna o równych oczkach; mapy regionalne i morskie przed upowszechnieniem odwzorowania Mercatora.",
                  _plate, (), {"lon0": None}),
    ProjectionDef("equirect", "Walcowe równoodległościowe z równoleżnikiem standardowym", "walcowe", "XVI–XVIII w.",
                  "Odwzorowanie Marinusa z Tyru: prostokątna siatka ze skalą wierną na wybranym równoleżniku; częste na mapach krain i województw.",
                  _equirect, ("lat1",), {"lon0": None}),
    ProjectionDef("mercator", "Mercatora (walcowe wiernokątne)", "walcowe", "od 1569",
                  "Mapy morskie i atlasy; loksodromy jako linie proste.",
                  _mercator, (), {"lon0": None}),
    ProjectionDef("cassini", "Cassini–Soldnera (walcowe poprzeczne równoodległościowe)", "walcowe poprzeczne", "XVIII–XIX w.",
                  "Carte de Cassini (Francja, XVIII w.); katastry i mapy szczegółowe XIX w. (m.in. austriacki i pruski).",
                  _cassini, ("lon0",)),
    ProjectionDef("tmerc", "Gaussa–Krügera (walcowe poprzeczne wiernokątne)", "walcowe poprzeczne", "XIX–XX w.",
                  "Mapy topograficzne XX w. (m.in. układy 1942, 1992, 2000).",
                  _tmerc, ("lon0",)),
    ProjectionDef("sinusoidal", "Sansona–Flamsteeda (sinusoidalne)", "pseudowalcowe", "XVII–XVIII w.",
                  "Równopolowe; atlasy XVII–XVIII w., mapy kontynentów i krajów.",
                  _sinusoidal, ("lon0",)),
    ProjectionDef("bonne", "Bonne'a (pseudostożkowe równopolowe)", "pseudostożkowe", "XVIII–XIX w.",
                  "Bardzo popularne od końca XVIII w.: Carte de l'État-major (Francja), mapa Dufoura (Szwajcaria), Mapa Kwatermistrzowska (Królestwo Polskie, 1843).",
                  _bonne, ("lon0", "lat1")),
    ProjectionDef("eqdc", "Stożkowe równoodległościowe (De l'Isle'a)", "stożkowe", "XVIII–XIX w.",
                  "Stożek prosty z dwoma równoleżnikami wiernymi (Ptolemeusz, J.-N. De l'Isle 1745) – mapy krajów i regionów, np. Rosji.",
                  _eqdc, ("lat1", "lat2"), {"lon0": None}),
    ProjectionDef("lcc", "Lamberta stożkowe wiernokątne", "stożkowe", "od 1772, XIX–XX w.",
                  "Upowszechnione w XIX–XX w. (mapy przeglądowe, lotnicze, wojskowe).",
                  _lcc, ("lat1", "lat2"), {"lon0": None}),
    ProjectionDef("albers", "Albersa stożkowe równopolowe", "stożkowe", "od 1805",
                  "Mapy przeglądowe XIX–XX w.",
                  _albers, ("lat1", "lat2"), {"lon0": None}),
    ProjectionDef("laea", "Lamberta azymutalne równopolowe", "azymutalne", "od 1772",
                  "Mapy kontynentów i półkul.",
                  _laea, ("lon0", "lat0")),
    ProjectionDef("stere", "Stereograficzne (azymutalne wiernokątne)", "azymutalne", "XVII–XX w.",
                  "Mapy półkul XVII–XVIII w.; odmiany quasi-stereograficzne w XX w. (mapy WIG 1:100 000, układ 1965).",
                  _stere, ("lon0", "lat0")),
    ProjectionDef("aeqd", "Azymutalne równoodległościowe (Postela)", "azymutalne", "od XVI w.",
                  "Mapy półkul i okolic punktu centralnego.",
                  _aeqd, ("lon0", "lat0")),
    ProjectionDef("poly", "Wielostożkowe (Hasslera)", "polikoniczne", "XIX w.",
                  "Mapy topograficzne XIX w.; w zasięgu jednego arkusza bliskie odwzorowaniu poliedrycznemu (III zdjęcie wojskowe Austro-Węgier, pruskie Messtischblatt).",
                  _polyconic, ("lon0",)),
    ProjectionDef("trapez", "Trapezowe (Donisa)", "pseudocylindryczne", "XV–XVIII w.",
                  "Proste południki zbieżne ku biegunowi, dwa równoleżniki wierne; mapy regionalne w wydaniach Ptolemeusza i późniejszych.",
                  _trapezoidal, ("lon0", "lat1", "lat2")),
]

PROJECTIONS_BY_KEY = {p.key: p for p in PROJECTIONS}

PARAM_LABELS = {
    "lon0": "λ₀ (południk osiowy)",
    "lat0": "φ₀ (środek)",
    "lat1": "φ₁ (równoleżnik standardowy)",
    "lat2": "φ₂ (równoleżnik standardowy)",
}


# ---------------------------------------------------------------------------
#  Dopasowanie płaszczyzna -> piksele
# ---------------------------------------------------------------------------

def _fit_plane(P: np.ndarray, Q: np.ndarray, mode: str):
    """Dopasowanie Q ≈ P·Aᵀ + t. Zwraca (A, t, reszty)."""
    Pm = P.mean(axis=0)
    Qm = Q.mean(axis=0)
    Pc = P - Pm
    Qc = Q - Qm
    if mode == "affine":
        M, *_ = np.linalg.lstsq(Pc, Qc, rcond=None)
        A = M.T
    else:
        den = float(np.sum(Pc ** 2))
        if den <= 0:
            A = np.eye(2)
        else:
            a = float(np.sum(Pc * Qc)) / den
            b = float(np.sum(Pc[:, 0] * Qc[:, 1] - Pc[:, 1] * Qc[:, 0])) / den
            A = np.array([[a, -b], [b, a]])
    t = Qm - A @ Pm
    return A, t, Q - (P @ A.T + t)


@dataclass
class Candidate:
    key: str
    name: str
    family: str
    era: str
    note: str
    params_deg: dict
    params_std_deg: dict
    n_free: int
    rss: float
    sigma0_px: float
    aicc: float
    residuals_px: np.ndarray
    A: np.ndarray
    t: np.ndarray
    is_reference: bool = False
    delta_aicc: float = float("nan")
    weight: float = float("nan")
    success: bool = True
    center: tuple = None          # (λ, φ) środka obszaru punktów [deg]
    is_hypothesis: bool = False   # parametry ustalone przez użytkownika (bez optymalizacji)

    @property
    def px_per_unit(self) -> float:
        return math.sqrt(abs(float(np.linalg.det(self.A))))

    def principal_scale_denominator(self, dpi: float) -> float:
        """Skala główna odwzorowania (na liniach/punktach wiernych – np. równik dla Mercatora)."""
        s = self.px_per_unit
        if s <= 0 or dpi <= 0:
            return float("nan")
        unit_m = 1.0 if self.is_reference else EARTH_RADIUS_M
        return unit_m / (s * INCH_M / dpi)

    def local_scale_factor(self) -> float:
        """Powierzchniowy współczynnik skali odwzorowania w środku obszaru punktów (k = √(a·b))."""
        if self.is_reference or self.center is None:
            return 1.0
        lon, lat = self.center
        h = 1e-4
        try:
            P = self.project([lon - h, lon + h, lon, lon], [lat, lat, lat - h, lat + h])
        except Exception:  # noqa: BLE001
            return float("nan")
        hr = math.radians(h)
        d_lam = (P[1] - P[0]) / (2 * hr)
        d_phi = (P[3] - P[2]) / (2 * hr)
        det = abs(d_lam[0] * d_phi[1] - d_lam[1] * d_phi[0])
        cphi = math.cos(math.radians(lat))
        if not np.isfinite(det) or cphi <= 0:
            return float("nan")
        return math.sqrt(det / cphi)

    def scale_denominator(self, dpi: float) -> float:
        """Skala mapy w środku obszaru punktów (porównywalna między odwzorowaniami)."""
        k = self.local_scale_factor()
        M = self.principal_scale_denominator(dpi)
        return M / k if np.isfinite(k) and k > 0 else float("nan")

    def project(self, lon_deg, lat_deg) -> np.ndarray:
        """Współrzędne płaskie (sfera jednostkowa) dla danych λ, φ [stopnie]."""
        proj = PROJECTIONS_BY_KEY[self.key]
        p = {k: math.radians(v) for k, v in self.params_deg.items()}
        x, y = proj.func(np.radians(np.asarray(lon_deg, float)), np.radians(np.asarray(lat_deg, float)), p)
        return np.column_stack([np.ravel(x), np.ravel(y)])

    def to_pixels(self, lon_deg, lat_deg) -> np.ndarray:
        if self.is_reference:
            raise EngineError("Kandydat referencyjny nie ma odwzorowania geograficznego.")
        P = self.project(lon_deg, lat_deg)
        return P @ self.A.T + self.t


@dataclass
class EstimationResult:
    candidates: list
    fit_mode: str
    n_points: int
    lon_range: tuple
    lat_range: tuple
    discriminable: bool
    spread_ratio: float
    messages: list = field(default_factory=list)   # lista (szablon, parametry) – tłumaczone w raporcie
    lonlat: np.ndarray = None     # dane wejściowe – potrzebne do oceny hipotez
    pixels: np.ndarray = None

    def free_fit(self, key):
        """Swobodnie dopasowany kandydat danego odwzorowania (bez hipotez)."""
        return next((c for c in self.candidates if c.key == key and not c.is_hypothesis and not c.is_reference), None)

    @property
    def best(self):
        for c in self.candidates:
            if not c.is_reference and c.success:
                return c
        return None

    def equivalent(self, threshold: float = 2.0) -> list:
        """Kandydaci równorzędni z najlepszym (ΔAICc < threshold)."""
        return [c for c in self.candidates if not c.is_reference and c.success
                and np.isfinite(c.delta_aicc) and c.delta_aicc < threshold]

    @property
    def conclusive(self) -> bool:
        """Czy wynik pozwala wskazać jedno odwzorowanie."""
        b = self.best
        return b is not None and self.discriminable and len(self.equivalent()) == 1 and b.weight >= 0.5


def _initial_guesses(free, lon_c, lat_c, lat_min, lat_max, lon_span):
    span = max(lat_max - lat_min, 0.5)
    base = {
        "lon0": lon_c,
        "lat0": lat_c,
        "lat1": lat_min + span / 6.0,
        "lat2": lat_max - span / 6.0,
    }
    variants = [dict(base)]
    v = dict(base)
    v["lat1"], v["lat2"] = lat_c, lat_c
    variants.append(v)
    v = dict(base)
    v["lon0"] = lon_c + max(lon_span, 1.0)
    v["lat1"], v["lat2"] = lat_min, lat_max
    variants.append(v)
    v = dict(base)
    v["lon0"] = lon_c - max(lon_span, 1.0)
    v["lat0"] = lat_c + 0.5 * span
    variants.append(v)
    if "lat1" in free and "lat2" not in free:
        variants.append(dict(base, lat1=lat_c))
    return [[g[k] for k in free] for g in variants]


def _bounds(free, lon_c, lat_c, lon_span):
    lon_w = max(30.0, 3.0 * lon_span)
    lo, hi = [], []
    for k in free:
        if k == "lon0":
            lo.append(lon_c - lon_w)
            hi.append(lon_c + lon_w)
        else:
            lo.append(-85.0)
            hi.append(85.0)
    return np.array(lo), np.array(hi)


def fit_projection(proj: ProjectionDef, lonlat_deg: np.ndarray, pixels: np.ndarray,
                   mode: str = "helmert") -> Candidate:
    lam = np.radians(lonlat_deg[:, 0])
    phi = np.radians(lonlat_deg[:, 1])
    n = len(lam)
    lon_c = float(np.mean(lonlat_deg[:, 0]))
    lat_c = float(np.mean(lonlat_deg[:, 1]))
    lat_min, lat_max = float(lonlat_deg[:, 1].min()), float(lonlat_deg[:, 1].max())
    lon_span = float(np.ptp(lonlat_deg[:, 0]))

    fixed = {}
    for k, v in proj.fixed.items():
        fixed[k] = math.radians(lon_c if (k == "lon0" and v is None) else (v if v is not None else 0.0))
    if proj.key == "plate":
        fixed.setdefault("lat1", 0.0)

    q_scale = float(np.sqrt(np.mean(np.sum((pixels - pixels.mean(0)) ** 2, axis=1)))) or 1.0
    penalty = np.full(2 * n, 10.0 * q_scale)

    def residuals(theta_deg):
        p = dict(fixed)
        for k, v in zip(proj.free, theta_deg):
            p[k] = math.radians(v)
        with np.errstate(all="ignore"):
            x, y = proj.func(lam, phi, p)
        P = np.column_stack([np.broadcast_to(x, lam.shape), np.broadcast_to(y, lam.shape)])
        if not np.all(np.isfinite(P)) or np.ptp(P, axis=0).max() <= 0:
            return penalty
        _, _, r = _fit_plane(P, pixels, mode)
        out = r.reshape(-1)
        return out if np.all(np.isfinite(out)) else penalty

    free = proj.free
    best_theta = np.array([], dtype=float)
    std = {}
    success = True
    if free:
        if least_squares is None:
            raise EngineError("Brak scipy.optimize – estymacja odwzorowania niemożliwa.")
        lo, hi = _bounds(free, lon_c, lat_c, lon_span)
        best = None
        for x0 in _initial_guesses(free, lon_c, lat_c, lat_min, lat_max, lon_span):
            x0 = np.clip(np.array(x0, float), lo + 1e-6, hi - 1e-6)
            try:
                sol = least_squares(residuals, x0, bounds=(lo, hi), x_scale=1.0,
                                    diff_step=1e-6, max_nfev=400)
            except Exception:
                continue
            if best is None or sol.cost < best.cost:
                best = sol
        if best is None:
            success = False
            best_theta = np.array(_initial_guesses(free, lon_c, lat_c, lat_min, lat_max, lon_span)[0])
            jac = None
        else:
            best_theta = best.x
            jac = best.jac
    else:
        jac = None

    r = residuals(best_theta)
    rss = float(r @ r)
    k_total = (6 if mode == "affine" else 4) + len(free)
    dof = 2 * n - k_total
    sigma0 = math.sqrt(rss / dof) if dof > 0 else float("nan")

    if jac is not None and dof > 0:
        try:
            cov = np.linalg.pinv(jac.T @ jac) * sigma0 ** 2
            std = {k: float(math.sqrt(max(cov[i, i], 0.0))) for i, k in enumerate(free)}
        except np.linalg.LinAlgError:
            std = {}

    p_rad = dict(fixed)
    for k, v in zip(free, best_theta):
        p_rad[k] = math.radians(float(v))
    params_deg = {k: math.degrees(v) for k, v in p_rad.items()}
    if "lon0" in params_deg:
        params_deg["lon0"] = (params_deg["lon0"] + 180.0) % 360.0 - 180.0

    with np.errstate(all="ignore"):
        x, y = proj.func(lam, phi, p_rad)
    P = np.column_stack([np.broadcast_to(x, lam.shape), np.broadcast_to(y, lam.shape)])
    if np.all(np.isfinite(P)):
        A, t, res_px = _fit_plane(P, pixels, mode)
    else:
        A, t, res_px = np.eye(2), np.zeros(2), r.reshape(-1, 2)
        success = False

    return Candidate(
        key=proj.key, name=proj.name, family=proj.family, era=proj.era, note=proj.note,
        params_deg=params_deg, params_std_deg=std, n_free=len(free), rss=rss,
        sigma0_px=sigma0, aicc=aicc(rss, 2 * n, k_total), residuals_px=res_px,
        A=A, t=t, success=success,
    )


def reference_candidate(ref_xy: np.ndarray, pixels: np.ndarray, label: str, mode: str = "helmert") -> Candidate:
    A, t, r = _fit_plane(ref_xy, pixels, mode)
    n = len(ref_xy)
    k = 6 if mode == "affine" else 4
    rss = float(np.sum(r ** 2))
    dof = 2 * n - k
    return Candidate(
        key="reference", name=label, family="układ odniesienia", era="współczesny",
        note="Układ współrzędnych projektu, w którym zebrano punkty referencyjne (punkt odniesienia porównania).",
        params_deg={}, params_std_deg={}, n_free=0, rss=rss,
        sigma0_px=math.sqrt(rss / dof) if dof > 0 else float("nan"),
        aicc=aicc(rss, 2 * n, k), residuals_px=r, A=A, t=t, is_reference=True,
    )


def estimate_projections(lonlat_deg, pixels, ref_xy=None, ref_label=None,
                         mode: str = "helmert", keys=None, progress=None, labels=None) -> EstimationResult:
    lonlat_deg = np.asarray(lonlat_deg, dtype=float)
    pixels = np.asarray(pixels, dtype=float)
    n = len(pixels)
    min_pts = 5 if mode == "helmert" else 6
    if n < min_pts:
        raise EngineError("Estymacja odwzorowania wymaga minimum {k} aktywnych punktów (zalecane > 15).", k=min_pts)

    defs = [p for p in PROJECTIONS if keys is None or p.key in keys]
    cands = []
    for i, proj in enumerate(defs):
        if 2 * n - ((6 if mode == "affine" else 4) + len(proj.free)) <= 1:
            continue
        cands.append(fit_projection(proj, lonlat_deg, pixels, mode))
        if progress:
            progress(i + 1, len(defs))
    if ref_xy is not None:
        cands.append(reference_candidate(np.asarray(ref_xy, float), pixels, ref_label or "Układ odniesienia", mode))

    _rank(cands)
    center = (float(np.mean(lonlat_deg[:, 0])), float(np.mean(lonlat_deg[:, 1])))
    for c in cands:
        c.center = center

    sig = [c.sigma0_px for c in cands if np.isfinite(c.sigma0_px) and not c.is_reference and c.success]
    spread = (max(sig) / min(sig) - 1.0) if sig and min(sig) > 0 else 0.0

    lon_rng = (float(lonlat_deg[:, 0].min()), float(lonlat_deg[:, 0].max()))
    lat_rng = (float(lonlat_deg[:, 1].min()), float(lonlat_deg[:, 1].max()))
    ext = max(lon_rng[1] - lon_rng[0], lat_rng[1] - lat_rng[0])
    msgs = []
    discriminable = True
    if ext < 2.0:
        discriminable = False
        msgs.append(("Zasięg punktów to tylko ~{ext}°. Na tak małym obszarze różnice między odwzorowaniami "
                     "są zwykle mniejsze niż błędy rysunku mapy – wynik ma charakter orientacyjny.",
                     {"ext": num(ext, 1)}))
    if spread < 0.10:
        discriminable = False
        msgs.append(("Błędy σ₀ wszystkich kandydatów różnią się o mniej niż {pct}% – "
                     "dane nie pozwalają wiarygodnie rozróżnić odwzorowań.", {"pct": f"{spread * 100:.0f}"}))
    if mode == "helmert" and ref_xy is not None and n >= 5:
        # anizotropia skali (skurcz papieru): czy afiniczne dopasowanie układu odniesienia jest wyraźnie lepsze?
        R = np.asarray(ref_xy, float)
        _, _, rh = _fit_plane(R, pixels, "helmert")
        _, _, ra = _fit_plane(R, pixels, "affine")
        a_h = aicc(float(np.sum(rh ** 2)), 2 * n, 4)
        a_a = aicc(float(np.sum(ra ** 2)), 2 * n, 6)
        if np.isfinite(a_h) and np.isfinite(a_a) and a_h - a_a > 10:
            msgs.append(("Dane wykazują anizotropię skali (nierównomierny skurcz papieru, ΔAICc = {d}). W trybie Helmerta "
                         "odwzorowania z parametrem zmieniającym proporcje siatki (np. walcowe z równoleżnikiem "
                         "standardowym) mogą wygrywać pozornie – użyj dopasowania afinicznego.", {"d": num(a_h - a_a, 1)}))
    if n < 15:
        msgs.append(("Mała liczba punktów (< 15) – parametry odwzorowania mogą być słabo wyznaczone.", {}))
    b = next((c for c in cands if not c.is_reference and c.success), None)
    if b is not None:
        undetermined = [k for k, s in b.params_std_deg.items() if s > 10.0]
        e = np.hypot(b.residuals_px[:, 0], b.residuals_px[:, 1])
        if np.isfinite(b.sigma0_px) and b.sigma0_px > 0:
            suspects = [i for i in np.argsort(-e) if e[i] / b.sigma0_px > 3.0]
            if suspects:
                ids = ", ".join(f"#{labels[i] if labels else i + 1}" for i in suspects[:5])
                msgs.append(("Punkty o dużych resztach względem najlepszego odwzorowania: {ids}. "
                             "Błędy grube silnie zniekształcają parametry odwzorowania – sprawdź je w zakładce Analiza "
                             "i rozważ wyłączenie przed ponowną estymacją.", {"ids": ids}))
        if undetermined:
            msgs.append(("Słabo wyznaczone parametry najlepszego kandydata: {params} (błąd > 10°). "
                         "Prawdopodobnie nie mają wpływu na geometrię w tym zasięgu.",
                         {"params": [PARAM_LABELS.get(k, k) for k in undetermined]}))

    return EstimationResult(cands, mode, n, lon_rng, lat_rng, discriminable, spread, msgs,
                            lonlat=lonlat_deg, pixels=pixels)



def _rank(cands):
    """ΔAICc, wagi Akaike i sortowanie kandydatów (w miejscu)."""
    finite = [c.aicc for c in cands if np.isfinite(c.aicc) and c.success]
    best = min(finite) if finite else float("nan")
    for c in cands:
        c.delta_aicc = c.aicc - best if np.isfinite(c.aicc) else float("nan")
    rel = np.array([math.exp(-0.5 * c.delta_aicc) if np.isfinite(c.delta_aicc) else 0.0 for c in cands])
    tot = rel.sum()
    for c, w in zip(cands, rel):
        c.weight = float(w / tot) if tot > 0 else float("nan")
    cands.sort(key=lambda c: (not c.success, c.aicc if np.isfinite(c.aicc) else float("inf")))


# ---------------------------------------------------------------------------
#  Test hipotezy: odwzorowanie z parametrami ustalonymi przez użytkownika
# ---------------------------------------------------------------------------

def hypothesis_params(key):
    """Parametry, które użytkownik może ustalić (wpływające na kształt siatki)."""
    return tuple(PROJECTIONS_BY_KEY[key].free)


def evaluate_hypothesis(key, params_deg, lonlat_deg, pixels, mode="helmert") -> Candidate:
    """Ocena odwzorowania o ustalonych parametrach – dopasowywana jest tylko transformacja płaska."""
    proj = PROJECTIONS_BY_KEY[key]
    lonlat_deg = np.asarray(lonlat_deg, float)
    pixels = np.asarray(pixels, float)
    lon_c = float(np.mean(lonlat_deg[:, 0]))
    lat_c = float(np.mean(lonlat_deg[:, 1]))
    p_deg = {}
    for k, v in proj.fixed.items():
        p_deg[k] = lon_c if (k == "lon0" and v is None) else (v if v is not None else 0.0)
    if key == "plate":
        p_deg.setdefault("lat1", 0.0)
    defaults = {"lon0": lon_c, "lat0": lat_c, "lat1": lat_c, "lat2": lat_c}
    for k in proj.free:
        v = params_deg.get(k)
        p_deg[k] = float(v) if v is not None and np.isfinite(v) else defaults[k]

    lam = np.radians(lonlat_deg[:, 0])
    phi = np.radians(lonlat_deg[:, 1])
    with np.errstate(all="ignore"):
        x, y = proj.func(lam, phi, {k: math.radians(v) for k, v in p_deg.items()})
    P = np.column_stack([np.broadcast_to(x, lam.shape), np.broadcast_to(y, lam.shape)])
    if not np.all(np.isfinite(P)):
        raise EngineError("Odwzorowanie nie jest określone dla tych parametrów w obszarze punktów.")
    n = len(pixels)
    k_total = 6 if mode == "affine" else 4
    A, t, r = _fit_plane(P, pixels, mode)
    rss = float(np.sum(r ** 2))
    dof = 2 * n - k_total
    return Candidate(
        key=key, name=proj.name, family=proj.family, era=proj.era, note=proj.note,
        params_deg=p_deg, params_std_deg={}, n_free=0, rss=rss,
        sigma0_px=math.sqrt(rss / dof) if dof > 0 else float("nan"),
        aicc=aicc(rss, 2 * n, k_total), residuals_px=r, A=A, t=t, is_hypothesis=True,
        center=(lon_c, lat_c),
    )


def add_hypothesis(est, key, params_deg) -> Candidate:
    """Dodaje (lub zastępuje) hipotezę danego odwzorowania w rankingu i przelicza wagi."""
    if est.lonlat is None or est.pixels is None:
        raise EngineError("Brak danych estymacji.")
    cand = evaluate_hypothesis(key, params_deg, est.lonlat, est.pixels, est.fit_mode)
    est.candidates = [c for c in est.candidates if not (c.is_hypothesis and c.key == key)]
    est.candidates.append(cand)
    _rank(est.candidates)
    return cand


# Zestawy parametrów znanych map (λ₀ podana względem wskazanego południka zerowego).
PRESETS = [
    {"id": "tkkp", "name": "Topograficzna Karta Królestwa Polskiego (1843)", "key": "bonne",
     "params": {"lon0": "38°42′30″", "lat1": "52°"}, "meridian": "Ferro",
     "source": "Bonne; południk warszawski 38°42′30″ E Ferro (= 21°02′30″ E Gr.), φ₁ = 52° N. "
               "Kuna, Panecki, Zawadzki 2024, IJGI 13(7):249; Olszewicz."},
    {"id": "etatmajor", "name": "Carte de l'État-major (1:80 000)", "key": "bonne",
     "params": {"lon0": "0°", "lat1": "45°"}, "meridian": "Paryż",
     "source": "Bonne; południk paryski, równoleżnik 50 gradów (45° N)."},
    {"id": "dufour", "name": "Mapa Dufoura (Szwajcaria, 1:100 000)", "key": "bonne",
     "params": {"lon0": "7°26′22.5″", "lat1": "46°57′08.66″"}, "meridian": "Greenwich",
     "source": "Bonne; środek w dawnym obserwatorium w Bernie."},
    {"id": "cassini", "name": "Carte de Cassini (XVIII w.)", "key": "cassini",
     "params": {"lon0": "0°"}, "meridian": "Paryż",
     "source": "Cassini–Soldner; południk paryski."},
]


def meridian_offset(name):
    """Długość południka zerowego względem Greenwich [deg] (Greenwich = 0)."""
    for n, off in PRIME_MERIDIANS:
        if n == name:
            return off
    return 0.0


_ANGLE_ALLOWED = set("0123456789 .,°º'′\"″")


def parse_angle(text) -> float:
    """Kąt w stopniach: 21.0417, -17.6667, 38°42′30″, 38 42 30, 52°30'N, 17°40′ W, 21,5."""
    import re
    if text is None:
        raise ValueError("pusty kąt")
    t = str(text).strip().upper()
    if not t:
        raise ValueError("pusty kąt")
    sign = 1.0
    if t[-1] in "NSEW":
        if t[-1] in "SW":
            sign = -1.0
        t = t[:-1].strip()
    if t.startswith("-"):
        sign = -sign
        t = t[1:].strip()
    elif t.startswith("+"):
        t = t[1:].strip()
    if not t or any(ch not in _ANGLE_ALLOWED for ch in t):
        raise ValueError(f"niepoprawny zapis kąta: {text}")
    nums = re.findall(r"\d+(?:[.,]\d+)?", t)
    if not nums or len(nums) > 3:
        raise ValueError(f"niepoprawny zapis kąta: {text}")
    vals = [float(x.replace(",", ".")) for x in nums]
    if len(vals) > 1 and (vals[1] >= 60 or (len(vals) > 2 and vals[2] >= 60)):
        raise ValueError(f"minuty/sekundy poza zakresem: {text}")
    deg = vals[0] + (vals[1] / 60.0 if len(vals) > 1 else 0.0) + (vals[2] / 3600.0 if len(vals) > 2 else 0.0)
    return sign * deg


def prime_meridian_variants(lon_greenwich: float) -> list:
    """Długość λ₀ wyrażona względem historycznych południków zerowych."""
    out = []
    for name, off in PRIME_MERIDIANS:
        v = (lon_greenwich - off + 180.0) % 360.0 - 180.0
        out.append((name, v))
    return out


def format_dms(value: float, pos: str = "E", neg: str = "W") -> str:
    if not np.isfinite(value):
        return "–"
    hemi = pos if value >= 0 else neg
    v = abs(value)
    d = int(v)
    m_f = (v - d) * 60.0
    m = int(m_f)
    s = (m_f - m) * 60.0
    if s >= 59.95:
        s = 0.0
        m += 1
    if m >= 60:
        m = 0
        d += 1
    sec = f"{s:04.1f}".replace(".", decimal_sep())
    return f"{d}°{m:02d}′{sec}″ {hemi}"


def graticule_lines(cand: Candidate, lon_range, lat_range, step_deg: float, samples: int = 60) -> list:
    """Linie siatki geograficznej (południki i równoleżniki) w pikselach mapy dawnej.

    Zwraca listę (typ, wartość [deg], tablica (m, 2)).
    """
    lo0 = math.floor(lon_range[0] / step_deg) * step_deg
    lo1 = math.ceil(lon_range[1] / step_deg) * step_deg
    la0 = math.floor(lat_range[0] / step_deg) * step_deg
    la1 = math.ceil(lat_range[1] / step_deg) * step_deg
    la0, la1 = max(la0, -89.0), min(la1, 89.0)
    lines = []
    lats = np.linspace(la0, la1, samples)
    for lon in np.arange(lo0, lo1 + step_deg * 0.5, step_deg):
        xy = cand.to_pixels(np.full_like(lats, lon), lats)
        if np.all(np.isfinite(xy)):
            lines.append(("meridian", float(lon), xy))
    lons = np.linspace(lo0, lo1, samples)
    for lat in np.arange(la0, la1 + step_deg * 0.5, step_deg):
        xy = cand.to_pixels(lons, np.full_like(lons, lat))
        if np.all(np.isfinite(xy)):
            lines.append(("parallel", float(lat), xy))
    return lines


def graticule_label(value: float, pos: str, neg: str) -> str:
    """Etykieta linii siatki, np. 52°30′N (sekundy tylko gdy niezerowe)."""
    hemi = pos if value >= 0 else neg
    total = round(abs(value) * 3600)
    d, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    out = f"{d}°"
    if m or sec:
        out += f"{m:02d}′"
    if sec:
        out += f"{sec:02d}″"
    return out + hemi


def step_label(step: float) -> str:
    return graticule_label(step, "", "")


def auto_graticule_step(lon_range, lat_range) -> float:
    span = max(lon_range[1] - lon_range[0], lat_range[1] - lat_range[0], 1e-6)
    for s in (1 / 60, 2 / 60, 5 / 60, 10 / 60, 15 / 60, 20 / 60, 0.5, 1, 2, 5, 10, 15, 30):
        if span / s <= 8:
            return s
    return 30.0
