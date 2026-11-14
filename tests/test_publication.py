"""
Testy trybu „wydawnictwo wieloarkuszowe” – zgodność arkuszy jednej mapy (bez GUI QGIS).

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_publication.py
"""
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
pb = __import__(f"{PKG}.publication", fromlist=["*"])

TMP = tempfile.mkdtemp(prefix="qma_pub_")


def make_library(path):
    """Mapa z trzema arkuszami (1, 2, 10) w pasie W–E; zakładki między sąsiednimi arkuszami;
    arkusz 2 w innej skali skanu, a punkt Z1 ma na nim błąd 500 m."""
    rng = np.random.default_rng(3)
    lib = lb.Library.create(path, "EPSG:2180")
    ref = {f"Z{i}": (600000.0 + 4000 * i, 300000.0 + 3000 * (i % 3)) for i in range(18)}
    pts = [lb.RefPoint(n, *xy) for n, xy in ref.items()]
    lib.add_points(pts)
    uid = {p.name: p.uid for p in pts}
    mid = lib.save_map(lb.MapInfo(title="Flötzkarte", year_from=1857))
    sheets = {"1": (range(0, 7), 40.0, 600000.0), "2": (range(5, 13), 44.0, 620000.0), "10": (range(11, 18), 40.0, 644000.0)}
    ids = {}
    for name, (rows, px, x0) in sheets.items():
        links, frame_x = [], []
        for i in rows:
            n = f"Z{i}"
            x, y = ref[n]
            ex, ey = rng.normal(0, 20, 2)
            if name == "2" and n == "Z1":
                ex += 500.0
            ox, oy = (x + ex - x0) / px, -(y + ey - 300000.0) / px - 100
            links.append(lb.Link(ox, oy, x, y, uid=uid[n], name=n))
            frame_x.append(ox)
        frame = [(min(frame_x) - 20, 0), (max(frame_x) + 20, 0), (max(frame_x) + 20, -400), (min(frame_x) - 20, -400)]
        aid = lib.save_analysis(lb.Analysis(sheet=name, map_id=mid, dpi=300, ref_crs="EPSG:2180", frame=lb.frame_text(frame)))
        lib.save_links(aid, links)
        ids[name] = aid
    # arkusz 2: punkt Z1 na zakładce z arkuszem 1 (dopisany z błędem)
    links = lib.links(ids["2"])
    x, y = ref["Z1"]
    links.append(lb.Link((x + 500 - 620000.0) / 44.0, -(y - 300000.0) / 44.0 - 100, x, y, uid=uid["Z1"], name="Z1"))
    lib.save_links(ids["2"], links)
    other = lib.save_map(lb.MapInfo(title="Inna"))
    lib.save_analysis(lb.Analysis(sheet="1", map_id=other))
    return lib, mid, uid, ids


def test_sheets_spread_and_order():
    lib, mid, uid, ids = make_library(os.path.join(TMP, "p.gpkg"))
    with lib:
        pub = pb.build_publication(lib, mid)
    assert [s.analysis.sheet for s in pub.sheets] == ["1", "2", "10"]   # numery liczbowo
    s1, s2, s10 = pub.sheets
    assert s1.n == 7 and s2.n == 9 and s10.n == 7
    # skala: 40 m/px przy 300 DPI → 1 : 472 441; arkusz 2: 44 m/px → 1 : 519 685
    assert abs(s1.scale_den / 472441 - 1) < 0.01 and abs(s2.scale_den / 519685 - 1) < 0.03, (s1.scale_den, s2.scale_den)
    lo, hi = pub.spread("scale_den")
    assert lo == min(s1.scale_den, s10.scale_den) and hi == s2.scale_den
    assert 8 < pub.scale_spread_pct() < 12, pub.scale_spread_pct()
    assert all(s.footprint and len(s.footprint) >= 4 for s in pub.sheets)
    xs = [x for x, _y in s1.footprint]
    assert 599000 < min(xs) and max(xs) < 626000                       # zasięg arkusza 1 w układzie biblioteki


def test_overlaps():
    lib, mid, uid, ids = make_library(os.path.join(TMP, "o.gpkg"))
    with lib:
        pub = pb.build_publication(lib, mid)
    pairs = {(o.name, o.a, o.b) for o in pub.overlaps}
    assert ("Z5", ids["1"], ids["2"]) in pairs and ("Z6", ids["1"], ids["2"]) in pairs
    assert ("Z11", ids["2"], ids["10"]) in pairs and ("Z12", ids["2"], ids["10"]) in pairs
    assert len(pub.overlaps) == 5                                       # Z1, Z5, Z6 (1–2), Z11, Z12 (2–10)
    worst = pub.overlaps[0]
    assert worst.name == "Z1" and 150 < worst.diff_m < 700, worst          # błąd 500 m (część wchłania arkusz 2)
    assert all(o.diff_m < worst.diff_m for o in pub.overlaps[1:]), [(o.name, o.diff_m) for o in pub.overlaps]
    text = pb.sheets_csv(pub)
    assert text.startswith("analysis_id,sheet,points") and "overlap_uid,name,sheet_a,sheet_b,diff_m" in text


def test_current_state_and_errors():
    lib, mid, uid, ids = make_library(os.path.join(TMP, "c.gpkg"))
    with lib:
        links = lib.links(ids["10"])[:4]
        pub = pb.build_publication(lib, mid, current=(lib.get_analysis(ids["10"]), links))
        assert pub.current_id == ids["10"] and pub.sheet(ids["10"]).n == 4
        try:
            pb.build_publication(lib, 999)
            raise AssertionError("unknown map accepted")
        except lb.LibraryError:
            pass
    assert math.isfinite(pub.sheet(ids["1"]).rmse_m)


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
