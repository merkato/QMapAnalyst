"""
Odczyt i zapis punktów GCP: format Georeferencera QGIS (.points) oraz CSV.

Zgodność z QGIS (src/app/georeferencer/qgsgcplist.cpp):
* zapis: pierwsza linia "#CRS: <WKT>", nagłówek "mapX,mapY,sourceX,sourceY,enable,dX,dY,residual",
* odczyt: kolumny pozycyjne (mapX, mapY, sourceX, sourceY[, enable]); separator "," lub –
  w starszym formacie – tabulator.

Nazwy punktów:
* plik .points pozostaje dokładnie w formacie Georeferencera (QGIS nie zna nazw i zgubiłby
  dodatkową kolumnę przy ponownym zapisie),
* nazwy trafiają do pliku towarzyszącego "<plik>.points.names.csv" z kolumnami
  id,name,sourceX,sourceY,mapX,mapY. Przy odczycie nazwy są przypisywane po współrzędnych
  skanu (odporne na zmianę kolejności, dodanie lub usunięcie punktów w Georeferencerze),
  a w drugiej kolejności po numerze punktu – gdy liczba punktów się zgadza,
* w CSV QMapAnalyst nazwa jest zwykłą kolumną "name".

Moduł nie zależy od QGIS.
"""

import csv
import io
import os

POINTS_HEADER = "mapX,mapY,sourceX,sourceY,enable,dX,dY,residual"
NAMES_SUFFIX = ".names.csv"
NAMES_HEADER = ["id", "name", "sourceX", "sourceY", "mapX", "mapY"]
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


def _rows(lines, delim):
    return [[c.strip() for c in r] for r in csv.reader(lines, delimiter=delim)]


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


def _parse_row(row, ix, ien, iname, comma_decimal):
    vals = [float(row[i].replace(",", ".") if comma_decimal else row[i]) for i in ix]
    if any(v != v for v in vals):  # NaN
        raise ValueError("NaN")
    enabled = not (0 <= ien < len(row) and row[ien] != "" and row[ien].strip().lower() in _FALSE)
    name = row[iname].strip() if 0 <= iname < len(row) else ""
    return (*vals, enabled, name)


def parse_gcp_text(text, filename=""):
    """Zwraca (definicja CRS lub None, lista (ox, oy, rx, ry, enabled, name), liczba pominiętych wierszy)."""
    crs_def, lines = _split_comments(text)
    if not lines:
        return crs_def, [], 0

    delim = _delimiter(lines[0])
    comma_decimal = delim in (";", "\t")
    rows = _rows(lines, delim)
    header = rows[0]
    has_header = not all(_is_num(h, comma_decimal) for h in header[:4]) if len(header) >= 4 else True
    hl = [h.lower() for h in header] if has_header else []
    data = rows[1:] if has_header else rows

    is_points = filename.lower().endswith(".points") or crs_def is not None or "mapx" in hl
    ix = _column_indices(hl, is_points)
    # bez nagłówka kolumna 5 to "enable" (format Georeferencera); z nagłówkiem – tylko nazwana
    ien = hl.index("enable") if "enable" in hl else (-1 if hl else 4)
    iname = hl.index("name") if "name" in hl else -1

    out, skipped = [], 0
    for row in data:
        try:
            out.append(_parse_row(row, ix, ien, iname, comma_decimal))
        except (ValueError, IndexError):
            skipped += 1
    return crs_def, out, skipped


def format_points(gcps, crs_def=None, residuals=None):
    """gcps: iterowalne (ox, oy, rx, ry, enabled[, name]) – nazwa jest pomijana (czysty format QGIS).
    residuals: opcjonalnie lista (dX, dY)."""
    out = []
    if crs_def:
        out.append(f"#CRS: {crs_def}")
    out.append(POINTS_HEADER)
    for i, (ox, oy, rx, ry, en, *_) in enumerate(gcps):
        dx, dy = residuals[i] if residuals is not None and residuals[i] is not None else (0.0, 0.0)
        res = (dx * dx + dy * dy) ** 0.5
        out.append(",".join([repr(float(rx)), repr(float(ry)), repr(float(ox)), repr(float(oy)),
                             "1" if en else "0", repr(float(dx)), repr(float(dy)), repr(float(res))]))
    return "\n".join(out) + "\n"


def _csv_text(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def has_names(gcps):
    return any(len(g) > 5 and g[5] for g in gcps)


def format_csv(gcps):
    """gcps: (ox, oy, rx, ry, enabled[, name]). Kolumna "name" tylko gdy któryś punkt ma nazwę."""
    gcps = list(gcps)
    named = has_names(gcps)
    header = ["x_old", "y_old", "x_ref", "y_ref", "enable"] + (["name"] if named else [])
    rows = []
    for g in gcps:
        ox, oy, rx, ry, en = g[:5]
        r = [repr(float(ox)), repr(float(oy)), repr(float(rx)), repr(float(ry)), "1" if en else "0"]
        if named:
            r.append(g[5] if len(g) > 5 else "")
        rows.append(r)
    return _csv_text(header, rows)


# ---------------------------------------------------------------------------
#  Plik towarzyszący z nazwami punktów (dla .points)
# ---------------------------------------------------------------------------

def names_path(points_path):
    return points_path + NAMES_SUFFIX


def format_names(gcps):
    """Plik nazw: id, name, sourceX, sourceY, mapX, mapY. Kolumny jak w .points, więc plik
    daje się też wczytać samodzielnie jako lista GCP (wszystkie punkty aktywne)."""
    rows = [[str(i), g[5] if len(g) > 5 else "", repr(float(g[0])), repr(float(g[1])),
             repr(float(g[2])), repr(float(g[3]))] for i, g in enumerate(gcps, 1)]
    return _csv_text(NAMES_HEADER, rows)


def parse_names(text):
    """Zwraca listę (id, name, sourceX, sourceY) z pliku nazw (wiersze niepoprawne pomijane)."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []
    rows = _rows(lines, ",")
    hl = [h.lower() for h in rows[0]]
    try:
        iid, iname, isx, isy = (hl.index(k) for k in ("id", "name", "sourcex", "sourcey"))
    except ValueError:
        return []
    out = []
    for r in rows[1:]:
        try:
            out.append((int(r[iid]), r[iname], float(r[isx]), float(r[isy])))
        except (ValueError, IndexError):
            continue
    return out


def assign_names(gcps, names, tol=1e-3):
    """Przypisuje nazwy punktom (ox, oy, rx, ry, enabled[, name]); zwraca 6-krotki.

    Najpierw dopasowanie po współrzędnych skanu (|Δ| ≤ tol px), potem – dla punktów bez
    dopasowania – po numerze, ale tylko gdy liczba punktów w pliku nazw jest taka sama jak
    w .points (wtedy kolejność najpewniej się nie zmieniła, np. poprawiono współrzędne)."""
    gcps = [tuple(g[:5]) + ((g[5] if len(g) > 5 else ""),) for g in gcps]
    if not names:
        return gcps
    used, result, unmatched = set(), list(gcps), []
    for i, g in enumerate(gcps):
        hit = next((k for k, (_, _nm, sx, sy) in enumerate(names)
                    if k not in used and abs(g[0] - sx) <= tol and abs(g[1] - sy) <= tol), None)
        if hit is None:
            unmatched.append(i)
            continue
        used.add(hit)
        result[i] = g[:5] + (names[hit][1],)
    if len(names) == len(gcps):
        by_id = {nid: k for k, (nid, *_rest) in enumerate(names)}
        for i in unmatched:
            k = by_id.get(i + 1)
            if k is not None and k not in used:
                used.add(k)
                result[i] = gcps[i][:5] + (names[k][1],)
    return result


def read_names(points_path):
    """Nazwy z pliku towarzyszącego lub [] gdy go nie ma."""
    p = names_path(points_path)
    if not os.path.isfile(p):
        return []
    with open(p, "r", encoding="utf-8-sig") as f:
        return parse_names(f.read())


def read_file(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return parse_gcp_text(f.read(), os.path.basename(path))


def write_file(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
