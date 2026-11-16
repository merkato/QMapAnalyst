"""
Testy rektyfikacji wydawnictwa wieloarkuszowego (GDAL, bez GUI QGIS).

Uruchomienie (OSGeo4W):  python-qgis.bat tests/test_publication_rectify.py
"""
import math
import os
import shutil
import sys
import tempfile

import numpy as np
from osgeo import gdal

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

bk = __import__(f"{PKG}.block", fromlist=["*"])
pr = __import__(f"{PKG}.publication_rectify", fromlist=["*"])
TMP = tempfile.mkdtemp(prefix="qma_pubrect_")
W, H = 400.0, 300.0


def world(r, c, px, py):
    gx, gy = c * W + px, -r * H + py
    x, y = 500000 + 10 * gx, 300000 + 10 * gy
    return x + 60 * math.sin(gx / 90.0), y + 50 * math.cos(gy / 70.0)          # lokalne zniekształcenie mapy


def make(margin=0.0, seed=1):
    """Arkusze 2×2; margin > 0 – ramki zachodzą na siebie (zakładki); skany o jednolitym kolorze arkusza."""
    rng = np.random.default_rng(seed)
    sheets, scans = [], {}
    for r in range(2):
        for c in range(2):
            i = len(sheets)
            src = np.column_stack([rng.uniform(10, W - 10, 14), rng.uniform(-H + 10, -10, 14)])
            dst = np.array([world(r, c, *p) for p in src])
            frame = [(-margin, margin), (W + margin, margin), (W + margin, -H - margin), (-margin, -H - margin)]
            if margin:                                   # skan obejmuje zakładkę – przesuwamy układ skanu
                src = src + margin
                dst = np.array([world(r, c, *(p - margin)) for p in src])
                frame = [(x + margin, y + margin) for x, y in frame]
            s = bk.BlockSheet(100 + i, f"{r}{c}", frame, src, dst, [""] * len(src), [""] * len(src), grid=(r, c))
            sheets.append(s)
            path = os.path.join(TMP, f"scan{i}_{margin:.0f}.tif")
            ds = gdal.GetDriverByName("GTiff").Create(path, int(W + 2 * margin + 1), int(H + 2 * margin + 1), 3, gdal.GDT_Byte)
            for b in range(3):
                ds.GetRasterBand(b + 1).Fill(40 * (i + 1) if b == 0 else 100)
            ds = None
            scans[s.key] = path
    return sheets, scans


def test_nodes_plan_shared_edges():
    sheets, _scans = make()
    plan = pr.build_plan(sheets, "tps")
    assert plan.stitch == "nodes" and plan.correction.rmse_after < plan.correction.rmse_before
    by = {sp.sheet.label: sp for sp in plan.sheets}
    right_of_00 = by["00"].ring[96:193]                  # krawędź TR→BR arkusza 00
    left_of_01 = by["01"].ring[288:] if len(by["01"].ring) > 288 else None
    a = {tuple(np.round(p, 6)) for p in right_of_00}
    b = {tuple(np.round(p, 6)) for p in np.vstack([by["01"].ring[288:], by["01"].ring[:1]])}
    assert left_of_01 is not None and a == b, len(a ^ b)  # ta sama krawędź w obu arkuszach
    assert not plan.overlaps()
    p = by["11"].func(np.array([[200.0, -150.0]]))[0]
    assert np.hypot(*(p - world(1, 1, 200.0, -150.0))) < 40


def test_warp_mosaic_no_gaps():
    sheets, scans = make()
    plan = pr.build_plan(sheets, "tps")
    out_dir = os.path.join(TMP, "nodes")
    paths = pr.warp_sheets(plan, scans, out_dir, "EPSG:2180", 10.0, "gtiff_lzw")
    assert len(paths) == 4 and all(os.path.exists(p) for p in paths)
    vrt = pr.write_mosaic(plan, os.path.join(out_dir, "mozaika.vrt"), 10.0)
    ds = gdal.Open(vrt)
    gt = ds.GetGeoTransform()
    alpha = ds.GetRasterBand(4).ReadAsArray()
    # wzdłuż wspólnych krawędzi (środek mozaiki) – brak szczelin
    by = {sp.sheet.label: sp for sp in plan.sheets}
    edge = by["00"].ring[100:190]
    cols = ((edge[:, 0] - gt[0]) / gt[1]).astype(int)
    rows = ((edge[:, 1] - gt[3]) / gt[5]).astype(int)
    assert (alpha[rows, cols] == 255).all() and (alpha[rows, cols + 1] == 255).all()
    assert "relativeToVRT=\"1\"" in open(vrt, encoding="utf-8").read()
    items = pr.footprints_items(plan, "tps")
    assert len(items) == 4 and len(items[0]["ring"]) == 96 and math.isfinite(items[0]["rmse_corr_m"])


def test_overlap_priority_and_blend():
    sheets, scans = make(margin=20.0)
    plan = pr.build_plan(sheets, "none", stitch=pr.STITCH_BLOCK, model="affine", seam_weight=0,
                         priorities={103: 0})
    assert plan.sheets[0].sheet.key == 103 and plan.overlaps()
    out_dir = os.path.join(TMP, "block")
    pr.warp_sheets(plan, scans, out_dir, "EPSG:2180", 10.0, "gtiff_lzw")
    vrt = pr.write_mosaic(plan, os.path.join(out_dir, "mozaika.vrt"), 10.0)
    # w strefie zakładki 11 (priorytet 0) z 01 – piksel z arkusza 11 (czerwony 160)
    top = plan.sheets[0]
    probe = top.func(np.array([[25.0, -60.0]]))[0]       # tuż przy lewej krawędzi ramki 11 (zakładka z 10)
    ds = gdal.Open(vrt)
    gt = ds.GetGeoTransform()
    c, r = int((probe[0] - gt[0]) / gt[1]), int((probe[1] - gt[3]) / gt[5])
    assert ds.GetRasterBand(1).ReadAsArray(c, r, 1, 1)[0, 0] == 160
    # blendowanie: maska narasta od krawędzi arkusza w strefie zakładki
    layers = pr.write_blend_vrts(plan, width=300.0)
    assert layers[0].endswith("_blend.vrt") and os.path.exists(layers[0].replace("_blend.vrt", "_mask.tif"))
    bds = gdal.Open(layers[0])
    a0 = bds.GetRasterBand(4).ReadAsArray()
    assert 0 < a0.max() == 255 and ((a0 > 0) & (a0 < 255)).any()
    out = pr.composite(layers, os.path.join(out_dir, "mozaika_blend.tif"), block=128, fmt="gtiff_lzw")
    cds = gdal.Open(out)
    cgt = cds.GetGeoTransform()
    c, r = int((probe[0] - cgt[0]) / cgt[1]), int((probe[1] - cgt[3]) / cgt[5])
    v = cds.GetRasterBand(1).ReadAsArray(c, r, 1, 1)[0, 0]
    assert 120 <= v < 160, v                             # mieszanina arkusza 11 (160) i 10 (120)
    assert cds.GetRasterBand(4).ReadAsArray(c, r, 1, 1)[0, 0] == 255


def test_alpha_function():
    sheets, _scans = make(margin=20.0)
    plan = pr.build_plan(sheets, "none", stitch=pr.STITCH_BLOCK, model="affine", seam_weight=0)
    x, y = plan.sheets[0].ring.mean(axis=0)
    assert pr.blend_alpha(plan, 0, np.array([x]), np.array([y]), 100.0)[0] == 1.0
    far = np.array([x + 1e7]), np.array([y])
    assert pr.blend_alpha(plan, 0, *far, 100.0)[0] == 0.0


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
