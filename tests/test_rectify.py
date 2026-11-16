"""
Testy silnika rektyfikacji (bez GUI QGIS, tylko numpy).

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_rectify.py
"""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

rc = __import__(f"{PKG}.rectify", fromlist=["*"])


def synthetic(n=60, noise=20.0, seed=3):
    """Punkty terenu ~40×30 km; reszty = gładkie pole (do 1,5 km) + szum."""
    rng = np.random.default_rng(seed)
    P = np.column_stack([rng.uniform(0, 40000, n), rng.uniform(0, 30000, n)])

    def smooth(Q):
        return np.column_stack([1500 * np.sin(Q[:, 0] / 9000.0), 1000 * np.cos(Q[:, 1] / 7000.0)])

    E = smooth(P) + rng.normal(0, noise, (n, 2))
    return P, E, smooth


def test_smooth_tps_auto_recovers_field():
    P, E, smooth = synthetic()
    c = rc.build_correction(P, E, "tps")
    assert c.auto and rc.LAMBDA_MIN <= c.lam <= rc.LAMBDA_MAX
    assert c.rmse_before > 800 and c.rmse_after < 60, (c.rmse_before, c.rmse_after)
    # pole odtworzone także między punktami
    rng = np.random.default_rng(11)
    Q = np.column_stack([rng.uniform(5000, 35000, 200), rng.uniform(5000, 25000, 200)])
    err = np.hypot(*(c(Q) - smooth(Q)).T)
    assert np.sqrt((err ** 2).mean()) < 120, np.sqrt((err ** 2).mean())
    assert c.loo_rmse < 150


def test_loo_matches_brute_force():
    P, E, _s = synthetic(n=25)
    lam = 1e-2
    t = rc.SmoothTPS(P, E, lam)
    brute = []
    for i in range(len(P)):
        idx = np.delete(np.arange(len(P)), i)          # ta sama normalizacja co pełny model
        brute.append(E[i] - rc.SmoothTPS(P[idx], E[idx], lam, norm=(t.c, t.s))(P[i:i + 1])[0])
    assert np.allclose(t.loo, np.array(brute), atol=1e-6 * np.abs(E).max()), np.abs(t.loo - np.array(brute)).max()


def test_exact_and_triangles_interpolate():
    P, E, _s = synthetic(n=30)
    for m in ("tps_exact", "triangles"):
        c = rc.build_correction(P, E, m)
        assert c.rmse_after < 1e-3, (m, c.rmse_after)
        assert math.isfinite(c.loo_rmse)


def test_lambda_slider_and_weights():
    assert abs(rc.lambda_to_slider(rc.slider_to_lambda(37)) - 37) < 1e-9
    P, E, _s = synthetic(n=30)
    loose = rc.build_correction(P, E, "tps", lam=rc.slider_to_lambda(5))
    stiff = rc.build_correction(P, E, "tps", lam=rc.slider_to_lambda(95))
    assert loose.rmse_after < stiff.rmse_after
    # punkt z małą wagą – korekcja słabiej go dociąga
    E2 = E.copy()
    E2[0] += 5000
    w = np.ones(len(P))
    w[0] = 0.01
    a = rc.build_correction(P, E2, "tps", lam=1e-3)
    b = rc.build_correction(P, E2, "tps", lam=1e-3, w=w)
    assert np.hypot(*b.after[0]) > np.hypot(*a.after[0])


def test_mls_reproduces_affine():
    rng = np.random.default_rng(5)
    P = rng.uniform(0, 1000, (20, 2))
    A = np.array([[1.02, 0.05], [-0.03, 0.97]])
    target = P @ A.T + [30, -12]
    c = rc.build_correction(P, P - target, "mls")
    Q = rng.uniform(100, 900, (50, 2))
    assert np.allclose(c.apply(Q), Q @ A.T + [30, -12], atol=1e-6)


def _hull_area(p):
    p = sorted(map(tuple, p))

    def half(seq):
        h = []
        for q in seq:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (q[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (q[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(q)
        return h
    hull = half(p)[:-1] + half(p[::-1])[:-1]
    return 0.5 * abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(hull, hull[1:] + hull[:1])))


def test_delaunay():
    ang = np.linspace(0, 2 * np.pi, 12, endpoint=False)
    fan = np.vstack([[[0, 0]], np.column_stack([np.cos(ang), np.sin(ang)]) * 100])
    assert len(rc.delaunay(fan)) == 12
    pts = np.random.default_rng(1).uniform(0, 1000, (80, 2))
    t = np.array(rc.delaunay(pts))
    a, b, c = pts[t[:, 0]], pts[t[:, 1]], pts[t[:, 2]]
    area = 0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    assert abs(area.sum() - _hull_area(pts)) < 1e-6 * _hull_area(pts)          # pokrycie otoczki bez zakładek


def test_fold_check_and_grid():
    frame = [(0, 0), (1000, 0), (1000, -800), (0, -800)]
    _f, mask = rc.fold_check(lambda p: p * 2.0, frame, n=20)
    assert not mask.any()

    def folded(p):
        q = p.copy()
        q[:, 0] = np.where(p[:, 0] > 500, 1000 - p[:, 0], p[:, 0])   # zagięcie wzdłuż x = 500
        return q
    frac, mask = rc.fold_check(folded, frame, n=20)
    assert 0.3 < frac <= 0.5 and mask.any()
    g = rc.gcp_grid(lambda p: p + [100, 200], [(0, -800), (1000, 0), (0, 0), (1000, -800)], k=4)
    assert len(g) == 25 and g[0] == (0.0, 0.0, 100.0, 200.0) and g[-1] == (1000.0, 800.0, 1100.0, -600.0)
    ring = rc.frame_ring(frame, per_edge=10)
    assert len(ring) == 40 and tuple(ring[0]) == (0.0, 0.0) and tuple(ring[10]) == (1000.0, 0.0)


def test_rectifier_composition():
    P, E, _s = synthetic(n=30)
    c = rc.build_correction(P, E, "tps_exact")
    r = rc.Rectifier(lambda s: s + [1000, 0], c)
    src = P - [1000, 0]
    assert np.allclose(r(src), P - E, atol=1e-3)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f"OK   {n}")
        except Exception as e:  # noqa: BLE001 – raport z uruchomienia ręcznego
            failed += 1
            print(f"FAIL {n}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} OK")
    sys.exit(1 if failed else 0)
