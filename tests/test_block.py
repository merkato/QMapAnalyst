"""
Testy wyrównania blokowego arkuszy (#8) – bez GUI QGIS.

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_block.py
"""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

bk = __import__(f"{PKG}.block", fromlist=["*"])

W, H = 1000.0, 800.0                       # ramka arkusza na skanie (piksele)


def world(r, c, px, py):
    """Jedna transformacja całej mapy: arkusz (r, c), punkt skanu (px, py) → teren (10 m/px, obrót 5°)."""
    gx, gy = c * W + px, -r * H + py
    a = math.radians(5)
    return 500000 + 10 * (gx * math.cos(a) - gy * math.sin(a)), 300000 + 10 * (gx * math.sin(a) + gy * math.cos(a))


def make_sheets(shift=None, no_gcp=None, noise=5.0, seed=2):
    rng = np.random.default_rng(seed)
    sheets = []
    for r in range(2):
        for c in range(2):
            src = np.column_stack([rng.uniform(50, W - 50, 8), rng.uniform(-H + 50, -50, 8)])
            dst = np.array([world(r, c, *p) for p in src]) + rng.normal(0, noise, (8, 2))
            if shift == (r, c):
                dst += [2000.0, 0.0]
            if no_gcp == (r, c):
                src, dst = np.zeros((0, 2)), np.zeros((0, 2))
            frame = [(0, 0), (W, 0), (W, -H), (0, -H)]
            sheets.append(bk.BlockSheet(len(sheets), f"{r}{c}", frame, src, dst, [""] * len(src), [""] * len(src)))
    return sheets


def ties_between(sheets, n=3, seed=5):
    """Punkty wiążące wzdłuż wspólnych krawędzi (ten sam obiekt na dwóch arkuszach)."""
    rng = np.random.default_rng(seed)
    out = []
    for ia, ib, edge in ((0, 1, "v"), (2, 3, "v"), (0, 2, "h"), (1, 3, "h")):
        for _ in range(n):
            if edge == "v":
                y = rng.uniform(-H + 50, -50)
                out.append((ia, (W - 2.0, y), ib, (-2.0 + 0.0 + 2.0 * 0, y), "t"))
            else:
                x = rng.uniform(50, W - 50)
                out.append((ia, (x, -H + 2.0), ib, (x, 2.0 - 0.0), "t"))
    return out


def test_detect_grid():
    sheets = make_sheets()
    g = bk.detect_grid(sheets)
    assert [g[i] for i in range(4)] == [(0, 0), (0, 1), (1, 0), (1, 1)], g
    for i, s in enumerate(sheets):
        s.grid = g[i]
    nodes = bk.nodes_from_grid(sheets)
    assert len(nodes) == 9 and len(nodes[(1, 1)]) == 4


def _with_grid(sheets):
    for i, s in enumerate(sheets):
        s.grid = (i // 2, i % 2)
    return sheets


def test_recovers_transformation_and_closes_seams():
    for model in ("helmert", "affine", "bilinear"):
        sheets = _with_grid(make_sheets())
        free = bk.adjust(sheets, model, seam_weight=0)
        tight = bk.adjust(sheets, model, seam_weight=100)
        assert tight.gap_stats()[1] < 1.0, (model, tight.gap_stats())
        assert tight.rmse < 12 and abs(tight.sigma0 - 5 * math.sqrt(2) / math.sqrt(2)) < 4, (model, tight.rmse, tight.sigma0)
        assert free.gap_stats()[0] > tight.gap_stats()[0]
        p = tight.transform(3, [(500.0, -400.0)])[0]
        assert np.hypot(*(p - world(1, 1, 500.0, -400.0))) < 15


def test_sheet_without_gcp_from_neighbours():
    sheets = _with_grid(make_sheets(no_gcp=(1, 1)))
    res = bk.adjust(sheets, "affine", seam_weight=100)
    p = res.transform(3, [(500.0, -400.0)])[0]
    assert np.hypot(*(p - world(1, 1, 500.0, -400.0))) < 30, p - world(1, 1, 500.0, -400.0)
    res = bk.adjust(sheets, "affine", seam_weight=0, ties=ties_between(sheets))
    p = res.transform(3, [(500.0, -400.0)])[0]
    assert np.hypot(*(p - world(1, 1, 500.0, -400.0))) < 30
    try:
        bk.adjust(sheets, "affine", seam_weight=0, ties=[])
        raise AssertionError("arkusz bez punktów i warunków powinien dać układ nieoznaczony")
    except bk.BlockError:
        pass


def test_shifted_sheet_detected():
    sheets = _with_grid(make_sheets(shift=(0, 1)))
    res = bk.adjust(sheets, "affine", seam_weight=100)
    assert res.outlier_sheets == [1], (res.outlier_sheets, res.sheet_sigma0)
    free = bk.adjust(sheets, "affine", seam_weight=0)
    assert not free.outlier_sheets                       # osobno arkusz „pochłania” przesunięcie
    assert max(g.gap for g in free.corner_gaps()) > 1500


def test_tradeoff_curve_and_ties_std():
    sheets = _with_grid(make_sheets())
    curve = bk.tradeoff(sheets, "affine")
    assert curve[0][1] == 0 and curve[-1][1] == 100
    assert curve[0][3] >= curve[-1][3] and curve[0][2] <= curve[-1][2] + 1e-6
    res = bk.adjust(sheets, "affine", seam_weight=1, ties=ties_between(sheets))
    assert len(res.tie_std) == 12 and all(math.isfinite(w) for w in res.tie_std)
    assert len([g for g in res.gaps if g.kind == "tie"]) == 12
    assert abs(bk.slider_to_weight(100) - 100) < 1e-9 and bk.slider_to_weight(0) == 0


def test_brick_layout_wrong_grid_not_stitched():
    """Rząd dolny przesunięty o pół arkusza, a siatka błędnie przypisana (wszystkie arkusze w jednej komórce):
    zszywane są tylko narożniki rzeczywiście bliskie, wyrównanie nie psuje arkuszy."""
    rng = np.random.default_rng(9)
    sheets = []
    for r in range(2):
        for c in range(2):
            off = 0.5 * W if r == 1 else 0.0
            src = np.column_stack([rng.uniform(50, W - 50, 8), rng.uniform(-H + 50, -50, 8)])
            dst = np.array([world(0, 0, c * W + off + x, -r * H + y) for x, y in src]) + rng.normal(0, 5, (8, 2))
            sheets.append(bk.BlockSheet(len(sheets), f"{r}{c}", [(0, 0), (W, 0), (W, -H), (0, -H)], src, dst,
                                        [""] * 8, [""] * 8, grid=(0, 0)))
    nodes = bk.corner_nodes(sheets)
    assert sorted(len(o) for o in nodes.values()) == [2, 2, 2, 2], nodes        # tylko styki w obrębie rzędów
    res = bk.adjust(sheets, "affine", seam_weight=100)
    assert res.rmse < 12 and res.gap_stats()[1] < 1.0, (res.rmse, res.gap_stats())


def test_leave_out_matches_direct_solution():
    """Poprawka Woodbury’ego = wyrównanie od nowa bez punktów arkusza."""
    sheets = _with_grid(make_sheets())
    A, b, p, tag, offs, _c = bk._design("affine", sheets, [], bk.corner_nodes(sheets), 10.0, 1.0)
    N = A.T @ (A * p[:, None])
    Q = np.linalg.inv(N)
    x = Q @ (A.T @ (b * p))
    res = bk.BlockResult("affine", 10.0, sheets, [x[o:o + 6] for o in offs])
    lo = bk.LeaveOut(res, A, b, p, tag, Q, x, offs)
    for removed in ([1], [3], [1, 2], [0, 2]):
        reduced = list(sheets)
        for k in removed:
            t = sheets[k]
            reduced[k] = bk.BlockSheet(t.key, t.label, t.frame, np.zeros((0, 2)), np.zeros((0, 2)), grid=t.grid)
        try:
            direct = bk.adjust(reduced, "affine", 10.0, ties=[], nodes=bk.corner_nodes(sheets), check_sheets=False)
        except bk.BlockError:
            assert lo.params(removed) is None, removed                     # nieoznaczony w obu metodach
            continue
        assert np.allclose(lo.params(removed), np.concatenate(direct.params), atol=1e-6), removed


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
