"""
Import punktów do biblioteki: odczyt CSV, dopasowanie pól, rozpoznawanie typów, wykrywanie duplikatów.

Moduł bez zależności od QGIS (testowalny samodzielnie). Rekord źródłowy to słownik
{nazwa pola: wartość} z dodatkowymi kluczami X_KEY / Y_KEY (współrzędne już w układzie biblioteki).
"""

import csv
import io
import math

from . import library as lb

X_KEY, Y_KEY = "__x__", "__y__"

# role pól biblioteki -> nazwy pól źródłowych rozpoznawane automatycznie (porównanie bez wielkości liter)
FIELD_GUESSES = {
    "name": ("name", "nazwa", "nazwa_glowna", "nazwaglowna", "nazwa_miejscowosci", "miejscowosc", "toponym", "label"),
    "alt_names": ("alt_names", "alt_name", "nazwy_dawne", "nazwa_dawna", "nazwy_obce", "old_name", "historic_name",
                  "nazwa_historyczna", "warianty"),
    "type": ("type", "typ", "rodzaj", "rodzaj_obiektu", "place", "kategoria", "class", "fclass"),
    "feature": ("feature", "obiekt", "wskazany_obiekt"),
    "sigma_m": ("sigma_m", "sigma", "niepewnosc", "dokladnosc", "accuracy"),
    "valid_from": ("valid_from", "od", "od_roku", "rok_od"),
    "valid_to": ("valid_to", "do", "do_roku", "rok_do"),
    "source": ("source", "zrodlo"),
    "note": ("note", "uwagi", "opis", "komentarz"),
    "x": ("x", "lon", "lng", "long", "longitude", "easting", "e", "dlugosc"),
    "y": ("y", "lat", "latitude", "northing", "n", "szerokosc"),
}

# wartości pola typu w popularnych źródłach (PRNG, OSM, BDOT10k) -> kod library.POINT_TYPES
TYPE_SYNONYMS = {
    "town": ("miasto", "city", "town", "stolica"),
    "village": ("wieś", "wies", "village", "hamlet", "osada", "przysiółek", "przysiolek", "kolonia", "część wsi",
                "czesc wsi", "isolated_dwelling", "osada leśna", "osada lesna", "dorf", "weiler"),
    "church": ("kościół", "kosciol", "church", "cerkiew", "kaplica", "chapel", "place_of_worship", "kirche"),
    "monastery": ("klasztor", "monastery", "opactwo", "abbey", "kloster"),
    "castle": ("zamek", "castle", "ruiny zamku", "schloss", "burg"),
    "manor": ("dwór", "dwor", "pałac", "palac", "manor", "folwark", "gutshof"),
    "mill": ("młyn", "mlyn", "mill", "watermill", "windmill", "wiatrak", "mühle", "muhle"),
    "bridge": ("most", "bridge", "brücke", "brucke"),
    "confluence": ("ujście", "ujscie", "confluence", "mündung", "mundung"),
    "crossroads": ("skrzyżowanie", "skrzyzowanie", "crossroads", "crossing"),
    "peak": ("szczyt", "góra", "gora", "peak", "summit", "wzgórze", "wzgorze", "hill", "berg"),
    "control": ("punkt osnowy", "osnowa", "triangulation", "survey_point", "trig"),
}
_TYPE_LOOKUP = {lb.normalize(v): code for code, values in TYPE_SYNONYMS.items() for v in values}
_TYPE_LOOKUP.update({code: code for code in lb.POINT_TYPES})


def guess_field(fields, role):
    """Pole źródłowe najlepiej pasujące do roli (np. "name") albo None."""
    norm = {lb.normalize(f).replace(" ", "_"): f for f in fields}
    for cand in FIELD_GUESSES.get(role, ()):
        if cand in norm:
            return norm[cand]
    return None


def map_type(value, default=""):
    """Kod typu punktu dla wartości ze źródła (synonimy PL/EN/DE, PRNG, OSM)."""
    if value is None:
        return default
    return _TYPE_LOOKUP.get(lb.normalize(str(value)), default)


def parse_number(value):
    """Liczba z tekstu (przecinek dziesiętny, spacje tysięcy) albo None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        x = float(text)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _int_or_none(value):
    x = parse_number(value)
    return int(round(x)) if x is not None else None


def _text(value):
    return "" if value is None else str(value).strip()


# ----------------------------------------------------------------------
#  CSV
# ----------------------------------------------------------------------
def read_csv(text):
    """(pola, rekordy) z tekstu CSV; separator wykrywany automatycznie (, ; tab |)."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    fields = [f.strip() for f in (reader.fieldnames or [])]
    rows = []
    for raw in reader:
        rows.append({(k or "").strip(): v for k, v in raw.items()})
    return fields, rows


def read_csv_file(path):
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            return read_csv(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return read_csv(data.decode("utf-8", errors="replace"))


# ----------------------------------------------------------------------
#  Budowa punktów
# ----------------------------------------------------------------------
def build_points(records, mapping, default_type="", default_source=""):
    """RefPoint z rekordów. mapping: rola -> pole źródłowe (None – brak).
    Zwraca (punkty, liczba pominiętych: bez nazwy lub współrzędnych)."""
    points, skipped = [], 0
    for rec in records:
        p = _point_from(rec, mapping, default_type, default_source)
        if p is None:
            skipped += 1
        else:
            points.append(p)
    return points, skipped


def _field(rec, mapping, role):
    f = mapping.get(role)
    return rec.get(f) if f else None


def _point_from(rec, mapping, default_type, default_source):
    name = _text(_field(rec, mapping, "name"))
    x, y = parse_number(rec.get(X_KEY)), parse_number(rec.get(Y_KEY))
    if not name or x is None or y is None:
        return None
    alt = _text(_field(rec, mapping, "alt_names")).replace(";", "|").replace(",", "|")
    alt = "|".join(a.strip() for a in alt.split("|") if a.strip() and a.strip() != name)
    typ = map_type(_field(rec, mapping, "type"), default_type) if mapping.get("type") else default_type
    sigma = parse_number(_field(rec, mapping, "sigma_m"))
    return lb.RefPoint(
        name=name, x=x, y=y, alt_names=alt, type=typ,
        feature=_text(_field(rec, mapping, "feature")),
        sigma_m=sigma if sigma is not None and sigma > 0 else None,
        valid_from=_int_or_none(_field(rec, mapping, "valid_from")),
        valid_to=_int_or_none(_field(rec, mapping, "valid_to")),
        source=_text(_field(rec, mapping, "source")) or default_source,
        note=_text(_field(rec, mapping, "note")),
    )


# ----------------------------------------------------------------------
#  Duplikaty
# ----------------------------------------------------------------------
def _names(p):
    return {lb.normalize(n) for n in p.names()}


def find_duplicates(existing, new_points, tol):
    """Podział nowych punktów: (do dodania, duplikaty [(nowy, istniejący)], bliskie o innej nazwie [(nowy, istniejący)]).

    Duplikat = punkt w odległości ≤ tol o tej samej nazwie (także dawnej) – również w obrębie importu.
    Punkt bliski o innej nazwie jest dodawany (np. kościół i dwór w jednej wsi), ale raportowany."""
    pool = list(existing)
    to_add, dups, near = [], [], []
    for p in new_points:
        names = _names(p)
        close = [q for q in pool if math.hypot(q.x - p.x, q.y - p.y) <= tol]
        same = next((q for q in close if names & _names(q)), None)
        if same is not None:
            dups.append((p, same))
            continue
        if close:
            near.append((p, close[0]))
        to_add.append(p)
        pool.append(p)
    return to_add, dups, near


def merge_alt_names(target, other):
    """Nazwy z other dopisane do nazw dawnych target (bez powtórzeń). True, gdy coś dodano."""
    have = _names(target)
    extra = [n for n in other.names() if lb.normalize(n) not in have]
    if not extra:
        return False
    target.alt_names = "|".join([a for a in (target.alt_names or "").split("|") if a] + extra)
    return True
