"""
Testy wykresów SVG (bez GUI QGIS).

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_charts.py
"""
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

ch = __import__(f"{PKG}.charts", fromlist=["*"])
NS = "{http://www.w3.org/2000/svg}"


def parse(svg):
    root = ET.fromstring(svg)
    assert root.tag == NS + "svg" and root.get("viewBox")
    return root


def texts(root):
    return [t.text for t in root.iter(NS + "text")]


def test_helpers():
    assert ch.metres_label(250) == "250 m" and ch.metres_label(1000) == "1 km" and ch.metres_label(2500) == "2,5 km"
    assert ch.metres_label(20000) == "20 km"
    assert ch._log_ticks(20, 2000) == [20, 50, 100, 200, 500, 1000, 2000]
    lo, hi = ch._log_domain([45, 31200])
    assert lo == 20 and hi == 50000, (lo, hi)
    assert ch.diverging(0) == ch.DIV_MID and ch.diverging(1) == ch.DIV_POS and ch.diverging(-1) == ch.DIV_NEG
    assert ch.diverging(float("nan")) == ch.DIV_MID
    assert ch._ink_on("#e34948") == "#ffffff" and ch._ink_on(ch.DIV_MID) == ch.INK
    assert ch._wrap("Zannoni Carte De La Pologne (1772)", 18) == ["Zannoni Carte De", "La Pologne (1772)"]
    assert ch._nice_length(730) == 500 and ch._nice_length(0) == 0


def test_accuracy_timeline():
    items = [{"label": "Polonia et Silesia", "year_from": 1585, "year_to": None, "rmse_m": 9800, "n": 12},
             {"label": "Mapa A (1770)", "year_from": 1770, "year_to": None, "rmse_m": 316, "n": 15},
             {"label": "Mapa B (1800)", "year_from": 1800, "year_to": None, "rmse_m": 318, "n": 15},
             {"label": "TKKP", "year_from": 1822, "year_to": 1843, "rmse_m": 420, "n": 20},
             {"label": "Mapa C (1830)", "year_from": 1830, "year_to": None, "rmse_m": 299, "n": 15},
             {"label": "Bez roku", "year_from": None, "year_to": None, "rmse_m": 100, "n": 5}]
    root = parse(ch.accuracy_timeline(items))
    t = texts(root)
    assert "Dokładność map w czasie" in t and "Polonia et Silesia" in t and "Bez roku" not in t
    assert len(root.findall(f".//{NS}circle")) == 5
    assert any("TKKP" in (c.findtext(f"{NS}title") or "") for c in root.iter(NS + "line"))   # pasek lat wydania
    assert "10 km" in t and "1800" in t
    empty = parse(ch.accuracy_timeline([]))
    assert "brak danych" in texts(empty)


def test_error_strip_and_matrix():
    cols = [{"label": "Zannoni Carte De La Pologne (1772)", "errors": [("Łagów", 8271.3, False), ("Kielce", 3304.4, True)]},
            {"label": "TKKP (1822–1843)", "errors": [("Łagów", 625.2, False)]}, {"label": "Pusta", "errors": []}]
    root = parse(ch.error_strip(cols, {"outlier": "outlier"}))
    t = texts(root)
    assert "Kielce" in t and "outlier" in t and "Pusta" not in t                # podpis tylko odstających
    labels = ["A (1770)", "B (1800)", "C (1830)"]
    pairs = {(0, 1): (0.99, 0.001, 15, True), (0, 2): (0.36, 0.06, 15, False), (1, 2): (float("nan"), float("nan"), 2, False)}
    root = parse(ch.similarity_matrix(labels, pairs))
    t = texts(root)
    assert "0,99 *" in t and "0,36" in t and "–" in t
    assert len([r for r in root.iter(NS + "rect") if r.get("rx") == "4"]) == 3


def test_vectors_sheets_index():
    panels = [{"label": "A", "sub": "RMSE 300 m", "points": [("P1", 0, 0, 100, 0, 100.0, False), ("P2", 1000, 500, 0, -50, 50.0, True)]},
              {"label": "B", "sub": "", "points": [("P1", 0, 0, 10, 10, 14.1, False)]}]
    root = parse(ch.vector_panels(panels, unit_m=1.0))
    t = texts(root)
    assert "A" in t and "B" in t and any(s and s.endswith("błędu") for s in t)
    assert len(root.findall(f".//{NS}clipPath")) == 2
    root = parse(ch.sheet_bars([{"label": "arkusz 1", "rmse_m": 35, "scale_den": 471968},
                                {"label": "arkusz 2", "rmse_m": 110, "scale_den": 521720}]))
    assert "110 m" in texts(root) and "Odchylenie skali od średniej" in texts(root)
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    root = parse(ch.sheet_index([{"label": "1", "footprint": sq}, {"label": "2", "footprint": [(x + 8, y) for x, y in sq]}],
                                [{"name": "Z1", "x": 9, "y": 5, "diff_m": 208.0}, {"name": "Z2", "x": 9, "y": 2, "diff_m": 50.0}]))
    assert "Z1 – 208 m" in texts(root)
    assert int(float(root.get("viewBox").split()[3])) < 600                      # wysokość z proporcji


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
