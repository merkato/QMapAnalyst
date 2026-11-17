"""
Testy: spójność punktów w serii i porównanie pól korekcji (#5) – bez GUI QGIS.

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_series_fields.py
"""
import math
import os
import sys
from types import SimpleNamespace

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

sf = __import__(f"{PKG}.series_fields", fromlist=["*"])

N = 40
rng0 = np.random.default_rng(3)
POS = {f"P{i}": tuple(rng0.uniform(0, 50000, 2)) for i in range(N)}


def field_a(x, y):
    return np.array([600 * math.sin(x / 9000.0), 500 * math.cos(y / 8000.0)])


def field_c(x, y):
    return np.array([500 * math.cos(y / 6000.0 + 1), -600 * math.sin(x / 7000.0 + 2)])


def make_map(key, fn, noise, seed, extra=None):
    rng = np.random.default_rng(seed)
    vec = {u: tuple(fn(*p) + rng.normal(0, noise, 2)) for u, p in POS.items()}
    for u, v in (extra or {}).items():
        vec[u] = tuple(np.array(vec[u]) + v)
    err = {u: float(np.hypot(*v)) for u, v in vec.items()}
    rmse = float(np.sqrt(np.mean([e * e for e in err.values()])))
    return SimpleNamespace(key=key, vec=vec, pos=dict(POS), err_m=err, fitted=True, rmse_m=rmse)


def series(maps):
    return SimpleNamespace(maps=maps, points=[SimpleNamespace(uid=u, name=u) for u in POS])


def test_copied_fields_similar_independent_not():
    a = make_map(1, field_a, 40, 1)
    b = make_map(2, field_a, 40, 2)                  # kopia: to samo pole + szum
    c = make_map(3, field_c, 40, 3)                  # niezależna
    ab = sf.compare_fields(a, b, permutations=199)
    ac = sf.compare_fields(a, c, permutations=199)
    assert ab.ok and ab.nodes >= sf.MIN_GRID_NODES
    assert ab.r_field > 0.8 and ab.p_field < 0.05 and ab.mantel_r > 0.6 and ab.mantel_p < 0.05, (ab.r_field, ab.p_field, ab.mantel_r, ab.mantel_p)
    assert abs(ac.r_field) < 0.5 and ac.p_field > 0.05, (ac.r_field, ac.p_field)
    assert ab.r_dilation > ac.r_dilation and ab.r_rotation > 0.5
    assert np.hypot(*ab.diff.T).mean() < np.hypot(*ac.diff.T).mean()
    allp = sf.compare_series_fields(series([a, b, c]), permutations=99)
    assert len(allp) == 3 and (allp[0].a, allp[0].b) == (1, 2)


def test_no_common_area_and_few_points():
    a = make_map(1, field_a, 40, 1)
    far = make_map(2, field_a, 40, 2)
    far.pos = {u: (x + 1e6, y) for u, (x, y) in far.pos.items()}
    assert sf.compare_fields(a, far).warning == "no_common_area"
    few = make_map(3, field_a, 40, 3)
    few.vec = dict(list(few.vec.items())[:4])
    assert sf.compare_fields(a, few).warning == "too_few_points"


def test_suspect_point_in_library():
    shift = {"P5": np.array([2500.0, 1500.0])}      # błąd współrzędnych P5 w bibliotece – na każdej mapie
    maps = [make_map(k, fn, 40, k, shift) for k, fn in ((1, field_a), (2, field_c), (3, field_a), (4, field_c))]
    sus = sf.suspect_points(series(maps))
    assert [s.uid for s in sus] == ["P5"] and sus[0].coherence > 0.9 and sus[0].maps == 4
    assert sf.suspect_points(series(maps[:2])) == []                 # za mało map


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
