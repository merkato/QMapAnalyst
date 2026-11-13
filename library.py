"""
Biblioteka punktów referencyjnych – jeden plik GeoPackage użytkownika.

Tabele (schemat w wersji SCHEMA_VERSION):
    ref_points   – punkty biblioteki (geometria punktowa, klucz uid)
    point_sets   – zestawy punktów (np. regionalne), set_id = FID
    set_members  – przynależność punktów do zestawów (set_id + uid)
    analyses     – analizy map (skan, rok, skala, arkusz…), analysis_id = FID
    gcp_links    – punkty GCP analizy: położenie na skanie, kopia współrzędnych odniesienia, status
    qma_meta     – wersja schematu

Zasady (docs/plan_0.3.md):
  - wyniki obliczeń nie są przechowywane – przelicza się je z gcp_links;
  - współrzędne odniesienia GCP są kopią z chwili powiązania (w układzie analizy), zmianę punktu
    w bibliotece wykrywa changed_refs();
  - status „absent” (brak na mapie) zapisuje się bez położenia na skanie.

Moduł korzysta tylko z GDAL/OGR (bez qgis.core), więc działa w testach bez QgsApplication.
Współrzędne zawsze w kolejności x/y (wschód, północ; dla układów geograficznych: długość, szerokość).
Kody typów i statusów są stałe (ASCII); nazwy wyświetlane tłumaczy interfejs.
"""

import datetime
import functools
import hashlib
import math
import os
import unicodedata
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, fields

from osgeo import gdal, ogr, osr

SCHEMA_VERSION = 1

# status identyfikacji punktu na danej mapie
STATUSES = ("sure", "uncertain", "illegible", "absent")
COMPUTABLE_STATUSES = ("sure", "uncertain")   # tylko te mogą wejść do obliczeń
# czynnik wagi a priori dla statusu (waga ∝ 1/σ²: niepewny ≈ dwukrotnie większy błąd wskazania)
STATUS_WEIGHTS = {"sure": 1.0, "uncertain": 0.25}

POINT_TYPES = ("town", "village", "church", "monastery", "castle", "manor", "mill", "bridge",
               "confluence", "crossroads", "peak", "control", "other")

_POINTS = "ref_points"
_SETS = "point_sets"
_MEMBERS = "set_members"
_ANALYSES = "analyses"
_LINKS = "gcp_links"
_META = "qma_meta"

S, I, R = ogr.OFTString, ogr.OFTInteger, ogr.OFTReal
_SCHEMA = {
    _POINTS: [("uid", S), ("name", S), ("alt_names", S), ("type", S), ("feature", S), ("sigma_m", R),
              ("valid_from", I), ("valid_to", I), ("source", S), ("note", S)],
    _SETS: [("name", S), ("description", S)],
    _MEMBERS: [("set_id", I), ("uid", S)],
    _ANALYSES: [("title", S), ("author", S), ("year_from", I), ("year_to", I), ("scale", I), ("sheet", S),
                ("dpi", R), ("scan_path", S), ("scan_sha256", S), ("ref_crs", S), ("note", S),
                ("created", S), ("modified", S)],
    _LINKS: [("analysis_id", I), ("seq", I), ("uid", S), ("name", S), ("x_old", R), ("y_old", R),
             ("ref_x", R), ("ref_y", R), ("enabled", I), ("status", S), ("comment", S)],
    _META: [("key", S), ("value", S)],
}
_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS qma_ref_points_uid ON ref_points(uid)",
    "CREATE UNIQUE INDEX IF NOT EXISTS qma_set_members_key ON set_members(set_id, uid)",
    "CREATE INDEX IF NOT EXISTS qma_gcp_links_analysis ON gcp_links(analysis_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS qma_gcp_links_key ON gcp_links(analysis_id, uid) WHERE uid IS NOT NULL",
)

# migracje: wersja -> funkcja(ds) podnosząca schemat do wersji + 1
_MIGRATIONS = {}


class LibraryError(Exception):
    """Błąd biblioteki punktów."""


class NotALibrary(LibraryError):
    """Plik nie jest biblioteką QMapAnalyst (brak tabeli qma_meta)."""


class SchemaTooNew(LibraryError):
    """Plik utworzyła nowsza wersja wtyczki."""

    def __init__(self, found):
        super().__init__(f"schema version {found} > {SCHEMA_VERSION}")
        self.found = found


class PointInUse(LibraryError):
    """Punkt jest użyty w analizach (lista analysis_id w .analyses)."""

    def __init__(self, uid, analyses):
        super().__init__(f"point {uid} used in analyses {analyses}")
        self.uid = uid
        self.analyses = analyses


@dataclass
class RefPoint:
    name: str
    x: float
    y: float
    uid: str = ""
    alt_names: str = ""          # nazwy historyczne / obce rozdzielone „|”
    type: str = ""               # kod z POINT_TYPES
    feature: str = ""            # co dokładnie wskazuje punkt (kościół parafialny, rynek…)
    sigma_m: float = None        # niepewność współrzędnych odniesienia [m]
    valid_from: int = None       # rok powstania / przeniesienia obiektu
    valid_to: int = None         # rok zniszczenia / przeniesienia
    source: str = ""             # PRNG, OSM, BDOT10k, ręcznie…
    note: str = ""

    def names(self):
        return [self.name] + [n.strip() for n in (self.alt_names or "").split("|") if n.strip()]


@dataclass
class Analysis:
    title: str = ""
    author: str = ""
    year_from: int = None
    year_to: int = None
    scale: int = None            # mianownik skali nominalnej
    sheet: str = ""
    dpi: float = None
    scan_path: str = ""
    scan_sha256: str = ""
    ref_crs: str = ""            # układ odniesienia współrzędnych ref_x/ref_y (authid lub WKT)
    note: str = ""
    created: str = ""
    modified: str = ""
    analysis_id: int = None


@dataclass
class Link:
    x_old: float = None
    y_old: float = None
    ref_x: float = None
    ref_y: float = None
    uid: str = None              # None – punkt spoza biblioteki
    name: str = ""
    enabled: bool = True
    status: str = "sure"
    comment: str = ""

    @property
    def computable(self):
        return self.status in COMPUTABLE_STATUSES and self.x_old is not None and self.ref_x is not None


_POINT_FIELDS = [f for f, _t in _SCHEMA[_POINTS]]
_ANALYSIS_FIELDS = [f for f, _t in _SCHEMA[_ANALYSES]]
_LINK_FIELDS = [f for f, _t in _SCHEMA[_LINKS] if f not in ("analysis_id", "seq")]


# ----------------------------------------------------------------------
#  Funkcje pomocnicze
# ----------------------------------------------------------------------
_FOLD = str.maketrans({"ł": "l", "Ł": "L", "ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O",
                       "đ": "d", "Đ": "D", "œ": "oe", "Œ": "OE", "ı": "i"})


def normalize(text):
    """Tekst do wyszukiwania: bez znaków diakrytycznych, małe litery, pojedyncze spacje."""
    s = unicodedata.normalize("NFKD", (text or "").translate(_FOLD))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.casefold().split())


def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _q(value):
    """Literał tekstowy do filtra atrybutów OGR."""
    return "'" + str(value).replace("'", "''") + "'"


def _srs(crs):
    srs = osr.SpatialReference()
    if srs.SetFromUserInput(str(crs)) != 0:
        raise LibraryError(f"invalid CRS: {crs}")
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _ok(err, what):
    if err != ogr.OGRERR_NONE:
        raise LibraryError(f"{what}: OGR error {err} {gdal.GetLastErrorMsg()}")


def _get(feat, name):
    if not feat.IsFieldSetAndNotNull(name):
        return None
    return feat.GetField(name)


def _set(feat, name, value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        feat.SetFieldNull(name)
    else:
        feat.SetField(name, value)


def _exceptions():
    """Wyjątki GDAL włączone lokalnie – niezależnie od globalnego ustawienia (QGIS, GDAL 4)."""
    mgr = getattr(gdal, "ExceptionMgr", None)
    return mgr(useExceptions=True) if mgr else nullcontext()


def _guarded(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _exceptions():
            try:
                return fn(*args, **kwargs)
            except RuntimeError as e:
                raise LibraryError(str(e)) from e
    return wrapper


def _match_score(query, point):
    """0 – pełna zgodność nazwy, 1 – początek, 2 – początek słowa, 3 – fragment, None – brak."""
    best = None
    for n in point.names():
        n = normalize(n)
        if n == query:
            s = 0
        elif n.startswith(query):
            s = 1
        elif any(w.startswith(query) for w in n.split()):
            s = 2
        elif query in n:
            s = 3
        else:
            continue
        best = s if best is None else min(best, s)
    return best


def search_points(points, text, near=None, limit=20, max_dist=None):
    """Wyszukiwanie w liście RefPoint (bez dostępu do pliku) – zob. Library.search."""
    query = normalize(text)
    out = []
    for p in points:
        score = _match_score(query, p) if query else 3
        if score is None:
            continue
        dist = math.hypot(p.x - near[0], p.y - near[1]) if near is not None else None
        if max_dist is not None and dist is not None and dist > max_dist:
            continue
        out.append((score, dist if dist is not None else 0.0, normalize(p.name), p, dist))
    out.sort(key=lambda t: t[:3])
    return [(p, d) for *_k, p, d in out[:limit]]


# ----------------------------------------------------------------------
#  Biblioteka
# ----------------------------------------------------------------------
class Library:
    """Otwarta biblioteka punktów. Użycie: `with Library.open(path) as lib: ...`."""

    def __init__(self, ds, path):
        self.ds = ds
        self.path = path

    # --- otwieranie / zakładanie ---------------------------------------
    @classmethod
    def create(cls, path, crs="EPSG:2180"):
        if os.path.exists(path):
            raise LibraryError(f"file exists: {path}")
        drv = ogr.GetDriverByName("GPKG")
        ds = drv.CreateDataSource(path)
        if ds is None:
            raise LibraryError(f"cannot create {path}: {gdal.GetLastErrorMsg()}")
        lyr = ds.CreateLayer(_POINTS, _srs(crs), ogr.wkbPoint, ["FID=fid", "GEOMETRY_NAME=geom"])
        cls._add_fields(lyr, _POINTS)
        for name in (_SETS, _MEMBERS, _ANALYSES, _LINKS, _META):
            cls._add_fields(ds.CreateLayer(name, None, ogr.wkbNone, ["FID=fid"]), name)
        for sql in _INDEXES:
            ds.ExecuteSQL(sql)
        lib = cls(ds, path)
        lib._set_meta("schema_version", str(SCHEMA_VERSION))
        lib._set_meta("created", _now())
        return lib

    @classmethod
    def open(cls, path):
        if not os.path.isfile(path):
            raise LibraryError(f"no such file: {path}")
        try:
            ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE, allowed_drivers=["GPKG"])
        except RuntimeError:
            ds = None
        if ds is None:
            raise NotALibrary(path)
        if ds.GetLayerByName(_META) is None or ds.GetLayerByName(_POINTS) is None:
            raise NotALibrary(path)
        lib = cls(ds, path)
        lib._migrate()
        return lib

    @staticmethod
    def _add_fields(lyr, table):
        for name, typ in _SCHEMA[table]:
            fd = ogr.FieldDefn(name, typ)
            if name in ("uid", "key") and table in (_POINTS, _MEMBERS, _META):
                fd.SetNullable(False)
            _ok(lyr.CreateField(fd), f"{table}.{name}")

    def _migrate(self):
        version = int(self._get_meta("schema_version") or 0)
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(version)
        while version < SCHEMA_VERSION:
            with self._tx():
                _MIGRATIONS[version](self.ds)
                version += 1
                self._set_meta("schema_version", str(version))

    def close(self):
        self.ds = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def _tx(self):
        self.ds.StartTransaction()
        try:
            yield
        except BaseException:
            self.ds.RollbackTransaction()
            raise
        self.ds.CommitTransaction()

    def _layer(self, name):
        lyr = self.ds.GetLayerByName(name)
        lyr.SetAttributeFilter(None)
        lyr.ResetReading()
        return lyr

    def _features(self, name, where=None):
        lyr = self._layer(name)
        if where:
            lyr.SetAttributeFilter(where)
        feats = list(lyr)
        lyr.SetAttributeFilter(None)
        return feats

    def _delete_where(self, name, where):
        lyr = self._layer(name)
        for fid in [f.GetFID() for f in self._features(name, where)]:
            _ok(lyr.DeleteFeature(fid), f"delete {name}")

    # --- metadane --------------------------------------------------------
    def _get_meta(self, key):
        feats = self._features(_META, f"key = {_q(key)}")
        return feats[0].GetField("value") if feats else None

    def _set_meta(self, key, value):
        lyr = self._layer(_META)
        feats = self._features(_META, f"key = {_q(key)}")
        feat = feats[0] if feats else ogr.Feature(lyr.GetLayerDefn())
        feat.SetField("key", key)
        feat.SetField("value", value)
        _ok(lyr.SetFeature(feat) if feats else lyr.CreateFeature(feat), "qma_meta")

    @property
    def schema_version(self):
        return int(self._get_meta("schema_version") or 0)

    @property
    def crs_wkt(self):
        return self._layer(_POINTS).GetSpatialRef().ExportToWkt()

    @property
    def crs_authid(self):
        srs = self._layer(_POINTS).GetSpatialRef()
        name, code = srs.GetAuthorityName(None), srs.GetAuthorityCode(None)
        return f"{name}:{code}" if name and code else ""

    # --- punkty ------------------------------------------------------------
    def _point_from(self, feat):
        g = feat.GetGeometryRef()
        kw = {f: _get(feat, f) for f in _POINT_FIELDS}
        for k in ("name", "alt_names", "type", "feature", "source", "note", "uid"):
            kw[k] = kw[k] or ""
        return RefPoint(x=g.GetX() if g else None, y=g.GetY() if g else None, **kw)

    def _fill_point(self, feat, p):
        for f in _POINT_FIELDS:
            v = getattr(p, f)
            _set(feat, f, v if v != "" or f == "name" else None)
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint_2D(float(p.x), float(p.y))
        feat.SetGeometry(g)

    def add_point(self, p):
        """Dodaje punkt; nadaje uid (UUID), gdy pusty. Zwraca uid."""
        if not p.uid:
            p.uid = str(uuid.uuid4())
        elif self._features(_POINTS, f"uid = {_q(p.uid)}"):
            raise LibraryError(f"duplicate uid: {p.uid}")
        lyr = self._layer(_POINTS)
        feat = ogr.Feature(lyr.GetLayerDefn())
        self._fill_point(feat, p)
        _ok(lyr.CreateFeature(feat), "add point")
        return p.uid

    def add_points(self, points):
        with self._tx():
            return [self.add_point(p) for p in points]

    def update_point(self, p):
        feats = self._features(_POINTS, f"uid = {_q(p.uid)}")
        if not feats:
            raise LibraryError(f"no point {p.uid}")
        self._fill_point(feats[0], p)
        _ok(self._layer(_POINTS).SetFeature(feats[0]), "update point")

    def delete_point(self, uid, force=False):
        """Usuwa punkt i jego przynależność do zestawów. Gdy jest użyty w analizach – PointInUse,
        chyba że force=True (powiązania zostają jako punkty spoza biblioteki, uid = NULL)."""
        used = sorted({f.GetField("analysis_id") for f in self._features(_LINKS, f"uid = {_q(uid)}")})
        if used and not force:
            raise PointInUse(uid, used)
        with self._tx():
            links = self._layer(_LINKS)
            for f in self._features(_LINKS, f"uid = {_q(uid)}"):
                f.SetFieldNull("uid")
                _ok(links.SetFeature(f), "unlink")
            self._delete_where(_MEMBERS, f"uid = {_q(uid)}")
            self._delete_where(_POINTS, f"uid = {_q(uid)}")

    def get_point(self, uid):
        feats = self._features(_POINTS, f"uid = {_q(uid)}")
        return self._point_from(feats[0]) if feats else None

    def points(self, set_id=None):
        pts = [self._point_from(f) for f in self._features(_POINTS)]
        if set_id is not None:
            members = set(self.set_members(set_id))
            pts = [p for p in pts if p.uid in members]
        return pts

    def search(self, text, near=None, set_id=None, limit=20, max_dist=None):
        """Punkty pasujące do nazwy lub nazw dawnych (bez znaków diakrytycznych i wielkości liter).
        near=(x, y) w układzie biblioteki – sortowanie także po odległości (ta sama nazwa w wielu miejscach).
        Zwraca listę (RefPoint, odległość lub None)."""
        return search_points(self.points(set_id), text, near, limit, max_dist)

    # --- zestawy --------------------------------------------------------------
    def add_set(self, name, description="", uids=()):
        with self._tx():
            lyr = self._layer(_SETS)
            feat = ogr.Feature(lyr.GetLayerDefn())
            feat.SetField("name", name)
            _set(feat, "description", description or None)
            _ok(lyr.CreateFeature(feat), "add set")
            set_id = feat.GetFID()
            self._add_members(set_id, uids)
        return set_id

    def update_set(self, set_id, name=None, description=None):
        lyr = self._layer(_SETS)
        feat = lyr.GetFeature(set_id)
        if feat is None:
            raise LibraryError(f"no set {set_id}")
        if name is not None:
            feat.SetField("name", name)
        if description is not None:
            _set(feat, "description", description or None)
        _ok(lyr.SetFeature(feat), "update set")

    def delete_set(self, set_id):
        """Usuwa zestaw (punkty zostają w bibliotece)."""
        with self._tx():
            self._delete_where(_MEMBERS, f"set_id = {int(set_id)}")
            _ok(self._layer(_SETS).DeleteFeature(int(set_id)), "delete set")

    def sets(self):
        """Lista (set_id, nazwa, opis, liczba punktów)."""
        counts = {}
        for f in self._features(_MEMBERS):
            counts[f.GetField("set_id")] = counts.get(f.GetField("set_id"), 0) + 1
        out = [(f.GetFID(), f.GetField("name") or "", _get(f, "description") or "", counts.get(f.GetFID(), 0))
               for f in self._features(_SETS)]
        return sorted(out, key=lambda s: normalize(s[1]))

    def set_members(self, set_id):
        return [f.GetField("uid") for f in self._features(_MEMBERS, f"set_id = {int(set_id)}")]

    def _add_members(self, set_id, uids):
        existing = set(self.set_members(set_id))
        lyr = self._layer(_MEMBERS)
        for uid in dict.fromkeys(uids):
            if uid in existing:
                continue
            if self.get_point(uid) is None:
                raise LibraryError(f"no point {uid}")
            feat = ogr.Feature(lyr.GetLayerDefn())
            feat.SetField("set_id", int(set_id))
            feat.SetField("uid", uid)
            _ok(lyr.CreateFeature(feat), "add member")

    def add_to_set(self, set_id, uids):
        with self._tx():
            self._add_members(set_id, uids)

    def remove_from_set(self, set_id, uids):
        with self._tx():
            for uid in uids:
                self._delete_where(_MEMBERS, f"set_id = {int(set_id)} AND uid = {_q(uid)}")

    def point_sets_of(self, uid):
        return sorted(f.GetField("set_id") for f in self._features(_MEMBERS, f"uid = {_q(uid)}"))

    # --- analizy ----------------------------------------------------------------
    def _analysis_from(self, feat):
        kw = {f: _get(feat, f) for f in _ANALYSIS_FIELDS}
        for k in ("title", "author", "sheet", "scan_path", "scan_sha256", "ref_crs", "note", "created", "modified"):
            kw[k] = kw[k] or ""
        return Analysis(analysis_id=feat.GetFID(), **kw)

    def save_analysis(self, a):
        """Zapisuje (nową lub istniejącą) analizę. Zwraca analysis_id."""
        lyr = self._layer(_ANALYSES)
        feat = lyr.GetFeature(a.analysis_id) if a.analysis_id is not None else None
        new = feat is None
        if new:
            feat = ogr.Feature(lyr.GetLayerDefn())
            a.created = a.created or _now()
        a.modified = _now()
        for f in _ANALYSIS_FIELDS:
            v = getattr(a, f)
            _set(feat, f, None if v == "" else v)
        _ok(lyr.CreateFeature(feat) if new else lyr.SetFeature(feat), "save analysis")
        a.analysis_id = feat.GetFID()
        return a.analysis_id

    def get_analysis(self, analysis_id):
        feat = self._layer(_ANALYSES).GetFeature(int(analysis_id))
        return self._analysis_from(feat) if feat is not None else None

    def analyses(self):
        return [self._analysis_from(f) for f in self._features(_ANALYSES)]

    def find_analyses(self, scan_sha256=None, scan_path=None):
        """Analizy tego samego skanu – po sumie kontrolnej, a gdy jej brak – po ścieżce."""
        if scan_sha256:
            return [self._analysis_from(f) for f in self._features(_ANALYSES, f"scan_sha256 = {_q(scan_sha256)}")]
        if scan_path:
            norm = os.path.normcase(os.path.abspath(scan_path))
            return [a for a in self.analyses() if a.scan_path and os.path.normcase(os.path.abspath(a.scan_path)) == norm]
        return []

    def delete_analysis(self, analysis_id):
        with self._tx():
            self._delete_where(_LINKS, f"analysis_id = {int(analysis_id)}")
            _ok(self._layer(_ANALYSES).DeleteFeature(int(analysis_id)), "delete analysis")

    # --- punkty GCP analizy ----------------------------------------------------------
    @staticmethod
    def _validate_links(links):
        seen = set()
        for k, ln in enumerate(links):
            if ln.status not in STATUSES:
                raise LibraryError(f"link {k}: unknown status {ln.status!r}")
            if ln.uid:
                if ln.uid in seen:
                    raise LibraryError(f"link {k}: duplicate uid {ln.uid}")
                seen.add(ln.uid)
            if ln.status == "absent":
                if not ln.uid:
                    raise LibraryError(f"link {k}: status 'absent' requires a library point")
            elif ln.x_old is None or ln.y_old is None:
                raise LibraryError(f"link {k}: missing scan position")

    def links(self, analysis_id):
        feats = self._features(_LINKS, f"analysis_id = {int(analysis_id)}")
        feats.sort(key=lambda f: (_get(f, "seq") or 0, f.GetFID()))
        out = []
        for f in feats:
            kw = {k: _get(f, k) for k in _LINK_FIELDS}
            kw["enabled"] = bool(kw["enabled"]) if kw["enabled"] is not None else True
            kw["name"] = kw["name"] or ""
            kw["comment"] = kw["comment"] or ""
            kw["status"] = kw["status"] or "sure"
            if kw["status"] == "absent":
                kw["x_old"] = kw["y_old"] = None
            out.append(Link(**kw))
        return out

    def save_links(self, analysis_id, links):
        """Zastępuje wszystkie punkty GCP analizy (kolejność = numeracja punktów)."""
        if self.get_analysis(analysis_id) is None:
            raise LibraryError(f"no analysis {analysis_id}")
        self._validate_links(links)
        with self._tx():
            self._delete_where(_LINKS, f"analysis_id = {int(analysis_id)}")
            lyr = self._layer(_LINKS)
            for seq, ln in enumerate(links, 1):
                feat = ogr.Feature(lyr.GetLayerDefn())
                feat.SetField("analysis_id", int(analysis_id))
                feat.SetField("seq", seq)
                for k in _LINK_FIELDS:
                    v = getattr(ln, k)
                    if k == "enabled":
                        v = int(bool(v))
                    elif k in ("x_old", "y_old") and ln.status == "absent":
                        v = None
                    _set(feat, k, None if v == "" else v)
                _ok(lyr.CreateFeature(feat), "save link")
            a = self.get_analysis(analysis_id)
            self.save_analysis(a)  # znacznik modyfikacji

    def missing_from_set(self, analysis_id, set_id):
        """Punkty zestawu, których analiza jeszcze nie ma (w żadnym statusie)."""
        linked = {ln.uid for ln in self.links(analysis_id) if ln.uid}
        return [p for p in self.points(set_id) if p.uid not in linked]

    def changed_refs(self, analysis_id, to_analysis=None, tol=1e-3):
        """Punkty GCP, których współrzędne w bibliotece różnią się od kopii w analizie.
        to_analysis(x, y) -> (x, y): przeliczenie z układu biblioteki do układu analizy (domyślnie bez zmian).
        Zwraca listę (indeks, Link, RefPoint lub None gdy punkt usunięto, (x, y) lub None, przesunięcie)."""
        out = []
        for k, ln in enumerate(self.links(analysis_id)):
            if not ln.uid or ln.ref_x is None:
                continue
            p = self.get_point(ln.uid)
            if p is None:
                out.append((k, ln, None, None, None))
                continue
            xy = to_analysis(p.x, p.y) if to_analysis else (p.x, p.y)
            d = math.hypot(xy[0] - ln.ref_x, xy[1] - ln.ref_y)
            if d > tol:
                out.append((k, ln, p, xy, d))
        return out

    def usage(self, uid):
        """Analizy, w których występuje punkt: lista (analysis_id, status)."""
        return sorted((f.GetField("analysis_id"), _get(f, "status") or "sure")
                      for f in self._features(_LINKS, f"uid = {_q(uid)}"))


for _name, _attr in list(vars(Library).items()):
    if _name.startswith("_"):
        continue
    if isinstance(_attr, classmethod):
        setattr(Library, _name, classmethod(_guarded(_attr.__func__)))
    elif callable(_attr):
        setattr(Library, _name, _guarded(_attr))


def as_dict(obj):
    return {f.name: getattr(obj, f.name) for f in fields(obj)}
