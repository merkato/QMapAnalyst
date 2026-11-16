"""
Przetworzenie rastra wg rektyfikacji (GDAL/OGR, bez Qt): gęsta siatka GCP → gdalwarp -tps,
przycięcie do linii cięcia (obraz ramki), kanał alfa, COG/GeoTIFF, mozaika VRT.
"""

import json
import os
import tempfile

from osgeo import gdal, ogr, osr

gdal.UseExceptions()

FORMATS = {
    "cog_webp": ("COG", ["COMPRESS=WEBP", "QUALITY=88", "BLOCKSIZE=512", "BIGTIFF=IF_SAFER",
                         "OVERVIEW_RESAMPLING=AVERAGE", "NUM_THREADS=ALL_CPUS"]),
    "cog_jpeg": ("COG", ["COMPRESS=JPEG", "QUALITY=88", "BLOCKSIZE=512", "BIGTIFF=IF_SAFER",
                         "OVERVIEW_RESAMPLING=AVERAGE", "NUM_THREADS=ALL_CPUS"]),
    "gtiff_lzw": ("GTiff", ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"]),
}


def srs_from(crs_text):
    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs_text)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def scan_to_pixel(scan_path):
    """Funkcja (x, y) skanu (jak na mapie dawnej w QGIS) → (piksel, linia).
    Skan bez georeferencji: QGIS rysuje go z y = −linia."""
    ds = gdal.Open(scan_path)
    gt = ds.GetGeoTransform(can_return_null=True)
    ds = None
    if gt is None:
        return lambda x, y: (x, -y)
    inv = gdal.InvGeoTransform(gt)
    return lambda x, y: gdal.ApplyGeoTransform(inv, x, y)


def rgb_options(scan_path, fmt):
    """WEBP/JPEG wymagają RGB: paleta → rozwinięcie, skala szarości → trzy kopie kanału, nadmiarowe kanały – pominięte."""
    if fmt == "gtiff_lzw":
        return {}
    ds = gdal.Open(scan_path)
    n = ds.RasterCount
    palette = ds.GetRasterBand(1).GetColorTable() is not None
    ds = None
    if palette:
        return {"rgbExpand": "rgb"}
    if n < 3:
        return {"bandList": [1, 1, 1]}
    if n > 3:
        return {"bandList": [1, 2, 3]}
    return {}


def write_polygon(path, ring, srs, driver="GeoJSON"):
    drv = ogr.GetDriverByName(driver)
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    lyr = ds.CreateLayer("cut", srs, ogr.wkbPolygon)
    r = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in list(ring) + [ring[0]]:
        r.AddPoint_2D(float(x), float(y))
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(r)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(poly)
    lyr.CreateFeature(f)
    ds = None


def warp_scan(scan_path, gcps, ring, out_path, crs_text, res, fmt="cog_webp", progress=None, resampling="cubic"):
    """gcps – [(x_skanu, y_skanu, X, Y)] w układzie skanu jak w QGIS; ring – linia cięcia w terenie
    (None – bez przycięcia). Zwraca ścieżkę wyniku."""
    to_px = scan_to_pixel(scan_path)
    srs = srs_from(crs_text)
    wkt = srs.ExportToWkt()
    driver, options = FORMATS[fmt]
    tmp = tempfile.mkdtemp(prefix="qma_rect_")
    try:
        vrt = os.path.join(tmp, "gcp.vrt")
        g = [gdal.GCP(X, Y, 0, *to_px(x, y)) for x, y, X, Y in gcps]
        gdal.Translate(vrt, scan_path, format="VRT", GCPs=g, outputSRS=wkt, **rgb_options(scan_path, fmt))
        kw = {}
        if ring is not None:
            cut = os.path.join(tmp, "cut.geojson")
            write_polygon(cut, ring, srs)
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            kw = {"cutlineDSName": cut, "outputBounds": (min(xs), min(ys), max(xs), max(ys)),
                  "warpOptions": ["NUM_THREADS=ALL_CPUS", "CUTLINE_ALL_TOUCHED=FALSE"]}
        if os.path.exists(out_path):
            gdal.GetDriverByName("GTiff").Delete(out_path)
        cb = (lambda pct, _msg, _data: 0 if progress(pct) is False else 1) if progress else None
        gdal.Warp(out_path, vrt, format=driver, tps=True, dstSRS=wkt, xRes=res, yRes=res,
                  targetAlignedPixels=True, dstAlpha=True, resampleAlg=resampling, multithread=True,
                  warpMemoryLimit=1024, creationOptions=options, callback=cb, **kw)
    finally:
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        os.rmdir(tmp)
    return out_path


def write_settings(out_path, settings):
    """Ustawienia rektyfikacji obok wyniku (*.rectify.json) – do odtworzenia wyniku."""
    path = os.path.splitext(out_path)[0] + ".rectify.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=1)
    return path


def build_mosaic(paths, out_vrt, res):
    """Mozaika VRT ze ścieżkami względnymi (arkusze na wspólnej siatce pikseli)."""
    gdal.BuildVRT(out_vrt, list(paths), resolution="user", xRes=res, yRes=res, targetAlignedPixels=True)
    base = os.path.dirname(os.path.abspath(out_vrt))
    with open(out_vrt, encoding="utf-8") as f:
        txt = f.read()
    for p in paths:
        ap = os.path.abspath(p)
        if os.path.dirname(ap) == base:
            txt = txt.replace(f'relativeToVRT="0">{p}<', f'relativeToVRT="1">{os.path.basename(p)}<')
    with open(out_vrt, "w", encoding="utf-8") as f:
        f.write(txt)
    return out_vrt
