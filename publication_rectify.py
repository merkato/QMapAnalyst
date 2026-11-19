"""
Rektyfikacja wydawnictwa wieloarkuszowego (plan 0.3.2, etap 4) – bez Qt.

1. Wyrównanie blokowe (block.py) daje transformację każdego arkusza.
2. Tryb „węzły” (arkusze stykające się w siatce): wspólne narożniki ramek uśrednione do węzłów siatki,
   arkusz rozpięty biliniowo między swoimi 4 węzłami – krawędzie wspólne identyczne, styki bez szczelin.
   Tryb „blok” (arkusze zachodzące na siebie / bez siatki): transformacje z wyrównania blokowego.
3. Pole korekcji lokalnej (rectify.py) z reszt wszystkich arkuszy – w terenie, więc wspólne krawędzie
   przesuwają się w obu arkuszach tak samo.
4. Każdy arkusz: gęsta siatka GCP i linia cięcia z obwodu ramki → gdalwarp -tps (rectify_raster.py),
   wspólna siatka pikseli. Mozaika VRT w kolejności priorytetu; przy zakładkach – przycięcie do arkusza
   o wyższym priorytecie albo blendowanie (maska alfa z odległości od krawędzi, tylko w strefie zakładki).
"""

import math
import os
import re
from dataclasses import dataclass, field

import numpy as np

from . import block as bk
from . import rectify as rc

STITCH_NODES, STITCH_BLOCK = "nodes", "block"
HARD_SEAM_WEIGHT = 1e4


@dataclass
class SheetPlan:
    sheet: bk.BlockSheet
    base: object                                   # skan → teren (przed korekcją)
    func: object = None                            # skan → teren (po korekcji)
    ring: np.ndarray = None                        # linia cięcia w terenie
    rmse_m: float = float("nan")                   # reszty arkusza przed korekcją
    rmse_corr_m: float = float("nan")              # po korekcji
    output: str = ""


@dataclass
class Plan:
    stitch: str
    model: str
    correction: rc.Correction
    sheets: list = field(default_factory=list)     # SheetPlan w kolejności priorytetu (pierwszy – najważniejszy)
    block: bk.BlockResult = None
    unit_m: float = 1.0

    def overlaps(self):
        """Pary (i, j) arkuszy, których linie cięcia się nakładają (powierzchnia > 0)."""
        out = []
        for i in range(len(self.sheets)):
            for j in range(i + 1, len(self.sheets)):
                if _rings_overlap(self.sheets[i].ring, self.sheets[j].ring):
                    out.append((i, j))
        return out


def _rings_overlap(a, b):
    from osgeo import ogr

    def poly(r):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in list(r) + [r[0]]:
            ring.AddPoint_2D(float(x), float(y))
        p = ogr.Geometry(ogr.wkbPolygon)
        p.AddGeometry(ring)
        return p
    pa, pb = poly(a), poly(b)
    if not pa.Intersects(pb):
        return False
    inter = pa.Intersection(pb)
    return inter is not None and inter.GetArea() > 1e-6 * min(pa.GetArea(), pb.GetArea())


def node_quads(result):
    """Czworokąty węzłów dla arkuszy: narożnik wspólny = średnia położeń z arkuszy, które go dzielą."""
    sheets = result.sheets
    quads = [result.transform(i, s.frame).copy() for i, s in enumerate(sheets)]
    for owners in bk.corner_nodes(sheets).values():
        mean = np.mean([quads[i][k] for i, k in owners], axis=0)
        for i, k in owners:
            quads[i][k] = mean
    return quads


def _bilinear_func(frame, quad):
    def f(pts):
        u, v = rc.inv_bilinear(frame, pts)
        return rc.bilinear(quad, u, v)
    return f


def _block_func(result, i):
    return lambda pts: result.transform(i, pts)


def priority_order(sheets, priorities=None):
    """Indeksy arkuszy wg priorytetu (mniejszy = ważniejszy), potem wg siatki i etykiety."""
    priorities = priorities or {}

    def key(i):
        s = sheets[i]
        p = priorities.get(s.key)
        return (p if p is not None else 10 ** 6, s.grid or (10 ** 6, 10 ** 6), s.label)
    return sorted(range(len(sheets)), key=key)


def build_plan(sheets, method="tps", lam=None, stitch=STITCH_NODES, model="bilinear", seam_weight=None,
               priorities=None, unit_m=1.0):
    """Plan rektyfikacji wydawnictwa. stitch=nodes wymaga siatki (s.grid); seam_weight – tylko w trybie blok."""
    if stitch == STITCH_NODES:
        result = bk.adjust(sheets, "bilinear", HARD_SEAM_WEIGHT, unit_m=unit_m, check_sheets=False)
        quads = node_quads(result)
        bases = [_bilinear_func(s.frame, q) for s, q in zip(sheets, quads)]
    else:
        result = bk.adjust(sheets, model, seam_weight, unit_m=unit_m, check_sheets=False)
        bases = [_block_func(result, i) for i in range(len(sheets))]
    P, E, owner = [], [], []
    for i, (s, f) in enumerate(zip(sheets, bases)):
        if len(s.src):
            p = f(s.src)
            P.append(p)
            E.append(p - s.dst)
            owner += [i] * len(s.src)
    P, E, owner = np.vstack(P), np.vstack(E), np.array(owner)
    corr = rc.build_correction(P, E, method, lam=lam)
    plan = Plan(stitch, "bilinear" if stitch == STITCH_NODES else model, corr, block=result, unit_m=unit_m)
    for i in priority_order(sheets, priorities):
        s, base = sheets[i], bases[i]
        sp = SheetPlan(s, base, func=_compose(base, corr))
        sp.ring = sp.func(rc.frame_ring(s.frame, 96))
        m = owner == i
        if m.any():
            sp.rmse_m = float(np.sqrt((E[m] ** 2).sum(1).mean())) * unit_m
            sp.rmse_corr_m = float(np.sqrt((corr.after[m] ** 2).sum(1).mean())) * unit_m
        plan.sheets.append(sp)
    return plan


def _compose(base, corr):
    return lambda pts: corr.apply(base(np.atleast_2d(np.asarray(pts, float))))


def safe_name(label):
    return re.sub(r"[^\w.-]+", "_", str(label)).strip("_") or "arkusz"


# ---------------------------------------------------------------------------
#  Rastry
# ---------------------------------------------------------------------------
def warp_sheets(plan, scans, out_dir, crs_text, res, fmt="cog_webp", progress=None):
    """Przetworzenie arkuszy. scans – {analysis_id: ścieżka skanu}. progress(i, n, ułamek) → False przerywa.
    Zwraca listę ścieżek (w kolejności planu); arkusze bez skanu – pominięte."""
    from . import rectify_raster as rr
    os.makedirs(out_dir, exist_ok=True)
    out = []
    n = len(plan.sheets)
    for k, sp in enumerate(plan.sheets):
        scan = scans.get(sp.sheet.key)
        if not scan or not os.path.exists(scan):
            continue
        gcps = [(px, -ln, X, Y) for px, ln, X, Y in rc.gcp_grid(sp.func, sp.sheet.frame, k=24)]
        path = os.path.join(out_dir, f"{safe_name(sp.sheet.label)}.tif")
        cb = (lambda f, k=k: progress(k, n, f)) if progress else None
        rr.warp_scan(scan, gcps, sp.ring.tolist(), path, crs_text, res, fmt, cb)
        sp.output = path
        out.append(path)
    return out


def _point_in_poly(x, y, ring):
    """Wektorowo: punkty (x, y) wewnątrz wielokąta ring (n×2) – reguła parzystości."""
    inside = np.zeros(x.shape, bool)
    xs, ys = ring[:, 0], ring[:, 1]
    j = len(ring) - 1
    for i in range(len(ring)):
        cond = (ys[i] > y) != (ys[j] > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (xs[j] - xs[i]) * (y - ys[i]) / (ys[j] - ys[i]) + xs[i]
        inside ^= cond & (x < xint)
        j = i
    return inside


def _dist_to_ring(x, y, ring):
    d = np.full(x.shape, np.inf)
    a = ring
    b = np.roll(ring, -1, axis=0)
    for (ax, ay), (bx, by) in zip(a, b):
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy or 1e-30
        t = np.clip(((x - ax) * dx + (y - ay) * dy) / L, 0, 1)
        d = np.minimum(d, np.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return d


def blend_alpha(plan, k, x, y, width):
    """Maska krycia arkusza k (0…1) w punktach terenu: 1 w środku; w strefie zakładki z arkuszem o NIŻSZYM
    priorytecie (rysowanym pod spodem) narasta od krawędzi arkusza na szerokości width; poza arkuszem 0."""
    ring = plan.sheets[k].ring
    inside = _point_in_poly(x, y, ring)
    alpha = inside.astype(float)
    if width <= 0:
        return alpha
    below = np.zeros(x.shape, bool)
    for j in range(k + 1, len(plan.sheets)):
        below |= _point_in_poly(x, y, plan.sheets[j].ring)
    zone = inside & below
    if zone.any():
        alpha[zone] = np.clip(_dist_to_ring(x[zone], y[zone], ring) / width, 0, 1)
    return alpha


def write_blend_vrts(plan, width, factor=8):
    """Dla arkuszy z zakładką: maska krycia w niskiej rozdzielczości (GeoTIFF) i VRT arkusza, którego kanał
    alfa = alfa arkusza × maska (funkcja pikseli GDAL „mul”, maska skalowana dwuliniowo). Zwraca ścieżki
    rastrów do warstw (VRT albo oryginalny arkusz)."""
    from osgeo import gdal
    out = []
    pairs = plan.overlaps()
    upper = {i for i, _j in pairs}
    for k, sp in enumerate(plan.sheets):
        if not sp.output:
            continue
        if width <= 0 or k not in upper:
            out.append(sp.output)
            continue
        ds = gdal.Open(sp.output)
        gt, W, H = ds.GetGeoTransform(), ds.RasterXSize, ds.RasterYSize
        w, h = max(2, W // factor), max(2, H // factor)
        xs = gt[0] + (np.arange(w) + 0.5) * (W / w) * gt[1]
        ys = gt[3] + (np.arange(h) + 0.5) * (H / h) * gt[5]
        X, Y = np.meshgrid(xs, ys)
        a = (blend_alpha(plan, k, X.ravel(), Y.ravel(), width).reshape(h, w) * 255).round().astype(np.uint8)
        base = os.path.splitext(sp.output)[0]
        mask_path = base + "_mask.tif"
        m = gdal.GetDriverByName("GTiff").Create(mask_path, w, h, 1, gdal.GDT_Byte, ["COMPRESS=DEFLATE"])
        m.SetGeoTransform((gt[0], gt[1] * W / w, 0, gt[3], 0, gt[5] * H / h))
        m.SetProjection(ds.GetProjection())
        m.GetRasterBand(1).WriteArray(a)
        m = None
        vrt = base + "_blend.vrt"
        with open(vrt, "w", encoding="utf-8") as f:
            f.write(_blend_vrt_xml(ds, os.path.basename(sp.output), os.path.basename(mask_path), w, h))
        ds = None
        out.append(vrt)
    return out


def _blend_vrt_xml(ds, tif, mask, w, h):
    W, H = ds.RasterXSize, ds.RasterYSize
    gt = ds.GetGeoTransform()
    srs = ds.GetProjection().replace("&", "&amp;").replace("<", "&lt;")
    bands = []
    for b, interp in ((1, "Red"), (2, "Green"), (3, "Blue")):
        bands.append(f"""  <VRTRasterBand dataType="Byte" band="{b}"><ColorInterp>{interp}</ColorInterp>
    <SimpleSource><SourceFilename relativeToVRT="1">{tif}</SourceFilename><SourceBand>{b}</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{W}" ySize="{H}"/><DstRect xOff="0" yOff="0" xSize="{W}" ySize="{H}"/></SimpleSource>
  </VRTRasterBand>""")
    bands.append(f"""  <VRTRasterBand dataType="Byte" band="4" subClass="VRTDerivedRasterBand"><ColorInterp>Alpha</ColorInterp>
    <PixelFunctionType>mul</PixelFunctionType><SourceTransferType>Float32</SourceTransferType>
    <ComplexSource><SourceFilename relativeToVRT="1">{tif}</SourceFilename><SourceBand>4</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{W}" ySize="{H}"/><DstRect xOff="0" yOff="0" xSize="{W}" ySize="{H}"/></ComplexSource>
    <ComplexSource resampling="bilinear"><SourceFilename relativeToVRT="1">{mask}</SourceFilename><SourceBand>1</SourceBand>
      <ScaleOffset>0</ScaleOffset><ScaleRatio>0.00392156862745098</ScaleRatio>
      <SrcRect xOff="0" yOff="0" xSize="{w}" ySize="{h}"/><DstRect xOff="0" yOff="0" xSize="{W}" ySize="{H}"/></ComplexSource>
  </VRTRasterBand>""")
    return (f'<VRTDataset rasterXSize="{W}" rasterYSize="{H}">\n  <SRS>{srs}</SRS>\n'
            f'  <GeoTransform>{", ".join(repr(float(v)) for v in gt)}</GeoTransform>\n' + "\n".join(bands) + "\n</VRTDataset>\n")


def write_mosaic(plan, out_vrt, res):
    """Mozaika VRT: źródła w odwrotnej kolejności priorytetu (w VRT wygrywa ostatnie źródło) – przy zakładkach
    obraz z arkusza o wyższym priorytecie (przycięcie do pierwszego)."""
    from . import rectify_raster as rr
    paths = [sp.output for sp in reversed(plan.sheets) if sp.output]
    return rr.build_mosaic(paths, out_vrt, res)


def composite(layers, out_path, block=2048, fmt="cog_webp"):
    """Mozaika z blendowaniem: rastry RGBA (w kolejności rysowania – pierwszy na wierzchu) na wspólnej siatce
    pikseli, składanie „over” blokami (numpy), zapis COG. Zwraca ścieżkę."""
    from osgeo import gdal

    from . import rectify_raster as rr
    dss = [gdal.Open(p) for p in layers]
    gts = [d.GetGeoTransform() for d in dss]
    res = gts[0][1]
    x0 = min(g[0] for g in gts)
    y0 = max(g[3] for g in gts)
    x1 = max(g[0] + d.RasterXSize * g[1] for g, d in zip(gts, dss))
    y1 = min(g[3] + d.RasterYSize * g[5] for g, d in zip(gts, dss))
    W, H = int(round((x1 - x0) / res)), int(round((y0 - y1) / res))
    tmp = os.path.splitext(out_path)[0] + "_tmp.tif"
    drv = gdal.GetDriverByName("GTiff")
    dst = drv.Create(tmp, W, H, 4, gdal.GDT_Byte, ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
    dst.SetGeoTransform((x0, res, 0, y0, 0, -res))
    dst.SetProjection(dss[0].GetProjection())
    for b, ci in zip(range(1, 5), (gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand, gdal.GCI_AlphaBand)):
        dst.GetRasterBand(b).SetColorInterpretation(ci)
    for by in range(0, H, block):
        for bx in range(0, W, block):
            _composite_block(dss, gts, dst, bx, by, min(block, W - bx), min(block, H - by), x0, y0, res)
    dst = None
    drv_name, options = rr.FORMATS[fmt]
    gdal.Translate(out_path, tmp, format=drv_name, creationOptions=options)
    drv.Delete(tmp)
    return out_path


def _composite_block(dss, gts, dst, bx, by, w, h, x0, y0, res):
    rgb = np.zeros((3, h, w))
    acc = np.zeros((h, w))                         # łączne krycie (składanie od góry: over)
    for d, g in zip(dss, gts):
        ox = int(round((x0 - g[0]) / res)) + bx            # kolumna źródła odpowiadająca początkowi bloku
        oy = int(round((g[3] - y0) / res)) + by            # wiersz źródła
        sx, sy = max(0, ox), max(0, oy)
        ex, ey = min(d.RasterXSize, ox + w), min(d.RasterYSize, oy + h)
        if ex <= sx or ey <= sy:
            continue
        arr = d.ReadAsArray(sx, sy, ex - sx, ey - sy).astype(float)
        if arr.shape[0] < 4:
            continue
        a = arr[3] / 255.0
        dx, dy = sx - ox, sy - oy
        sl = (slice(dy, dy + (ey - sy)), slice(dx, dx + (ex - sx)))
        wgt = a * (1 - acc[sl])
        rgb[:, sl[0], sl[1]] += arr[:3] * wgt
        acc[sl] += wgt
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(acc > 0, rgb / np.where(acc > 0, acc, 1), 0)
    for b in range(3):
        dst.GetRasterBand(b + 1).WriteArray(np.clip(out[b], 0, 255).round().astype(np.uint8), bx, by)
    dst.GetRasterBand(4).WriteArray(np.clip(acc * 255, 0, 255).round().astype(np.uint8), bx, by)


def footprints_items(plan, method_label):
    """Zasięgi arkuszy do zapisu w bibliotece (library.save_footprints)."""
    c = plan.correction
    return [{"analysis_id": sp.sheet.key, "ring": [tuple(map(float, p)) for p in sp.ring[::4]],
             "method": method_label, "smoothing": None if not math.isfinite(c.lam) else c.lam,
             "rmse_m": sp.rmse_m, "rmse_corr_m": sp.rmse_corr_m,
             "loo_m": c.loo_rmse * plan.unit_m if math.isfinite(c.loo_rmse) else None} for sp in plan.sheets]
