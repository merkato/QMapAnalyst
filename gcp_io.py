"""
Odczyt i zapis punktów GCP: format Georeferencera QGIS (.points) oraz CSV.

Zgodność z QGIS (src/app/georeferencer/qgsgcplist.cpp):
* zapis: pierwsza linia "#CRS: <WKT>", nagłówek "mapX,mapY,sourceX,sourceY,enable,dX,dY,residual",
* odczyt: kolumny pozycyjne (mapX, mapY, sourceX, sourceY[, enable]); separator "," lub –
  w starszym formacie – tabulator.

Moduł nie zależy od QGIS.
"""

import os

POINTS_HEADER = "mapX,mapY,sourceX,sourceY,enable,dX,dY,residual"
_FALSE = ("0", "false", "no", "nie", "f")


def _is_num(s, comma_decimal=False):
    s = s.strip()
    if comma_decimal:
        s = s.replace(",", ".")
    try:
        float(s)
        return True
    except ValueError:
        return False


def _split_comments(text):
    """Oddziela linie danych od komentarzy; zwraca (definicja CRS z linii '#CRS:' lub None, linie danych)."""
    crs_def, lines = None, []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.upper().startswith("#CRS:"):
            crs_def = s[5:].strip() or None
        elif not s.startswith("#"):
            lines.append(line.rstrip("\r\n"))
    return crs_def, lines


def _delimiter(first):
    if "\t" in first and "," not in first:
        return "\t"
    if first.count(";") > 0 and first.count(";") >= first.count(","):
        return ";"
    return ","


def _column_indices(hl, is_points):
    """Indeksy kolumn (x dawna, y dawna, x ref., y ref.) wg nagłówka lub kolejności formatu."""
    if "mapx" in hl and ("sourcex" in hl or "pixelx" in hl):
        sx = "sourcex" if "sourcex" in hl else "pixelx"
        sy = "sourcey" if "sourcey" in hl else "pixely"
        return [hl.index(sx), hl.index(sy), hl.index("mapx"), hl.index("mapy")]
    if "x_old" in hl and "x_ref" in hl:
        return [hl.index("x_old"), hl.index("y_old"), hl.index("x_ref"), hl.index("y_ref")]
    if is_points:
        return [2, 3, 0, 1]          # format Georeferencera: najpierw mapa, potem źródło
    return [0, 1, 2, 3]              # CSV QMapAnalyst: najpierw mapa dawna


def _parse_row(row, ix, ien, comma_decimal):
    vals = [float(row[i].replace(",", ".") if comma_decimal else row[i]) for i in ix]
    if any(v != v for v in vals):  # NaN
        raise ValueError("NaN")
    enabled = not (ien < len(row) and row[ien] != "" and row[ien].strip().lower() in _FALSE)
    return (*vals, enabled)


def parse_gcp_text(text, filename=""):
    """Zwraca (definicja CRS lub None, lista (ox, oy, rx, ry, enabled), liczba pominiętych wierszy)."""
    crs_def, lines = _split_comments(text)
    if not lines:
        return crs_def, [], 0

    delim = _delimiter(lines[0])
    comma_decimal = delim in (";", "\t")
    rows = [[c.strip().strip('"') for c in ln.split(delim)] for ln in lines]
    header = rows[0]
    has_header = not all(_is_num(h, comma_decimal) for h in header[:4]) if len(header) >= 4 else True
    hl = [h.lower() for h in header] if has_header else []
    data = rows[1:] if has_header else rows

    is_points = filename.lower().endswith(".points") or crs_def is not None or "mapx" in hl
    ix = _column_indices(hl, is_points)
    ien = hl.index("enable") if "enable" in hl else 4

    out, skipped = [], 0
    for row in data:
        try:
            out.append(_parse_row(row, ix, ien, comma_decimal))
        except (ValueError, IndexError):
            skipped += 1
    return crs_def, out, skipped


def format_points(gcps, crs_def=None, residuals=None):
    """gcps: iterowalne (ox, oy, rx, ry, enabled). residuals: opcjonalnie lista (dX, dY)."""
    out = []
    if crs_def:
        out.append(f"#CRS: {crs_def}")
    out.append(POINTS_HEADER)
    for i, (ox, oy, rx, ry, en) in enumerate(gcps):
        dx, dy = residuals[i] if residuals is not None and residuals[i] is not None else (0.0, 0.0)
        res = (dx * dx + dy * dy) ** 0.5
        out.append(",".join([repr(float(rx)), repr(float(ry)), repr(float(ox)), repr(float(oy)),
                             "1" if en else "0", repr(float(dx)), repr(float(dy)), repr(float(res))]))
    return "\n".join(out) + "\n"


def format_csv(gcps):
    out = ["x_old,y_old,x_ref,y_ref,enable"]
    for ox, oy, rx, ry, en in gcps:
        out.append(",".join([repr(float(ox)), repr(float(oy)), repr(float(rx)), repr(float(ry)), "1" if en else "0"]))
    return "\n".join(out) + "\n"


def read_file(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return parse_gcp_text(f.read(), os.path.basename(path))


def write_file(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
