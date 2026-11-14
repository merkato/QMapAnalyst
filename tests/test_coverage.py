"""
Testy macierzy pokrycia punkt × mapa (bez GUI QGIS).

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_coverage.py
"""
import json
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
cv = __import__(f"{PKG}.coverage", fromlist=["*"])

TMP = tempfile.mkdtemp(prefix="qma_cov_")


def make_library(path):
    """14 punktów zestawu „K”, 3 mapy z punktami zestawu i jedna bez nich.
    Test Baardy wykryje pojedynczy błąd gruby tylko przy dostatecznej nadwyżce (max |w| ≤ √r)."""
    rng = np.random.default_rng(11)
    lib = lb.Library.create(path, "EPSG:2180")
    pix = {f"P{i:02d}": (100.0 + 130 * i, -(150.0 + 110 * ((5 * i) % 14))) for i in range(14)}

    def ref(px, py, scale=40.0):
        return 600000 + scale * px, 300000 + scale * py

    pts = [lb.RefPoint(n, *ref(*xy)) for n, xy in pix.items()]
    lib.add_points(pts)
    uid = {p.name: p.uid for p in pts}
    sid = lib.add_set("K", uids=list(uid.values()))
    extra = lib.add_point(lb.RefPoint("Inny", 0.0, 0.0))

    def link(name, status="sure", enabled=True, gross=0.0):
        px, py = pix[name]
        rx, ry = ref(px, py)
        nx, ny = rng.normal(0, 1.0, 2)
        return lb.Link(px, py, rx + nx + gross, ry + ny, uid=uid[name], name=name, status=status, enabled=enabled)

    a1 = lib.save_analysis(lb.Analysis(title="Zannoni", year_from=1772, scale=280000))
    lib.save_links(a1, [link(f"P{i:02d}", gross=600.0 if i == 3 else 0.0) for i in range(13)]
                   + [lb.Link(uid=uid["P13"], name="P13", status="absent", comment="pominięty")])
    a2 = lib.save_analysis(lb.Analysis(title="Mapa 1830", year_from=1826, year_to=1830))
    lib.save_links(a2, [link("P02", "uncertain") if i == 2 else link("P05", enabled=False) if i == 5
                        else link(f"P{i:02d}", gross=800.0 if i == 3 else 0.0) for i in range(13)])
    a3 = lib.save_analysis(lb.Analysis(title="Bez daty"))
    lib.save_links(a3, [link("P00"), link("P01"), link("P02"), link("P03", "illegible")])
    a4 = lib.save_analysis(lb.Analysis(title="Inna mapa", year_from=1700))
    lib.save_links(a4, [lb.Link(1, 1, 0, 0, uid=extra, name="Inny"), lb.Link(2, 2, 5, 5), lb.Link(3, 1, 9, 0)])
    return lib, sid, uid, (a1, a2, a3, a4)


def test_coverage_matrix():
    lib, sid, uid, (a1, a2, a3, a4) = make_library(os.path.join(TMP, "m.gpkg"))
    with lib:
        cov = cv.build_coverage(lib, sid, "affine")
    assert [a.analysis_id for a in cov.analyses] == [a1, a2, a3]          # wg roku, bez daty na końcu; a4 pominięta
    assert [p.name for p in cov.points] == [f"P{i:02d}" for i in range(14)]
    c = cov.cell
    assert c(uid["P00"], a1) == "sure" and c(uid["P13"], a1) == "absent"
    assert c(uid["P03"], a1) == "outlier" and c(uid["P03"], a2) == "outlier", (c(uid["P03"], a1), c(uid["P03"], a2))
    assert c(uid["P02"], a2) == "uncertain" and c(uid["P05"], a2) == "disabled"
    assert c(uid["P03"], a3) == "illegible" and c(uid["P04"], a3) is None
    assert cov.fitted == {a1: True, a2: True, a3: False}                  # a3: 3 punkty – za mało dla afinicznej
    assert cov.common() == [uid["P00"], uid["P01"], uid["P02"]]
    assert cov.repeated_outliers() == [uid["P03"]]
    assert cov.row_count(uid["P03"]) == 2 and cov.column_count(a1) == 13
    assert sum(1 for p in cov.points for a in cov.analyses if c(p.uid, a.analysis_id) == "outlier") == 2


def test_helmert_and_selected_columns():
    lib, sid, uid, (a1, a2, a3, _a4) = make_library(os.path.join(TMP, "h.gpkg"))
    with lib:
        cov = cv.build_coverage(lib, sid, "helmert", analysis_ids={a2, a3})
    assert [a.analysis_id for a in cov.analyses] == [a2, a3]
    assert cov.fitted[a3] is True                                         # Helmert: 3 > 2 punkty
    assert cov.cell(uid["P03"], a2) == "outlier"


def test_export():
    lib, sid, uid, (a1, a2, a3, _a4) = make_library(os.path.join(TMP, "e.gpkg"))
    with lib:
        cov = cv.build_coverage(lib, sid)
    text = cv.to_csv(cov).splitlines()
    assert text[0] == "uid,name,Zannoni (1772),Mapa 1830 (1826–1830),Bez daty,present", text[0]
    assert text[1].startswith(f"{uid['P00']},P00,sure,sure,sure,3")
    assert text[14].endswith(",P13,absent,,,0")
    data = json.loads(cv.to_json(cov))
    assert data["set"]["name"] == "K" and data["common"] == [uid["P00"], uid["P01"], uid["P02"]]
    assert data["points"][3]["cells"][str(a1)] == "outlier" and data["analyses"][2]["outliers_tested"] is False


def test_current_state():
    lib, sid, uid, (a1, a2, a3, _a4) = make_library(os.path.join(TMP, "c.gpkg"))
    with lib:
        links = lib.links(a3) + [lb.Link(uid=uid["P04"], name="P04", status="absent")]
        cov = cv.build_coverage(lib, sid, current=(lib.get_analysis(a3), links))
        assert cov.current_id == a3 and cov.cell(uid["P04"], a3) == "absent"   # stan bieżący zamiast zapisanego
        new = [lb.Link(10, 10, 5, 5, uid=uid["P00"], name="P00"), lb.Link(uid=uid["P01"], name="P01", status="absent")]
        cov = cv.build_coverage(lib, sid, current=(None, new))
        cur = next(a for a in cov.analyses if a.analysis_id == cv.CURRENT)
        assert cov.current_id == cv.CURRENT and len(cov.analyses) == 4
        assert cov.cell(uid["P00"], cv.CURRENT) == "sure" and cov.cell(uid["P01"], cv.CURRENT) == "absent"
        assert cv.analysis_label(cur) == "current"
        cov = cv.build_coverage(lib, sid, analysis_ids={a1}, current=(None, new))
        assert [a.analysis_id for a in cov.analyses] == [a1, cv.CURRENT]


def test_unknown_set_and_empty():
    lib = lb.Library.create(os.path.join(TMP, "empty.gpkg"), "EPSG:2180")
    with lib:
        sid = lib.add_set("pusty")
        cov = cv.build_coverage(lib, sid)
        assert cov.points == [] and cov.analyses == [] and cov.common() == []
        try:
            cv.build_coverage(lib, 999)
            raise AssertionError("unknown set accepted")
        except lb.LibraryError:
            pass


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
