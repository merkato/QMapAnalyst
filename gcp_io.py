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

Powiązanie z biblioteką punktów (od 0.3.0): plik towarzyszący i CSV mają dodatkowo kolumny
uid (punkt biblioteki), status (sure / uncertain / illegible) i comment. Punkty zestawu oznaczone
„brak na mapie” zapisywane są w pliku towarzyszącym jako wiersze bez współrzędnych (status absent).
Wiersz punktu to krotka (ox, oy, rx, ry, enabled, name, uid, status, comment); krótsze krotki
są uzupełniane wartościami domyślnymi.

Moduł nie zależy od QGIS.
"""

import csv
import io
import os

POINTS_HEADER = "mapX,mapY,sourceX,sourceY,enable,dX,dY,residual"
NAMES_SUFFIX = ".names.csv"
NAMES_HEADER = ["id", "name", "sourceX", "sourceY", "mapX", "mapY", "uid", "status", "comment"]
LINK_COLUMNS = ["uid", "status", "comment"]
STATUSES = ("sure", "uncertain", "illegible")
_FALSE = ("0", "false", "no", "nie", "f")


def full(g):
    """Wiersz punktu jako 9-krotka (ox, oy, rx, ry, enabled, name, uid, status, comment)."""
    g = tuple(g)
    name = g[5] if len(g) > 5 else ""
    uid = (g[6] if len(g) > 6 else "") or ""
    status = g[7] if len(g) > 7 and g[7] in STATUSES else "sure"
    comment = (g[8] if len(g) > 8 else "") or ""
    return tuple(g[:5]) + (name or "", uid, status, comment)


def has_links(gcps):
    """Czy któryś punkt ma powiązanie z biblioteką, status inny niż pewny albo uwagi."""
    return any(f[6] or f[7] != "sure" or f[8] for f in map(full, gcps))


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


def _cell(row, i):
    return row[i].strip() if 0 <= i < len(row) else ""


def _parse_row(row, ix, ien, extra, comma_decimal):
    """extra – indeksy kolumn (name, uid, status, comment); -1 gdy brak."""
    vals = [float(row[i].replace(",", ".") if comma_decimal else row[i]) for i in ix]
    if any(v != v for v in vals):  # NaN
        raise ValueError("NaN")
    enabled = not (0 <= ien < len(row) and row[ien] != "" and row[ien].strip().lower() in _FALSE)
    return full((*vals, enabled, *(_cell(row, i) for i in extra)))


def parse_gcp_text(text, filename=""):
    """Zwraca (definicja CRS lub None, lista 9-krotek – zob. full(), liczba pominiętych wierszy)."""
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
    extra = [hl.index(k) if k in hl else -1 for k in ("name", "uid", "status", "comment")]

    out, skipped = [], 0
    for row in data:
        try:
            out.append(_parse_row(row, ix, ien, extra, comma_decimal))
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
    """gcps: wiersze punktów (zob. full()). Kolumna "name" tylko gdy któryś punkt ma nazwę,
    kolumny uid/status/comment – gdy któryś punkt ma powiązanie z biblioteką, status lub uwagi."""
    gcps = [full(g) for g in gcps]
    named, linked = has_names(gcps), has_links(gcps)
    header = ["x_old", "y_old", "x_ref", "y_ref", "enable"] + (["name"] if named else []) + (LINK_COLUMNS if linked else [])
    rows = []
    for g in gcps:
        ox, oy, rx, ry, en = g[:5]
        r = [repr(float(ox)), repr(float(oy)), repr(float(rx)), repr(float(ry)), "1" if en else "0"]
        if named:
            r.append(g[5])
        if linked:
            r += list(g[6:9])
        rows.append(r)
    return _csv_text(header, rows)


# ---------------------------------------------------------------------------
#  Plik towarzyszący z nazwami punktów (dla .points)
# ---------------------------------------------------------------------------

def names_path(points_path):
    return points_path + NAMES_SUFFIX


def format_names(gcps, absent=()):
    """Plik towarzyszący: id, name, sourceX, sourceY, mapX, mapY, uid, status, comment. Kolumny jak w .points,
    więc plik daje się też wczytać samodzielnie jako lista GCP. absent – (uid, name, comment) punktów
    „brak na mapie”: wiersze bez numeru i współrzędnych (pomijane przy odczycie jako GCP)."""
    rows = []
    for i, g in enumerate(map(full, gcps), 1):
        rows.append([str(i), g[5], repr(float(g[0])), repr(float(g[1])), repr(float(g[2])), repr(float(g[3]))]
                    + list(g[6:9]))
    rows += [["", name, "", "", "", "", uid, "absent", comment] for uid, name, comment in absent]
    return _csv_text(NAMES_HEADER, rows)


def parse_names(text):
    """Zwraca listę (id, name, sourceX, sourceY, uid, status, comment) z pliku towarzyszącego
    (wiersze niepoprawne pomijane). Wiersze „brak na mapie” mają id i współrzędne None."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []
    rows = _rows(lines, ",")
    hl = [h.lower() for h in rows[0]]
    try:
        iid, iname, isx, isy = (hl.index(k) for k in ("id", "name", "sourcex", "sourcey"))
    except ValueError:
        return []
    iuid, ist, icom = (hl.index(k) if k in hl else -1 for k in ("uid", "status", "comment"))
    out = []
    for r in rows[1:]:
        uid, status, comment = _cell(r, iuid), _cell(r, ist), _cell(r, icom)
        if status == "absent":
            if uid:
                out.append((None, _cell(r, iname), None, None, uid, status, comment))
            continue
        try:
            out.append((int(r[iid]), r[iname], float(r[isx]), float(r[isy]), uid, status, comment))
        except (ValueError, IndexError):
            continue
    return out


def absent_entries(names):
    """Punkty „brak na mapie” z pliku towarzyszącego: [(uid, name, comment)]."""
    return [(n[4], n[1], n[6]) for n in names if n[5] == "absent"]


def assign_names(gcps, names, tol=1e-3):
    """Przypisuje nazwy (oraz uid, status, uwagi) punktom; zwraca 9-krotki (zob. full()).

    Najpierw dopasowanie po współrzędnych skanu (|Δ| ≤ tol px), potem – dla punktów bez
    dopasowania – po numerze, ale tylko gdy liczba punktów w pliku nazw jest taka sama jak
    w .points (wtedy kolejność najpewniej się nie zmieniła, np. poprawiono współrzędne)."""
    gcps = [full(g) for g in gcps]
    names = [n for n in names if n[0] is not None]
    if not names:
        return gcps

    def merged(g, n):
        return full(g[:5] + (n[1],) + (tuple(n[4:7]) if len(n) > 4 else ()))

    used, result, unmatched = set(), list(gcps), []
    for i, g in enumerate(gcps):
        hit = next((k for k, n in enumerate(names)
                    if k not in used and abs(g[0] - n[2]) <= tol and abs(g[1] - n[3]) <= tol), None)
        if hit is None:
            unmatched.append(i)
            continue
        used.add(hit)
        result[i] = merged(g, names[hit])
    if len(names) == len(gcps):
        by_id = {n[0]: k for k, n in enumerate(names)}
        for i in unmatched:
            k = by_id.get(i + 1)
            if k is not None and k not in used:
                used.add(k)
                result[i] = merged(gcps[i], names[k])
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
