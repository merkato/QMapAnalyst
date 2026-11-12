"""
Testy silnika obliczeniowego (bez GUI QGIS).

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_engine.py
lub:                      python -m pytest tests
"""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

math_engine = __import__(f"{PKG}.math_engine", fromlist=["*"])
projections = __import__(f"{PKG}.projections", fromlist=["*"])
gcp_io = __import__(f"{PKG}.gcp_io", fromlist=["*"])
theme = __import__(f"{PKG}.theme", fromlist=["*"])


def _synthetic(n=40, seed=3):
    rng = np.random.default_rng(seed)
    src = rng.uniform([0, -8000], [10000, 0], size=(n, 2))
    ang = math.radians(3.0)
    s = 12.5
    R = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]]) * s
    dst = src @ R.T + np.array([500000.0, 300000.0])
    return src, dst, rng


def test_helmert_exact():
    src, dst, _ = _synthetic()
    fr = math_engine.fit_transform("helmert", src, dst)
    assert fr.stats["rmse"] < 1e-6
    assert abs(fr.derived["scale"] - 12.5) < 1e-9
    assert abs(fr.derived["rotation_deg"] - 3.0) < 1e-9
    assert np.allclose(fr.predict(src), dst)


def test_statistics_and_outlier():
    src, dst, rng = _synthetic()
    dst = dst + rng.normal(0, 5.0, dst.shape)
    dst[7] += [400.0, -300.0]
    fr = math_engine.fit_transform("affine", src, dst)
    assert fr.redundancy == 2 * len(src) - 6
    assert np.nanargmax(fr.std_residuals) == 7
    assert fr.std_residuals[7] > math_engine.OUTLIER_THRESHOLD
    assert np.nanargmax(fr.loo_errors) == 7
    # LOO z macierzy kapeluszowej == jawne ponowne wyrównanie bez punktu
    i = 3
    mask = np.arange(len(src)) != i
    fr_i = math_engine.fit_transform("affine", src[mask], dst[mask])
    loo = np.linalg.norm(dst[i] - fr_i.predict(src[i:i + 1])[0])
    assert abs(loo - fr.loo_errors[i]) < 1e-6
    assert np.isfinite(fr.derived["scale_std"]) and fr.derived["scale_std"] > 0


def test_poly2_and_comparison():
    src, dst, rng = _synthetic()
    dst = dst + 1e-6 * (src[:, [0]] ** 2) * [1.0, 0.5]
    cmp = math_engine.compare_models(src, dst + rng.normal(0, 0.5, dst.shape))
    best = min(cmp, key=lambda c: c["aicc"])
    assert best["model"] == "poly2"


def test_warp_and_distortion():
    src, dst, rng = _synthetic()
    dst = dst + rng.normal(0, 3.0, dst.shape)
    w = math_engine.Warp.build("helmert", src, dst)
    assert w.ok
    assert np.allclose(w.predict(src), dst, atol=1e-4)
    g = math_engine.local_distortion_grid(w, 30, 30)
    s = math_engine.summarize_distortion(g)
    assert abs(s["area_scale"]["mean"] - 12.5) < 0.5


def test_marching_squares():
    x = np.linspace(-1, 1, 41)
    y = np.linspace(-1, 1, 41)
    X, Y = np.meshgrid(x, y)
    segs = math_engine.marching_squares(x, y, np.hypot(X, Y), 0.5)
    r = np.hypot(segs[..., 0], segs[..., 1])
    assert len(segs) > 20 and np.allclose(r, 0.5, atol=0.01)
    lv = math_engine.nice_levels(np.array([12345, 13999]), 8)
    assert len(lv) >= 4


def test_moran():
    rng = np.random.default_rng(0)
    pts = rng.uniform(0, 100, (60, 2))
    trend = pts[:, 0] * 0.1
    I, E, p = math_engine.morans_i(pts, trend)
    assert I > E and p < 0.01
    I2, _, p2 = math_engine.morans_i(pts, rng.normal(size=60))
    assert p2 > 0.01


def _projected_map(key, params, n=40, noise_px=1.5, seed=5, lon=(14.5, 24.0), lat=(49.0, 54.8)):
    rng = np.random.default_rng(seed)
    ll = np.column_stack([rng.uniform(*lon, n), rng.uniform(*lat, n)])
    proj = projections.PROJECTIONS_BY_KEY[key]
    p = {k: math.radians(v) for k, v in params.items()}
    x, y = proj.func(np.radians(ll[:, 0]), np.radians(ll[:, 1]), p)
    P = np.column_stack([x, y])
    ang = math.radians(-1.7)
    R = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]]) * 60000.0
    px = P @ R.T + [4000.0, -3000.0] + rng.normal(0, noise_px, P.shape)
    return ll, px


def test_projection_estimation_bonne():
    ll, px = _projected_map("bonne", {"lon0": 19.0, "lat1": 52.0})
    est = projections.estimate_projections(ll, px)
    best = est.best
    assert best.key == "bonne", [c.key for c in est.candidates[:4]]
    # zgodność z wartością prawdziwą w granicach 3 sigma oszacowania
    assert abs(best.params_deg["lon0"] - 19.0) < 3 * best.params_std_deg["lon0"] + 0.1
    assert abs(best.params_deg["lat1"] - 52.0) < 3 * best.params_std_deg["lat1"] + 0.1
    assert best.sigma0_px < 3.0
    # skala główna: 60000 px na promień Ziemi przy 300 DPI
    M = best.scale_denominator(300)
    assert abs(M - projections.EARTH_RADIUS_M / (60000 * 0.0254 / 300)) / M < 0.01
    assert est.conclusive


def test_projection_estimation_cassini_and_mercator():
    ll, px = _projected_map("cassini", {"lon0": 17.0}, seed=8)
    est = projections.estimate_projections(ll, px)
    assert est.best.key in ("cassini", "tmerc"), [c.key for c in est.candidates[:4]]
    ll, px = _projected_map("mercator", {"lon0": 0.0}, seed=9, lon=(0, 25), lat=(35, 60))
    est = projections.estimate_projections(ll, px)
    assert est.best.key == "mercator", [(c.key, round(c.delta_aicc, 1)) for c in est.candidates[:4]]
    lines = projections.graticule_lines(est.best, est.lon_range, est.lat_range, 5.0)
    assert lines and all(np.all(np.isfinite(l[2])) for l in lines)


def test_single_sheet_inconclusive_and_local_scale():
    """Sytuacja jak arkusz TKKP Iłża: 13 punktów na ~0.8°, duże błędy rysunku."""
    ll, px = _projected_map("bonne", {"lon0": 21.04, "lat1": 52.0}, n=13, noise_px=20.0, seed=11,
                            lon=(20.59, 21.39), lat=(50.87, 51.28))
    est = projections.estimate_projections(ll, px, mode="affine")
    assert not est.conclusive
    assert len(est.equivalent()) >= 2
    # skala w środku obszaru musi być zgodna dla wszystkich sensownych kandydatów
    true_local = projections.EARTH_RADIUS_M / (60000 * 0.0254 / 300)   # Bonne jest równopolowe: k = 1
    scales = [c.scale_denominator(300) for c in est.candidates
              if c.success and not c.is_reference and c.sigma0_px < 1.2 * est.best.sigma0_px]
    principal = [c.principal_scale_denominator(300) for c in est.candidates
                 if c.success and not c.is_reference and c.sigma0_px < 1.2 * est.best.sigma0_px]
    # kandydaci zgodni co do skali w środku obszaru (skale główne rozjeżdżają się nawet o kilkadziesiąt %)
    assert max(scales) / min(scales) - 1 < 0.02, scales
    assert max(principal) / min(principal) - 1 > 0.2
    assert abs(np.mean(scales) / true_local - 1) < 0.10


def test_mercator_local_vs_principal_scale():
    ll, px = _projected_map("mercator", {"lon0": 0.0}, seed=9, lon=(0, 25), lat=(35, 60))
    est = projections.estimate_projections(ll, px)
    m = next(c for c in est.candidates if c.key == "mercator")
    k = m.local_scale_factor()
    lat_c = float(np.mean(ll[:, 1]))
    assert abs(k - 1 / math.cos(math.radians(lat_c))) < 1e-3
    assert abs(m.principal_scale_denominator(300) / m.scale_denominator(300) - k) < 1e-6



def test_parse_angle():
    pa = projections.parse_angle
    assert abs(pa("38°42′30″") - (38 + 42 / 60 + 30 / 3600)) < 1e-12
    assert abs(pa("21 02 30 E") - (21 + 2 / 60 + 30 / 3600)) < 1e-12
    assert abs(pa("17°40' W") + (17 + 40 / 60)) < 1e-12
    assert abs(pa("-17.6667") + 17.6667) < 1e-12
    assert abs(pa("52,5") - 52.5) < 1e-12
    assert abs(pa('46°57\'08.66"N') - (46 + 57 / 60 + 8.66 / 3600)) < 1e-12
    for bad in ("", "abc", "21°75′", "1 2 3 4", "21x"):
        try:
            pa(bad)
        except ValueError:
            continue
        raise AssertionError(f"przyjęto niepoprawny kąt: {bad!r}")
    # południk warszawski: Ferro -> Greenwich
    lon = pa("38°42′30″") + projections.meridian_offset("Ferro")
    assert abs(lon - (21 + 2.5 / 60)) < 1e-9


def test_hypothesis_true_vs_wrong():
    ll, px = _projected_map("bonne", {"lon0": 19.0, "lat1": 52.0}, noise_px=1.5)
    est = projections.estimate_projections(ll, px)
    good = projections.add_hypothesis(est, "bonne", {"lon0": 19.0, "lat1": 52.0})
    assert good.is_hypothesis and good.n_free == 0
    assert good.delta_aicc < 2, good.delta_aicc
    bad = projections.add_hypothesis(est, "bonne", {"lon0": 5.0, "lat1": 30.0})
    assert bad.delta_aicc > 10, bad.delta_aicc
    # zastąpienie hipotezy tego samego odwzorowania
    assert sum(1 for c in est.candidates if c.is_hypothesis and c.key == "bonne") == 1
    assert est.free_fit("bonne") is not None and not est.free_fit("bonne").is_hypothesis
    lines = projections.graticule_lines(good, est.lon_range, est.lat_range, 1.0)
    assert lines


def test_hypothesis_single_sheet_tkkp():
    """Arkusz TKKP: hipoteza z parametrami z literatury ma być zgodna z danymi."""
    lon0 = projections.parse_angle("38°42′30″") + projections.meridian_offset("Ferro")
    ll, px = _projected_map("bonne", {"lon0": lon0, "lat1": 52.0}, n=13, noise_px=20.0, seed=11,
                            lon=(20.59, 21.39), lat=(50.87, 51.28))
    est = projections.estimate_projections(ll, px, mode="affine")
    h = projections.add_hypothesis(est, "bonne", {"lon0": lon0, "lat1": 52.0})
    assert h.delta_aicc < 2, h.delta_aicc
    assert abs(h.scale_denominator(300) / est.best.scale_denominator(300) - 1) < 0.02


# ---------------------------------------------------------------------------
#  Pliki GCP (format Georeferencera QGIS)
# ---------------------------------------------------------------------------

WKT = ('PROJCRS["ETRF2000-PL / CS92",BASEGEOGCRS["ETRF2000-PL",DATUM["ETRF2000-PL",'
       'ELLIPSOID["GRS 1980",6378137,298.257222101,LENGTHUNIT["metre",1]]]],CONVERSION["Poland CS92",'
       'METHOD["Transverse Mercator",ID["EPSG",9807]]],CS[Cartesian,2],ID["EPSG",2180]]')


def _qgis_load(text):
    """Wierna kopia algorytmu QgsGCPList::loadGcps (QGIS master)."""
    lines = text.split("\n")
    i = 0
    line = lines[i]
    crs = None
    if "#CRS: " in line:
        crs = line.replace("#CRS: ", "")
        i += 1
    i += 1  # nagłówek (lub pierwsza linia bez CRS) jest pomijany
    out = []
    for line in lines[i:]:
        if line == "" and i:
            continue
        ls = line.split(",") if "," in line else line.split("\t")
        assert len(ls) >= 4, line
        out.append((float(ls[2]), float(ls[3]), float(ls[0]), float(ls[1]), int(ls[4]) != 0 if len(ls) >= 5 else True))
    return crs, out


def test_points_qgis_written_file():
    text = (f"#CRS: {WKT}\n"
            "mapX,mapY,sourceX,sourceY,enable,dX,dY,residual\n"
            "637012.123456789,486514.5,1534.25,-1022.75,1,0.5,-0.25,0.559\n"
            "641000,480000.25,2210.5,-2800.125,0,0,0,0\n")
    crs, rows, skipped = gcp_io.parse_gcp_text(text, "mapa.tif.points")
    assert crs == WKT and skipped == 0
    assert rows[0] == (1534.25, -1022.75, 637012.123456789, 486514.5, True, "")
    assert rows[1][4] is False


def test_points_legacy_and_headerless():
    legacy = "mapX\tmapY\tpixelX\tpixelY\tenable\n500000\t300000\t10.5\t-20.5\t1\n"
    _, rows, _ = gcp_io.parse_gcp_text(legacy, "a.points")
    assert rows == [(10.5, -20.5, 500000.0, 300000.0, True, "")]
    noheader = "500000,300000,10.5,-20.5,1\n500100,300100,11.5,-21.5,0\n"
    _, rows, _ = gcp_io.parse_gcp_text(noheader, "b.points")
    assert rows[0][:4] == (10.5, -20.5, 500000.0, 300000.0) and rows[1][4] is False
    csv_pl = "x_old;y_old;x_ref;y_ref;enable\n10,5;-20,5;500000,25;300000;1\nzly;wiersz;;;\n"
    _, rows, skipped = gcp_io.parse_gcp_text(csv_pl, "c.csv")
    assert rows == [(10.5, -20.5, 500000.25, 300000.0, True, "")] and skipped == 1


def test_points_roundtrip_readable_by_qgis():
    gcps = [(1534.25, -1022.75, 637012.123456789, 486514.5, True),
            (2210.5, -2800.125, 641000.0, 480000.25, False)]
    text = gcp_io.format_points(gcps, WKT)
    crs, rows = _qgis_load(text)
    assert crs == WKT and rows == gcps
    crs2, rows2, _ = gcp_io.parse_gcp_text(text, "x.points")
    assert crs2 == WKT and [r[:5] for r in rows2] == gcps
    _, rows3, _ = gcp_io.parse_gcp_text(gcp_io.format_csv(gcps), "x.csv")
    assert [r[:5] for r in rows3] == gcps


NAMED = [(1534.25, -1022.75, 637012.1, 486514.5, True, "Kościół, wieża"),
         (2210.5, -2800.125, 641000.0, 480000.25, False, ""),
         (900.0, -400.0, 630000.0, 490000.0, True, 'Młyn "Dolny"')]


def test_points_with_names_stay_qgis_compatible():
    text = gcp_io.format_points(NAMED, WKT)
    assert "Kości" not in text and "Młyn" not in text
    _, rows = _qgis_load(text)
    assert rows == [g[:5] for g in NAMED]


def test_names_sidecar_roundtrip_and_matching():
    names = gcp_io.parse_names(gcp_io.format_names(NAMED))
    assert [n[1] for n in names] == ["Kościół, wieża", "", 'Młyn "Dolny"']
    plain = [g[:5] for g in NAMED]
    assert gcp_io.assign_names(plain, names) == NAMED
    # Georeferencer: kolejność zmieniona, jeden punkt usunięty, jeden dodany
    edited = [plain[2], (5.0, -5.0, 1.0, 1.0, True), plain[0]]
    got = gcp_io.assign_names(edited, names)
    assert [g[5] for g in got] == ['Młyn "Dolny"', "", "Kościół, wieża"]
    # współrzędne poprawione, liczba punktów ta sama – dopasowanie po numerze
    moved = [(g[0] + 3.0,) + g[1:5] for g in plain]
    assert [g[5] for g in gcp_io.assign_names(moved, names)] == [g[5] for g in NAMED]
    # plik nazw da się wczytać jako lista GCP
    _, rows, skipped = gcp_io.parse_gcp_text(gcp_io.format_names(NAMED), "a.points.names.csv")
    assert skipped == 0 and [r[:4] for r in rows] == [g[:4] for g in NAMED] and rows[0][5] == "Kościół, wieża"


def test_csv_with_names():
    text = gcp_io.format_csv(NAMED)
    assert text.splitlines()[0] == "x_old,y_old,x_ref,y_ref,enable,name"
    _, rows, skipped = gcp_io.parse_gcp_text(text, "x.csv")
    assert skipped == 0 and rows == NAMED
    assert "name" not in gcp_io.format_csv([g[:5] for g in NAMED])


def test_theme_contrast_on_qgis_backgrounds():
    # tła QTextBrowser: domyślne, Night Mapping, Blend of Gray, ciemny systemowy
    for bg, text in (("#ffffff", "#000000"), ("#535353", "#aaaaaa"), ("#b2b2b2", "#0e0e0e"), ("#1e1e1e", "#dddddd")):
        pal = theme.report_palette(bg, text)
        assert theme.contrast(pal["text"], bg) >= theme.TEXT_MIN - 1e-6, (bg, pal["text"])
        assert theme.contrast(pal["text"], pal["head"]) >= theme.SEMANTIC_MIN, (bg, pal["head"])
        for k in ("muted", "accent", "good", "warn", "bad"):
            assert theme.contrast(pal[k], bg) >= theme.SEMANTIC_MIN - 1e-6, (bg, k, pal[k])
    # kolor czytelny na obu tłach naprzemiennych wierszy tabeli
    c = theme.foreground("bad", "#202020", "#323232")
    assert min(theme.contrast(c, "#202020"), theme.contrast(c, "#323232")) >= theme.SEMANTIC_MIN


if __name__ == "__main__":
    import time
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            t = time.perf_counter()
            try:
                fn()
                print(f"OK    {name}  ({time.perf_counter() - t:.2f}s)")
            except Exception as e:  # noqa
                failed += 1
                import traceback
                print(f"FAIL  {name}: {e!r}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
