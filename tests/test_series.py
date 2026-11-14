"""
Testy serii czasowej – porównanie map z różnych lat (bez GUI QGIS).

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_series.py
"""
import json
import math
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

lb = __import__(f"{PKG}.library", fromlist=["*"])
sr = __import__(f"{PKG}.series", fromlist=["*"])

TMP = tempfile.mkdtemp(prefix="qma_series_")
N = 15
PX = 40.0                                     # metry na piksel skanu


def make_library(path):
    """Trzy mapy zestawu „S”: B kopiuje błędy A (z małym szumem), C ma błędy niezależne."""
    rng = np.random.default_rng(7)
    lib = lb.Library.create(path, "EPSG:2180")
    ref = {f"P{i:02d}": (600000.0 + 3000 * (i % 5) + 500 * (i // 5), 300000.0 + 2500 * (i // 5)) for i in range(N)}
    pts = [lb.RefPoint(n, *xy) for n, xy in ref.items()]
    lib.add_points(pts)
    uid = {p.name: p.uid for p in pts}
    sid = lib.add_set("S", uids=list(uid.values()))
    e_a = {n: rng.normal(0, 300, 2) for n in ref}
    e_b = {n: e_a[n] + rng.normal(0, 30, 2) for n in ref}
    e_c = {n: rng.normal(0, 300, 2) for n in ref}

    def links(errors, angle=0.0):
        c, s = math.cos(angle), math.sin(angle)
        out = []
        for n, (x, y) in ref.items():
            gx, gy = x + errors[n][0] - 600000, y + errors[n][1] - 300000     # położenie „na mapie”
            ox, oy = (c * gx + s * gy) / PX, (-s * gx + c * gy) / PX           # skan: obrót i skala
            out.append(lb.Link(ox, -oy, x, y, uid=uid[n], name=n))
        return out

    ids = []
    for title, year, errors, angle, dpi in (("Mapa A", 1770, e_a, 0.05, 300), ("Mapa B", 1800, e_b, -0.02, None),
                                            ("Mapa C", 1830, e_c, 0.0, None)):
        aid = lib.save_analysis(lb.Analysis(title=title, year_from=year, dpi=dpi, ref_crs="EPSG:2180"))
        lib.save_links(aid, links(errors, angle))
        ids.append(aid)
    other = lib.save_analysis(lb.Analysis(title="Bez punktów zestawu"))
    lib.save_links(other, [lb.Link(1, 1, 2, 2), lb.Link(2, 1, 3, 2), lb.Link(1, 2, 2, 3)])
    return lib, sid, uid, ids, e_a


def map_ids(lib, *aids):
    return [lib.get_analysis(a).map_id for a in aids]


def test_sheets_and_errors():
    lib, sid, uid, (a, b, c), e_a = make_library(os.path.join(TMP, "s.gpkg"))
    with lib:
        ser = sr.build_series(lib, sid, "affine", permutations=199)
        ma, mb, mc = map_ids(lib, a, b, c)
    assert [s.key for s in ser.maps] == [ma, mb, mc]
    sa = ser.get(ma)
    assert sa.sheet_count == 1 and sa.parts[0].analysis.analysis_id == a
    assert sa.fitted and sa.n == N and 100 < sa.rmse_m < 500 and sa.m_per_unit == 1.0
    # skala: 40 m/px przy 300 DPI → 1 : 472 441 (skos mapy błędów zmienia ją nieznacznie)
    assert abs(sa.scale_den / (PX * 300 / 0.0254) - 1) < 0.03, sa.scale_den
    assert abs(sa.rotation_deg - math.degrees(-0.05)) < 1.0 or abs(sa.rotation_deg - math.degrees(0.05)) < 1.0
    assert math.isnan(ser.get(mb).scale_den)                          # bez DPI
    # błąd położenia punktu ≈ wprowadzony błąd mapy (bez części wchłoniętej przez transformację)
    corr = sr.vector_correlation(np.array([complex(*sa.vec[uid[n]]) for n in e_a]),
                                 np.array([complex(*e_a[n]) for n in e_a]))
    assert corr > 0.7, corr                                           # część błędu wchłania afiniczna
    assert all(abs(sa.err_m[u] - math.hypot(*sa.vec[u])) < 1e-6 for u in sa.vec)
    n, mean, _v = ser.point_summary(uid["P03"])
    assert n == 3 and mean > 0 and len(ser.trajectory(uid["P03"])) == 3


def test_copied_errors_detected():
    lib, sid, uid, (a, b, c), _e = make_library(os.path.join(TMP, "p.gpkg"))
    with lib:
        ser = sr.build_series(lib, sid, permutations=199)
        a, b, c = map_ids(lib, a, b, c)
    pairs = {(p.a, p.b): p for p in ser.pairs}
    assert set(pairs) == {(a, b), (a, c), (b, c)}
    ab, ac = pairs[(a, b)], pairs[(a, c)]
    assert ab.n == N and ab.r > 0.8 and ab.p < 0.01 and ab.similar, ab
    assert not ac.similar and abs(ac.r) < 0.5, ac
    assert ab.diff_m < 100 < ac.diff_m
    assert ser.similar_pairs() == [ab]


def test_current_state_and_export():
    lib, sid, uid, (a, b, c), _e = make_library(os.path.join(TMP, "e.gpkg"))
    with lib:
        links = lib.links(c)[:6]                                        # bieżący stan C: tylko 6 punktów
        ser = sr.build_series(lib, sid, current=(lib.get_analysis(c), links), permutations=99)
        ma, mb, mc = map_ids(lib, a, b, c)
        assert ser.current_id == mc and ser.get(mc).n == 6
        assert next(p for p in ser.pairs if (p.a, p.b) == (ma, mc)).n == 6
        only = sr.build_series(lib, sid, analysis_ids={a, b}, permutations=99)
        assert [s.key for s in only.maps] == [ma, mb]
    text = sr.maps_csv(ser).splitlines()
    assert text[0].startswith("key,map,sheets,year_from") and text[1].split(",")[1] == "Mapa A"
    pts = sr.points_csv(ser).splitlines()
    assert pts[0].endswith(",maps,mean_m") and len(pts) == N + 1
    data = json.loads(sr.to_json(ser))
    assert len(data["maps"]) == 3 and data["pairs"][0]["similar"] is True
    assert data["maps"][1]["scale_den"] is None and data["maps"][0]["analysis_ids"] == [a]


def test_unknown_set():
    with lb.Library.create(os.path.join(TMP, "u.gpkg"), "EPSG:2180") as lib:
        try:
            sr.build_series(lib, 99)
            raise AssertionError("unknown set accepted")
        except lb.LibraryError:
            pass
        sid = lib.add_set("pusty")
        assert sr.build_series(lib, sid).maps == []


def test_multisheet_publication_is_one_map():
    """Wydawnictwo wieloarkuszowe: arkusze wyrównane osobno, w serii – jeden element; pary tylko między mapami."""
    lib, sid, uid, (a, b, c), _e = make_library(os.path.join(TMP, "m.gpkg"))
    with lib:
        links = lib.links(a)
        mid = lib.get_analysis(a).map_id
        # arkusz 2 tej samej mapy: punkty P08–P14 (P08–P09 – zakładka, także na arkuszu 1), inny obrót skanu
        a2 = lib.save_analysis(lb.Analysis(sheet="2", map_id=mid, ref_crs="EPSG:2180"))
        lib.save_links(a2, [lb.Link(-ln.y_old, -ln.x_old, ln.ref_x, ln.ref_y, uid=ln.uid, name=ln.name)
                            for ln in links[8:]])
        lib.save_links(a, links[:10])                                     # arkusz 1: P00–P09
        ser = sr.build_series(lib, sid, permutations=99)
    keys = [m.key for m in ser.maps]
    assert len(keys) == 3 and keys[0] == mid, keys
    z = ser.get(mid)
    assert z.sheet_count == 2 and z.n == 10 + 7 and len(z.vec) == N   # wszystkie punkty zestawu na mapie
    assert sorted(p.analysis.analysis_id for p in z.parts) == [a, a2]
    assert math.isfinite(z.rmse_m) and min(p.rmse_m for p in z.parts) <= z.rmse_m <= max(p.rmse_m for p in z.parts)
    assert all((p.a, p.b) != (mid, mid) for p in ser.pairs) and len(ser.pairs) == 3


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
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} OK")
    sys.exit(1 if failed else 0)
