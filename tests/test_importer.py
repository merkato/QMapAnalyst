"""
Testy importu punktów (bez GUI QGIS).

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_importer.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

im = __import__(f"{PKG}.importer", fromlist=["*"])
lb = __import__(f"{PKG}.library", fromlist=["*"])


def test_guess_fields_prng_osm():
    prng = ["ID_PRNG", "NAZWA_GLOWNA", "RODZAJ_OBIEKTU", "NAZWA_HISTORYCZNA", "X", "Y"]
    assert im.guess_field(prng, "name") == "NAZWA_GLOWNA"
    assert im.guess_field(prng, "type") == "RODZAJ_OBIEKTU"
    assert im.guess_field(prng, "alt_names") == "NAZWA_HISTORYCZNA"
    assert im.guess_field(prng, "x") == "X" and im.guess_field(prng, "y") == "Y"
    osm = ["osm_id", "name", "place", "old_name", "lat", "lon"]
    assert im.guess_field(osm, "type") == "place" and im.guess_field(osm, "alt_names") == "old_name"
    assert im.guess_field(osm, "x") == "lon" and im.guess_field(osm, "y") == "lat"
    assert im.guess_field(["a", "b"], "name") is None


def test_map_type_and_numbers():
    assert im.map_type("Miasto") == "town" and im.map_type("wieś") == "village" and im.map_type("WIES") == "village"
    assert im.map_type("hamlet") == "village" and im.map_type("Kościół") == "church" and im.map_type("Schloss") == "castle"
    assert im.map_type("church") == "church"
    assert im.map_type("osiedle", "other") == "other" and im.map_type(None, "x") == "x"
    assert im.parse_number("12,5") == 12.5 and im.parse_number("1 234,5") == 1234.5
    assert im.parse_number("abc") is None and im.parse_number("nan") is None and im.parse_number(3) == 3.0


def test_read_csv_semicolon_and_comma_decimal():
    text = "nazwa;x;y;typ;nazwy_dawne\nIłża;21,24;51,16;miasto;Ilza, Ilsa\nKielce;20,63;50,87;miasto;\n;1;2;wieś;\n"
    fields, rows = im.read_csv(text)
    assert fields == ["nazwa", "x", "y", "typ", "nazwy_dawne"], fields
    for r in rows:
        r[im.X_KEY], r[im.Y_KEY] = r["x"], r["y"]
    mapping = {"name": "nazwa", "type": "typ", "alt_names": "nazwy_dawne"}
    pts, skipped = im.build_points(rows, mapping, default_source="test.csv")
    assert skipped == 1 and len(pts) == 2
    assert pts[0].name == "Iłża" and pts[0].alt_names == "Ilza|Ilsa" and pts[0].type == "town"
    assert (pts[0].x, pts[0].y) == (21.24, 51.16) and pts[0].source == "test.csv"


def test_build_points_fields():
    rec = {"n": "Wąchock", "a": "Wąchock|Wanchock", "t": "opactwo", "s": "15", "f": "1179", "u": "", im.X_KEY: 1.0, im.Y_KEY: 2.0}
    pts, _ = im.build_points([rec], {"name": "n", "alt_names": "a", "type": "t", "sigma_m": "s", "valid_from": "f",
                                     "valid_to": "u"}, default_type="other")
    p = pts[0]
    assert p.alt_names == "Wanchock" and p.type == "monastery" and p.sigma_m == 15.0
    assert p.valid_from == 1179 and p.valid_to is None
    pts, _ = im.build_points([dict(rec, t="osiedle")], {"name": "n", "type": "t"}, default_type="other")
    assert pts[0].type == "other"


def test_duplicates():
    existing = [lb.RefPoint("Iłża", 0.0, 0.0, uid="a", alt_names="Ilsa"),
                lb.RefPoint("Kielce", 1000.0, 0.0, uid="b")]
    new = [
        lb.RefPoint("Ilza", 10.0, 0.0),            # ta sama nazwa (bez znaków), blisko – duplikat
        lb.RefPoint("Ilsa", 30.0, 30.0),           # nazwa dawna – duplikat
        lb.RefPoint("Iłża", 500.0, 0.0),           # ta sama nazwa, daleko – nowy
        lb.RefPoint("Kościół", 1020.0, 0.0),       # blisko Kielc, inna nazwa – nowy, raportowany
        lb.RefPoint("Nowa Wieś", 5000.0, 0.0),
        lb.RefPoint("Nowa Wies", 5005.0, 0.0),     # duplikat w obrębie importu
    ]
    to_add, dups, near = im.find_duplicates(existing, new, 50.0)
    assert [p.name for p in to_add] == ["Iłża", "Kościół", "Nowa Wieś"], [p.name for p in to_add]
    assert [(p.name, q.uid) for p, q in dups[:2]] == [("Ilza", "a"), ("Ilsa", "a")]
    assert dups[2][0].name == "Nowa Wies" and dups[2][1] is to_add[2]
    assert [(p.name, q.uid) for p, q in near] == [("Kościół", "b")]


def test_merge_alt_names():
    q = lb.RefPoint("Iłża", 0, 0, alt_names="Ilsa")
    assert im.merge_alt_names(q, lb.RefPoint("Iłża", 0, 0, alt_names="Ilza|Ilsa|Илжа"))
    assert q.alt_names == "Ilsa|Илжа"     # „Ilza” = „Iłża” po normalizacji
    assert not im.merge_alt_names(q, lb.RefPoint("ilza", 0, 0))


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
