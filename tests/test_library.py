"""
Testy biblioteki punktów referencyjnych (GeoPackage, bez GUI QGIS).

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_library.py
lub:                      python -m pytest tests/test_library.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

lib_mod = __import__(f"{PKG}.library", fromlist=["*"])
Library, RefPoint, Analysis, Link = lib_mod.Library, lib_mod.RefPoint, lib_mod.Analysis, lib_mod.Link

TMP = tempfile.mkdtemp(prefix="qma_lib_")


def _new(name):
    return Library.create(os.path.join(TMP, name + ".gpkg"), "EPSG:2180")


def _kielce(lib):
    pts = [
        RefPoint("Iłża", 690000.0, 380000.0, alt_names="Ilza|Ilsa", type="town", feature="kościół farny",
                 sigma_m=10.0, source="PRNG"),
        RefPoint("Kielce", 640000.0, 355000.0, type="town", feature="katedra"),
        RefPoint("Wąchock", 660000.0, 372000.0, type="monastery", valid_from=1179),
        RefPoint("Łagów", 680000.0, 340000.0, type="town"),
        RefPoint("Łagów", 400000.0, 500000.0, type="village", note="inna miejscowość"),
    ]
    return lib.add_points(pts), pts


def test_create_and_reopen():
    path = os.path.join(TMP, "reopen.gpkg")
    with Library.create(path, "EPSG:2180") as lib:
        assert lib.schema_version == lib_mod.SCHEMA_VERSION
        assert lib.crs_authid == "EPSG:2180"
        uids, _ = _kielce(lib)
    with Library.open(path) as lib:
        assert len(lib.points()) == 5
        p = lib.get_point(uids[0])
        assert p.name == "Iłża" and p.alt_names == "Ilza|Ilsa" and p.sigma_m == 10.0
        assert (p.x, p.y) == (690000.0, 380000.0)
        assert lib.get_point(uids[1]).sigma_m is None
        assert lib.get_point(uids[2]).valid_from == 1179


def test_gpkg_is_valid_for_other_tools():
    path = os.path.join(TMP, "valid.gpkg")
    _new("valid").close()
    con = sqlite3.connect(path)
    contents = dict(con.execute("SELECT table_name, data_type FROM gpkg_contents").fetchall())
    con.close()
    assert contents["ref_points"] == "features"
    for t in ("point_sets", "set_members", "analyses", "gcp_links", "qma_meta"):
        assert contents[t] == "attributes", (t, contents)


def test_not_a_library_and_too_new():
    from osgeo import ogr
    other = os.path.join(TMP, "other.gpkg")
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(other)
    ds.CreateLayer("x", None, ogr.wkbPoint)
    ds = None
    try:
        Library.open(other)
        raise AssertionError("NotALibrary expected")
    except lib_mod.NotALibrary:
        pass
    path = os.path.join(TMP, "future.gpkg")
    with Library.create(path) as lib:
        lib._set_meta("schema_version", str(lib_mod.SCHEMA_VERSION + 1))
    try:
        Library.open(path)
        raise AssertionError("SchemaTooNew expected")
    except lib_mod.SchemaTooNew as e:
        assert e.found == lib_mod.SCHEMA_VERSION + 1


def test_normalize_and_search():
    assert lib_mod.normalize("  ŁAGÓW  Wielki ") == "lagow wielki"
    assert lib_mod.normalize("Wąchock") == "wachock"
    with _new("search") as lib:
        uids, _ = _kielce(lib)
        assert [p.name for p, _d in lib.search("ilza")] == ["Iłża"]
        assert [p.name for p, _d in lib.search("ILSA")] == ["Iłża"]       # nazwa dawna
        assert [p.name for p, _d in lib.search("wach")] == ["Wąchock"]
        hits = lib.search("lagow", near=(681000.0, 341000.0))
        assert [p.uid for p, _d in hits] == [uids[3], uids[4]]           # bliższy pierwszy
        assert hits[0][1] < 2000
        hits = lib.search("lagow", near=(681000.0, 341000.0), max_dist=50000)
        assert len(hits) == 1
        assert lib.search("zzz") == []
        assert len(lib.search("")) == 5


def test_update_and_delete_point():
    with _new("edit") as lib:
        uids, pts = _kielce(lib)
        p = lib.get_point(uids[1])
        p.x += 15.0
        p.note = "poprawione"
        lib.update_point(p)
        q = lib.get_point(uids[1])
        assert q.x == 640015.0 and q.note == "poprawione"
        try:
            lib.add_point(RefPoint("dup", 0, 0, uid=uids[0]))
            raise AssertionError("duplicate uid accepted")
        except lib_mod.LibraryError:
            pass
        lib.delete_point(uids[4])
        assert lib.get_point(uids[4]) is None and len(lib.points()) == 4


def test_sets():
    with _new("sets") as lib:
        uids, _ = _kielce(lib)
        sid = lib.add_set("Kielecczyzna-18", "pilot", uids[:4])
        other = lib.add_set("Inne", uids=[uids[4], uids[0]])
        assert lib.sets() == [(other, "Inne", "", 2), (sid, "Kielecczyzna-18", "pilot", 4)]
        assert set(lib.set_members(sid)) == set(uids[:4])
        lib.add_to_set(sid, [uids[0], uids[4]])                       # duplikat pomijany
        assert len(lib.set_members(sid)) == 5
        lib.remove_from_set(sid, [uids[4]])
        assert len(lib.points(sid)) == 4
        assert lib.point_sets_of(uids[0]) == sorted([sid, other])
        lib.update_set(sid, name="Kielecczyzna-18 (M1–M5)")
        assert lib.sets()[1][1] == "Kielecczyzna-18 (M1–M5)"
        lib.delete_set(other)
        assert [s[0] for s in lib.sets()] == [sid] and len(lib.points()) == 5
        try:
            lib.add_to_set(sid, ["no-such-uid"])
            raise AssertionError("unknown uid accepted")
        except lib_mod.LibraryError:
            pass
        assert len(lib.set_members(sid)) == 4                         # transakcja wycofana


def _links(uids):
    return [
        Link(100.0, -200.0, 690000.0, 380000.0, uid=uids[0], name="Iłża"),
        Link(400.0, -250.0, 640000.0, 355000.0, uid=uids[1], name="Kielce", status="uncertain",
             comment="sygnatura zatarta"),
        Link(uid=uids[2], name="Wąchock", status="absent", comment="pominięty przez kartografa"),
        Link(900.0, -700.0, 500100.0, 300200.0, name="kopiec", enabled=False),   # spoza biblioteki
    ]


def test_analysis_and_links():
    with _new("analyses") as lib:
        uids, _ = _kielce(lib)
        scan = os.path.join(TMP, "scan.jpg")
        with open(scan, "wb") as f:
            f.write(b"\xff\xd8 test scan")
        a = Analysis(title="Zannoni ark. 14", author="Rizzi-Zannoni", year_from=1772, scale=280000,
                     sheet="14", dpi=400.0, scan_path=scan, scan_sha256=lib_mod.file_sha256(scan),
                     ref_crs="EPSG:2180")
        aid = lib.save_analysis(a)
        assert aid is not None and a.created and a.modified
        lib.save_links(aid, _links(uids))
        got = lib.links(aid)
        assert [ln.name for ln in got] == ["Iłża", "Kielce", "Wąchock", "kopiec"]
        assert got[1].status == "uncertain" and got[1].comment == "sygnatura zatarta"
        assert got[2].x_old is None and got[2].status == "absent" and not got[2].computable
        assert got[3].uid is None and got[3].enabled is False and got[3].computable
        assert [ln.computable for ln in got] == [True, True, False, True]
        assert lib.usage(uids[1]) == [(aid, "uncertain")]

        b = lib.get_analysis(aid)
        assert b.title == "Zannoni ark. 14" and b.year_to is None and b.scale == 280000
        assert [x.analysis_id for x in lib.find_analyses(scan_sha256=a.scan_sha256)] == [aid]
        assert [x.analysis_id for x in lib.find_analyses(scan_path=scan)] == [aid]

        sid = lib.add_set("K", uids=uids[:4])
        assert [p.uid for p in lib.missing_from_set(aid, sid)] == [uids[3]]   # absent liczy się jako obecny

        lib.save_links(aid, _links(uids)[:2])                                # zastąpienie
        assert len(lib.links(aid)) == 2


def test_invalid_links_rejected():
    with _new("invalid") as lib:
        uids, _ = _kielce(lib)
        aid = lib.save_analysis(Analysis(title="x"))
        lib.save_links(aid, _links(uids))
        bad = [
            [Link(1, 1, 1, 1, uid=uids[0]), Link(2, 2, 2, 2, uid=uids[0])],   # uid dwukrotnie
            [Link(name="?", status="absent")],                                # absent bez biblioteki
            [Link(None, None, 1, 1, uid=uids[1])],                            # brak położenia na skanie
            [Link(1, 1, 1, 1, status="maybe")],                               # nieznany status
        ]
        for links in bad:
            try:
                lib.save_links(aid, links)
                raise AssertionError(f"accepted {links}")
            except lib_mod.LibraryError:
                pass
        assert len(lib.links(aid)) == 4                                       # bez zmian


def test_changed_refs_and_delete_in_use():
    with _new("changed") as lib:
        uids, _ = _kielce(lib)
        aid = lib.save_analysis(Analysis(title="TKKP Iłża", year_from=1965, ref_crs="EPSG:2180"))
        lib.save_links(aid, _links(uids))
        assert lib.changed_refs(aid) == []
        p = lib.get_point(uids[0])
        p.y += 120.0
        lib.update_point(p)
        ch = lib.changed_refs(aid)
        assert len(ch) == 1 and ch[0][0] == 0 and abs(ch[0][4] - 120.0) < 1e-9
        # przeliczenie do układu analizy (tu: przesunięcie o 120 m) – brak różnic
        assert lib.changed_refs(aid, to_analysis=lambda x, y: (x, y - 120.0) if y == 380120.0 else (x, y)) == []
        try:
            lib.delete_point(uids[1])
            raise AssertionError("PointInUse expected")
        except lib_mod.PointInUse as e:
            assert e.analyses == [aid]
        lib.delete_point(uids[1], force=True)
        got = lib.links(aid)
        assert got[1].uid is None and got[1].name == "Kielce" and got[1].ref_x == 640000.0
        lib.delete_analysis(aid)
        assert lib.analyses() == [] and lib.usage(uids[0]) == []


def test_geographic_crs_axis_order():
    path = os.path.join(TMP, "wgs.gpkg")
    with Library.create(path, "EPSG:4326") as lib:
        uid = lib.add_point(RefPoint("Iłża", 21.24, 51.16))
    with Library.open(path) as lib:
        p = lib.get_point(uid)
        assert (p.x, p.y) == (21.24, 51.16)
    from osgeo import ogr
    ds = ogr.Open(path)
    feat = ds.GetLayerByName("ref_points").GetNextFeature()
    g = feat.GetGeometryRef()
    assert abs(g.GetX() - 21.24) < 1e-12, "x musi być długością geograficzną (kolejność GIS)"


def test_match_point():
    pts = [lib_mod.RefPoint("Iłża", 0, 0, uid="a", alt_names="Ilsa"), lib_mod.RefPoint("Wierzbica", 300, 0, uid="b"),
           lib_mod.RefPoint("Iłża", 4000, 0, uid="c")]
    p, d, by_name = lib_mod.match_point(pts, "ilsa", 1000, 0, 5000, 500)
    assert p.uid == "a" and d == 1000 and by_name                      # nazwa dawna, najbliższy z tą nazwą
    p, d, by_name = lib_mod.match_point(pts, "", 350, 100, 5000, 500)
    assert p.uid == "b" and not by_name                                 # bez nazwy – najbliższy do 500 m
    p, d, by_name = lib_mod.match_point(pts, "Inna", 350, 0, 5000, 500)
    assert p.uid == "b" and not by_name                                 # nazwa bez dopasowania – najbliższy
    assert lib_mod.match_point(pts, "", 2000, 2000, 5000, 500) is None
    assert lib_mod.match_point(pts, "Iłża", 100, 0, 5000, 500, exclude={"a", "b"})[0].uid == "c"


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
