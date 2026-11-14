"""
Wykresy serii czasowej i wydawnictwa wieloarkuszowego jako SVG (bez QGIS i bez matplotlib).

Zasady (spójny wygląd raportów i slajdów): jasna powierzchnia, cienkie znaki, włoskowate siatki,
jedna oś na wykres, kolor wg roli – seria (niebieski), stan „odstający” (czerwony, zawsze z legendą),
skala rozbieżna niebieski ↔ czerwony z szarym środkiem. Tekst zawsze w kolorach tekstu, nie serii.
Każdy znak ma <title> – podpowiedź po najechaniu w przeglądarce.

Napisy przekazuje wywołujący (tłumaczenia) przez słownik `texts`; domyślne – po polsku.
"""

import math
import zlib
from html import escape

# --- paleta (jasna) ---------------------------------------------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SERIES = "#2a78d6"
SERIES_2 = "#eb6834"
CRITICAL = "#d03b3b"
DIV_NEG = "#2a78d6"
DIV_POS = "#e34948"
DIV_MID = "#f0efec"
FONT = "Segoe UI, system-ui, -apple-system, Arial, sans-serif"

TEXTS = {
    "accuracy_title": "Dokładność map w czasie",
    "accuracy_sub": "RMSE wyrównania (skala logarytmiczna); kreska – lata wydania",
    "year": "rok",
    "rmse": "RMSE",
    "points": "punkty",
    "errors_title": "Błędy położenia punktów na mapach",
    "errors_sub": "każdy punkt to obiekt zestawu; kreska – mediana (skala logarytmiczna)",
    "outlier": "odstający (test Baardy)",
    "median": "mediana",
    "similarity_title": "Podobieństwo błędów między mapami",
    "similarity_sub": "korelacja wektorowa r błędów punktów wspólnych; * – istotne (p < 0,05)",
    "similar": "podobne",
    "unrelated": "bez związku",
    "opposite": "przeciwne",
    "too_few": "za mało punktów wspólnych",
    "vectors_title": "Wektory błędów położenia",
    "vectors_sub": "kierunek i wielkość przesunięcia obiektów; powiększenie różne w każdym panelu – patrz podziałka",
    "error_of": "błędu",
    "sheets_title": "Arkusze wydawnictwa",
    "sheets_rmse": "RMSE arkusza",
    "sheets_scale": "Odchylenie skali od średniej",
    "index_title": "Skorowidz arkuszy",
    "index_sub": "zasięgi arkuszy na mapie współczesnej; punkty – zakładki (wskazania na dwóch arkuszach)",
    "overlap": "zakładka",
    "overlap_worst": "największa rozbieżność",
    "no_data": "brak danych",
}


def _t(texts, key):
    return (texts or {}).get(key, TEXTS[key])


def _f(v, nd=1):
    return f"{v:.{nd}f}"


def _svg(width, height, body, title):
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' width='{width}' height='{height}' "
            f"font-family='{FONT}' role='img' aria-label='{escape(title)}' style='max-width:100%;height:auto'>"
            f"<rect width='{width}' height='{height}' fill='{SURFACE}'/>{body}</svg>")


def _text(x, y, s, size=11, fill=INK2, anchor="start", weight="normal", extra=""):
    return (f"<text x='{_f(x)}' y='{_f(y)}' font-size='{size}' fill='{fill}' text-anchor='{anchor}' "
            f"font-weight='{weight}' {extra}>{escape(str(s))}</text>")


def _header(texts, title_key, sub_key, width):
    return (_text(16, 24, _t(texts, title_key), 15, INK, weight="600")
            + _text(16, 42, _t(texts, sub_key), 11, INK2))


def _wrap(label, width_chars=18, max_lines=3):
    words, lines, cur = str(label).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])[:width_chars - 1] + "…"]
    return lines


def metres_label(v):
    if v >= 1000:
        km = v / 1000
        return f"{km:g} km" if km >= 10 or km == int(km) else f"{km:.1f} km".replace(".", ",")
    return f"{v:g} m"


def _log_ticks(lo, hi):
    """Podziałka 1-2-5 w zakresie [lo, hi] (wartości dodatnie)."""
    ticks = []
    e = math.floor(math.log10(lo))
    while 10 ** e <= hi * 1.0001:
        for m in (1, 2, 5):
            v = m * 10 ** e
            if lo * 0.9999 <= v <= hi * 1.0001:
                ticks.append(v)
        e += 1
    if len(ticks) > 8:
        ticks = [t for t in ticks if str(int(t) if t >= 1 else t).lstrip("0.")[0] in "1"] or ticks
    return ticks


def _log_domain(values):
    vals = [v for v in values if v and v > 0 and math.isfinite(v)]
    if not vals:
        return 1.0, 10.0
    lo, hi = min(vals) * 0.85, max(vals) * 1.15
    steps = [m * 10 ** e for e in range(-3, 9) for m in (1, 2, 5)]
    return max([v for v in steps if v <= lo] or [steps[0]]), min([v for v in steps if v >= hi] or [steps[-1]])


def _nice_step(span, target=6):
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _empty(width, height, texts, title_key, sub_key):
    return _svg(width, height, _header(texts, title_key, sub_key, width)
                + _text(width / 2, height / 2, _t(texts, "no_data"), 12, MUTED, "middle"), _t(texts, title_key))


def _ylog_axis(x0, x1, y_of, ticks, unit_fmt=metres_label):
    out = []
    for t in ticks:
        y = y_of(t)
        out.append(f"<line x1='{_f(x0)}' x2='{_f(x1)}' y1='{_f(y)}' y2='{_f(y)}' stroke='{GRID}' stroke-width='1'/>")
        out.append(_text(x0 - 8, y + 4, unit_fmt(t), 10, MUTED, "end", extra="font-variant-numeric='tabular-nums'"))
    return "".join(out)


def _overlaps(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _place_label(x, y, lines, placed, dots, bars, bounds):
    """Pozycja podpisu punktu (nazwa + wartość) bez kolizji z innymi podpisami, kropkami i paskami lat."""
    w = 6.3 * max(len(s) for s in lines)
    h = 13 * (len(lines) + 1) + 2                       # nazwa (1–2 wiersze) i wartość
    near = ((10, -8, "start"), (10, 16, "start"), (-10, -8, "end"), (-10, 16, "end"),
            (10, -30, "start"), (10, 38, "start"), (-10, -30, "end"), (-10, 38, "end"))
    for dx, dy, anchor in near:
        lx, ly = x + dx, y + dy
        x0 = lx if anchor == "start" else lx - w
        box = (x0, ly - 11, x0 + w, ly - 11 + h)
        if box[0] < bounds[0] or box[2] > bounds[2] or box[1] < bounds[1] or box[3] > bounds[3]:
            continue
        if any(_overlaps(box, b) for b in placed):
            continue
        if any(_overlaps(box, (dx_ - 10, dy_ - 10, dx_ + 10, dy_ + 10)) for dx_, dy_ in dots):
            continue
        if any(_overlaps(box, (b0, by - 3, b1, by + 3)) for b0, b1, by in bars):
            continue
        placed.append(box)
        return lx, ly, anchor
    return None                                      # brak miejsca obok kropki – kolumna podpisów


def _year_axis(x_lo, x_hi, x_of, left, pw, y0, caption):
    out = []
    step = _nice_step(x_hi - x_lo)
    yr = math.ceil(x_lo / step) * step
    while yr <= x_hi:
        out.append(_text(x_of(yr), y0 + 18, f"{int(yr)}", 10, MUTED, "middle"))
        out.append(f"<line x1='{_f(x_of(yr))}' x2='{_f(x_of(yr))}' y1='{y0}' y2='{y0 + 4}' stroke='{AXIS}'/>")
        yr += step
    out.append(f"<line x1='{left}' x2='{left + pw}' y1='{y0}' y2='{y0}' stroke='{AXIS}' stroke-width='1'/>")
    out.append(_text(left + pw, y0 + 36, caption, 10, MUTED, "end"))
    return "".join(out)


# ---------------------------------------------------------------------------
#  1. Dokładność map w czasie
# ---------------------------------------------------------------------------
def accuracy_timeline(items, texts=None, width=760, height=380):
    """items: [{"label", "year_from", "year_to", "rmse_m", "n"}] – mapy z rokiem i RMSE."""
    items = [i for i in items if i.get("year_from") and i.get("rmse_m") and math.isfinite(i["rmse_m"]) and i["rmse_m"] > 0]
    if not items:
        return _empty(width, height, texts, "accuracy_title", "accuracy_sub")
    left, right, top, bottom = 70, 170, 64, 44
    years = [i["year_from"] for i in items] + [i.get("year_to") or i["year_from"] for i in items]
    span = max(years) - min(years)
    pad = max(5.0, span * 0.06)
    x_lo, x_hi = min(years) - pad, max(years) + pad
    lo, hi = _log_domain([i["rmse_m"] for i in items])
    pw, ph = width - left - right, height - top - bottom

    def x_of(year):
        return left + (year - x_lo) / (x_hi - x_lo) * pw

    def y_of(v):
        return top + ph - (math.log10(v) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * ph

    out = [_header(texts, "accuracy_title", "accuracy_sub", width), _ylog_axis(left, left + pw, y_of, _log_ticks(lo, hi))]
    out.append(_year_axis(x_lo, x_hi, x_of, left, pw, top + ph, _t(texts, "year")))
    dots = [(x_of(i["year_from"]), y_of(i["rmse_m"])) for i in items]
    bars = [(x_of(i["year_from"]), x_of(i["year_to"]), y_of(i["rmse_m"])) for i in items
            if i.get("year_to") and i["year_to"] > i["year_from"]]
    placed, column = [], []
    for i in sorted(items, key=lambda i: i["year_from"]):
        x, y = x_of(i["year_from"]), y_of(i["rmse_m"])
        tip = f"{i['label']}: {_t(texts, 'rmse')} {metres_label(round(i['rmse_m']))}, {_t(texts, 'points')}: {i.get('n', '')}"
        y_to = i.get("year_to")
        if y_to and y_to > i["year_from"]:
            out.append(f"<line x1='{_f(x)}' x2='{_f(x_of(y_to))}' y1='{_f(y)}' y2='{_f(y)}' stroke='{SERIES}' "
                       f"stroke-opacity='0.35' stroke-width='4' stroke-linecap='round'><title>{escape(tip)}</title></line>")
        out.append(f"<circle cx='{_f(x)}' cy='{_f(y)}' r='5.5' fill='{SERIES}' stroke='{SURFACE}' stroke-width='2'>"
                   f"<title>{escape(tip)}</title></circle>")
        lines = _wrap(i["label"], 22, 2)
        spot = _place_label(x, y, lines, placed, dots, bars, (left + 4, top - 20, left + pw - 4, top + ph - 2))
        if spot is None:
            column.append((y, x, lines, i))
            continue
        lx, ly, anchor = spot
        for k, s in enumerate(lines):
            out.append(_text(lx, ly + 13 * k, s, 11, INK2, anchor))
        out.append(_text(lx, ly + 13 * len(lines), metres_label(round(i["rmse_m"])), 10, MUTED, anchor))
    # kolumna podpisów na prawym marginesie (gęste skupiska), z liniami odniesienia
    cx = left + pw + 14
    prev = top - 30
    for y, x, lines, i in sorted(column, key=lambda c: c[0]):
        h = 13 * (len(lines) + 1) + 6
        ly = max(y, prev + 4) if prev > top - 30 else y
        ly = max(ly, prev + 4)
        prev = ly + h - 12
        out.append(f"<polyline points='{_f(x + 7)},{_f(y)} {_f(cx - 10)},{_f(ly - 4)} {_f(cx - 4)},{_f(ly - 4)}' "
                   f"fill='none' stroke='{AXIS}' stroke-width='1'/>")
        for k, s in enumerate(lines):
            out.append(_text(cx, ly + 13 * k, s, 11, INK2))
        out.append(_text(cx, ly + 13 * len(lines), metres_label(round(i["rmse_m"])), 10, MUTED))
    return _svg(width, height, "".join(out), _t(texts, "accuracy_title"))


# ---------------------------------------------------------------------------
#  2. Błędy punktów na mapach
# ---------------------------------------------------------------------------
def _jitter(key, spread):
    return ((zlib.crc32(str(key).encode()) % 1000) / 1000.0 - 0.5) * spread


def error_strip(columns, texts=None, width=760, height=400):
    """columns: [{"label", "errors": [(nazwa, metry, odstający)]}] – kolumny w kolejności map."""
    columns = [c for c in columns if c["errors"]]
    if not columns:
        return _empty(width, height, texts, "errors_title", "errors_sub")
    left, right, top, bottom = 70, 24, 70, 70
    lo, hi = _log_domain([e for c in columns for _n, e, _o in c["errors"]])
    pw, ph = width - left - right, height - top - bottom
    band = pw / len(columns)

    def y_of(v):
        v = max(v, lo)
        return top + ph - (math.log10(v) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * ph

    out = [_header(texts, "errors_title", "errors_sub", width), _ylog_axis(left, left + pw, y_of, _log_ticks(lo, hi))]
    any_outlier = False
    for k, c in enumerate(columns):
        cx = left + band * (k + 0.5)
        errs = sorted(e for _n, e, _o in c["errors"])
        med = errs[len(errs) // 2] if len(errs) % 2 else (errs[len(errs) // 2 - 1] + errs[len(errs) // 2]) / 2
        for name, e, outlier in c["errors"]:
            any_outlier |= bool(outlier)
            x = cx + _jitter(name, min(band * 0.55, 60))
            fill = CRITICAL if outlier else SERIES
            out.append(f"<circle cx='{_f(x)}' cy='{_f(y_of(e))}' r='4' fill='{fill}' fill-opacity='{1 if outlier else 0.75}' "
                       f"stroke='{SURFACE}' stroke-width='1.5'><title>{escape(f'{name}: {metres_label(round(e))}')}</title></circle>")
            if outlier:
                out.append(_text(x + 7, y_of(e) + 4, name, 10, INK2))
        hw = min(band * 0.35, 40)
        out.append(f"<line x1='{_f(cx - hw)}' x2='{_f(cx + hw)}' y1='{_f(y_of(med))}' y2='{_f(y_of(med))}' stroke='{INK}' "
                   f"stroke-width='2' stroke-linecap='round'><title>{escape(_t(texts, 'median'))}: "
                   f"{escape(metres_label(round(med)))}</title></line>")
        for j, s in enumerate(_wrap(c["label"], max(8, int(band / 6.6)), 3)):
            out.append(_text(cx, top + ph + 18 + 13 * j, s, 10, INK2, "middle"))
    out.append(f"<line x1='{left}' x2='{left + pw}' y1='{top + ph}' y2='{top + ph}' stroke='{AXIS}'/>")
    lx = width - right
    legend = [(f"<line x1='{lx - 128}' x2='{lx - 112}' y1='56' y2='56' stroke='{INK}' stroke-width='2'/>"
               + _text(lx - 106, 60, _t(texts, "median"), 10, INK2))]
    if any_outlier:
        legend.append(f"<circle cx='{lx - 238}' cy='56' r='4' fill='{CRITICAL}'/>"
                      + _text(lx - 230, 60, _t(texts, "outlier"), 10, INK2))
    out += legend
    return _svg(width, height, "".join(out), _t(texts, "errors_title"))


# ---------------------------------------------------------------------------
#  3. Macierz podobieństwa reszt
# ---------------------------------------------------------------------------
def _mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def diverging(r):
    if r is None or not math.isfinite(r):
        return DIV_MID
    r = max(-1.0, min(1.0, r))
    return _mix(DIV_MID, DIV_POS if r > 0 else DIV_NEG, abs(r))


def _ink_on(color):
    rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lum = sum(w * (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4) for w, c in zip((0.2126, 0.7152, 0.0722), rgb))
    return "#ffffff" if lum < 0.4 else INK


def similarity_matrix(labels, pairs, texts=None, cell=74):
    """labels: etykiety map (kolejność); pairs: {(i, j): (r, p, n, podobne)} dla i < j."""
    n = len(labels)
    if n < 2:
        return _empty(560, 220, texts, "similarity_title", "similarity_sub")
    left, top = 170, 70 + 48
    width = max(620, left + cell * (n - 1) + 40)
    height = top + cell * (n - 1) + 90
    out = [_header(texts, "similarity_title", "similarity_sub", width)]
    for j in range(1, n):
        for k, s in enumerate(_wrap(labels[j], 12, 3)):
            out.append(_text(left + cell * (j - 1) + cell / 2, top - 40 + 12 * k, s, 10, INK2, "middle"))
    for i in range(n - 1):
        for k, s in enumerate(_wrap(labels[i], 24, 2)):
            out.append(_text(left - 10, top + cell * i + cell / 2 - 2 + 12 * k, s, 10, INK2, "end"))
        for j in range(i + 1, n):
            x, y = left + cell * (j - 1), top + cell * i
            r, p, cnt, similar = pairs.get((i, j), (None, None, 0, False))
            fill = diverging(r)
            tip = (f"{labels[i]} – {labels[j]}: r = {r:.2f}, p = {p:.3f}, n = {cnt}" if r is not None and math.isfinite(r)
                   else f"{labels[i]} – {labels[j]}: {_t(texts, 'too_few')} (n = {cnt})")
            out.append(f"<rect x='{_f(x + 1)}' y='{_f(y + 1)}' width='{cell - 2}' height='{cell - 2}' rx='4' fill='{fill}'>"
                       f"<title>{escape(tip)}</title></rect>")
            if r is not None and math.isfinite(r):
                out.append(_text(x + cell / 2, y + cell / 2 + 2, f"{r:.2f}".replace(".", ",") + (" *" if similar else ""),
                                 13 if similar else 12, _ink_on(fill), "middle", "700" if similar else "normal"))
                out.append(_text(x + cell / 2, y + cell / 2 + 18, f"n = {cnt}", 9, _ink_on(fill), "middle"))
            else:
                out.append(_text(x + cell / 2, y + cell / 2 + 4, "–", 12, MUTED, "middle"))
    # legenda skali rozbieżnej
    lw = 330
    lx, ly = min(left, width - lw - 24), height - 44
    for s in range(40):
        r = -1 + 2 * s / 39
        out.append(f"<rect x='{_f(lx + lw * s / 40)}' y='{ly}' width='{_f(lw / 40 + 0.5)}' height='10' fill='{diverging(r)}'/>")
    out.append(_text(lx, ly + 24, f"−1 {_t(texts, 'opposite')}", 9, MUTED))
    out.append(_text(lx + lw / 2, ly + 24, f"0 {_t(texts, 'unrelated')}", 9, MUTED, "middle"))
    out.append(_text(lx + lw, ly + 24, f"{_t(texts, 'similar')} 1", 9, MUTED, "end"))
    return _svg(width, height, "".join(out), _t(texts, "similarity_title"))


# ---------------------------------------------------------------------------
#  4. Wektory błędów – małe mapki
# ---------------------------------------------------------------------------
def _nice_length(max_len):
    """„Okrągła” długość podziałki ≤ max_len (1-2-5)."""
    if max_len <= 0:
        return 0
    e = math.floor(math.log10(max_len))
    for m in (5, 2, 1):
        if m * 10 ** e <= max_len:
            return m * 10 ** e
    return 10 ** e


def _arrow(x0, y0, x1, y1, color, width=1.6, tip=""):
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    out = f"<line x1='{_f(x0)}' y1='{_f(y0)}' x2='{_f(x1)}' y2='{_f(y1)}' stroke='{color}' stroke-width='{width}' stroke-linecap='round'>"
    out += f"<title>{escape(tip)}</title></line>" if tip else "</line>"
    if length > 4:
        ux, uy = dx / length, dy / length
        h = min(6.0, length * 0.45)
        p1 = (x1 - h * ux + h * 0.5 * uy, y1 - h * uy - h * 0.5 * ux)
        p2 = (x1 - h * ux - h * 0.5 * uy, y1 - h * uy + h * 0.5 * ux)
        out += f"<path d='M{_f(x1)},{_f(y1)} L{_f(p1[0])},{_f(p1[1])} L{_f(p2[0])},{_f(p2[1])} Z' fill='{color}'/>"
    return out


def vector_panels(panels, unit_m=1.0, texts=None, columns=3, panel_w=250, panel_h=230):
    """panels: [{"label", "sub", "points": [(nazwa, x, y, dx, dy, metry, odstający)]}] – współrzędne w układzie
    biblioteki (wspólny zasięg wszystkich paneli), wektory w jego jednostkach; unit_m – metry na jednostkę."""
    panels = [p for p in panels if p["points"]]
    if not panels:
        return _empty(760, 220, texts, "vectors_title", "vectors_sub")
    cols = min(columns, len(panels))
    rows = math.ceil(len(panels) / cols)
    gap, top = 14, 60
    width = 16 + cols * panel_w + (cols - 1) * gap + 16
    height = top + rows * (panel_h + gap) + 8
    xs = [x for p in panels for _n, x, _y, *_r in p["points"]]
    ys = [y for p in panels for _n, _x, y, *_r in p["points"]]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    span = max(x1 - x0, y1 - y0) or 1.0
    inner_t, inner_b, inner = 46, 40, 36           # margines na wektory przy brzegu
    sw, sh = panel_w - 2 * inner, panel_h - inner_t - inner_b
    scale = min(sw / max(x1 - x0, span * 0.2), sh / max(y1 - y0, span * 0.2))
    out = [_header(texts, "vectors_title", "vectors_sub", width)]
    for k, p in enumerate(panels):
        px = 16 + (k % cols) * (panel_w + gap)
        py = top + (k // cols) * (panel_h + gap)
        ox = px + inner + (sw - (x1 - x0) * scale) / 2
        oy = py + inner_t + (sh - (y1 - y0) * scale) / 2

        def to_px(x, y, ox=ox, oy=oy):
            return ox + (x - x0) * scale, oy + (y1 - y) * scale

        out.append(f"<defs><clipPath id='qma-panel-{k}'><rect x='{px}' y='{py}' width='{panel_w}' height='{panel_h}' "
                   f"rx='6'/></clipPath></defs>")
        out.append(f"<rect x='{px}' y='{py}' width='{panel_w}' height='{panel_h}' rx='6' fill='none' stroke='{GRID}'/>")
        out.append(_text(px + 10, py + 18, _wrap(p["label"], 34, 1)[0], 11, INK, weight="600"))
        if p.get("sub"):
            out.append(_text(px + 10, py + 32, p["sub"], 9, MUTED))
        vmax = max(math.hypot(dx, dy) for _n, _x, _y, dx, dy, *_r in p["points"]) * scale or 1.0
        k_mag = (0.16 * sw) / vmax                               # powiększenie wektorów w panelu
        out.append(f"<g clip-path='url(#qma-panel-{k})'>")
        for name, x, y, dx, dy, err, outlier in p["points"]:
            ax, ay = to_px(x, y)
            out.append(f"<circle cx='{_f(ax)}' cy='{_f(ay)}' r='2.2' fill='{MUTED}'/>")
            bx, by = ax + dx * scale * k_mag, ay - dy * scale * k_mag
            out.append(_arrow(ax, ay, bx, by, CRITICAL if outlier else SERIES, 1.6,
                              f"{name}: {metres_label(round(err))}"))
        out.append("</g>")
        # podziałka błędu (długość w metrach)
        metres_per_px = unit_m / (scale * k_mag)
        ref_m = _nice_length(0.25 * sw * metres_per_px)
        ref_px = ref_m / metres_per_px
        sx, sy = px + 12, py + panel_h - 14
        out.append(_arrow(sx, sy, sx + ref_px, sy, INK2, 1.6))
        out.append(_text(sx + ref_px + 6, sy + 4, f"{metres_label(ref_m)} {_t(texts, 'error_of')}", 9, INK2))
    return _svg(width, height, "".join(out), _t(texts, "vectors_title"))


# ---------------------------------------------------------------------------
#  5. Arkusze wydawnictwa – RMSE i odchylenie skali
# ---------------------------------------------------------------------------
def sheet_bars(sheets, texts=None, width=760):
    """sheets: [{"label", "rmse_m", "scale_den"}] w kolejności arkuszy."""
    sheets = [s for s in sheets if s.get("rmse_m") is not None and math.isfinite(s["rmse_m"])]
    if not sheets:
        return _empty(width, 200, texts, "sheets_title", "sheets_rmse")
    row = 26
    top, label_w = 84, 110
    height = top + row * len(sheets) + 30
    half = (width - 48) / 2
    out = [_text(16, 24, _t(texts, "sheets_title"), 15, INK, weight="600"),
           _text(16, 60, _t(texts, "sheets_rmse"), 11, INK, weight="600"),
           _text(16 + half + 16, 60, _t(texts, "sheets_scale"), 11, INK, weight="600")]
    vmax = max(s["rmse_m"] for s in sheets) or 1.0
    bw = half - label_w - 70
    for k, s in enumerate(sheets):
        y = top + row * k
        out.append(_text(16 + label_w - 8, y + 13, s["label"], 10, INK2, "end"))
        w = max(2.0, bw * s["rmse_m"] / vmax)
        out.append(f"<path d='M{16 + label_w},{y + 3} h{_f(w - 4)} a4,4 0 0 1 4,4 v{row - 14} a4,4 0 0 1 -4,4 h{_f(-(w - 4))} Z' "
                   f"fill='{SERIES}'><title>{escape(s['label'])}: {escape(metres_label(round(s['rmse_m'])))}</title></path>")
        out.append(_text(16 + label_w + w + 6, y + 13, metres_label(round(s["rmse_m"])), 10, INK2))
    scales = [s["scale_den"] for s in sheets if s.get("scale_den") and math.isfinite(s["scale_den"])]
    x_base = 16 + half + 16
    if len(scales) >= 2:
        mean = sum(scales) / len(scales)
        devs = [100 * (s["scale_den"] - mean) / mean if s.get("scale_den") and math.isfinite(s["scale_den"]) else None
                for s in sheets]
        lim = max(1.0, max(abs(d) for d in devs if d is not None)) * 1.15
        cx0, cw = x_base + label_w, half - label_w - 20
        zero = cx0 + cw / 2
        out.append(f"<line x1='{_f(zero)}' x2='{_f(zero)}' y1='{top - 4}' y2='{top + row * len(sheets)}' stroke='{AXIS}'/>")
        out.append(_text(zero, top + row * len(sheets) + 16, "0 %", 9, MUTED, "middle"))
        out.append(_text(cx0, top + row * len(sheets) + 16, f"−{lim:.0f} %", 9, MUTED, "start"))
        out.append(_text(cx0 + cw, top + row * len(sheets) + 16, f"+{lim:.0f} %", 9, MUTED, "end"))
        for k, (s, d) in enumerate(zip(sheets, devs)):
            y = top + row * k + 9
            out.append(_text(x_base + label_w - 8, y + 4, s["label"], 10, INK2, "end"))
            out.append(f"<line x1='{cx0}' x2='{_f(cx0 + cw)}' y1='{_f(y)}' y2='{_f(y)}' stroke='{GRID}'/>")
            if d is not None:
                x = zero + d / lim * cw / 2
                out.append(f"<circle cx='{_f(x)}' cy='{_f(y)}' r='5' fill='{SERIES}' stroke='{SURFACE}' stroke-width='2'>"
                           f"<title>{escape(s['label'])}: 1 : {s['scale_den']:,.0f} ({d:+.1f} %)</title></circle>")
    else:
        out.append(_text(x_base, top + 14, _t(texts, "no_data"), 10, MUTED))
    return _svg(width, height, "".join(out), _t(texts, "sheets_title"))


# ---------------------------------------------------------------------------
#  6. Skorowidz arkuszy
# ---------------------------------------------------------------------------
def sheet_index(sheets, overlaps, texts=None, width=760, height=None):
    """sheets: [{"label", "footprint": [(x, y)…]}]; overlaps: [{"name", "x", "y", "diff_m"}] – układ biblioteki."""
    polys = [s for s in sheets if s.get("footprint")]
    if not polys:
        return _empty(width, 220, texts, "index_title", "index_sub")
    xs = [x for s in polys for x, _y in s["footprint"]] + [o["x"] for o in overlaps]
    ys = [y for s in polys for _x, y in s["footprint"]] + [o["y"] for o in overlaps]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    top, pad = 60, 20
    pw = width - 2 * pad
    if height is None:                               # wysokość z proporcji zasięgu (bez pustych pasów)
        ph = min(max(pw * ((y1 - y0) or 1) / ((x1 - x0) or 1), 160), 760)
        height = int(top + ph + pad + 24)
    ph = height - top - pad - 24
    scale = min(pw / ((x1 - x0) or 1), ph / ((y1 - y0) or 1))
    ox = pad + (pw - (x1 - x0) * scale) / 2
    oy = top + (ph - (y1 - y0) * scale) / 2

    def to_px(x, y):
        return ox + (x - x0) * scale, oy + (y1 - y) * scale

    out = [_header(texts, "index_title", "index_sub", width)]
    for s in polys:
        pts = [to_px(x, y) for x, y in s["footprint"]]
        d = "M" + " L".join(f"{_f(x)},{_f(y)}" for x, y in pts) + " Z"
        out.append(f"<path d='{d}' fill='{SERIES}' fill-opacity='0.08' stroke='{SERIES}' stroke-width='1.5'>"
                   f"<title>{escape(s['label'])}</title></path>")
        cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        out.append(_text(cx, cy + 5, s["label"], 13, INK2, "middle", "600"))
    worst = max((o["diff_m"] for o in overlaps if math.isfinite(o["diff_m"])), default=None)
    for o in sorted(overlaps, key=lambda o: o["diff_m"] if math.isfinite(o["diff_m"]) else 0):
        x, y = to_px(o["x"], o["y"])
        is_worst = worst is not None and o["diff_m"] == worst
        out.append(f"<circle cx='{_f(x)}' cy='{_f(y)}' r='{5 if is_worst else 4}' fill='{CRITICAL if is_worst else INK2}' "
                   f"stroke='{SURFACE}' stroke-width='2'><title>{escape(o['name'])}: {escape(metres_label(round(o['diff_m'])))}"
                   f"</title></circle>")
        if is_worst:
            out.append(_text(x + 8, y + 4, f"{o['name']} – {metres_label(round(o['diff_m']))}", 10, INK2))
    if overlaps:
        ly = height - 14
        out.append(f"<circle cx='24' cy='{ly - 4}' r='4' fill='{INK2}'/>" + _text(32, ly, _t(texts, "overlap"), 10, INK2))
        if worst is not None:
            out.append(f"<circle cx='120' cy='{ly - 4}' r='5' fill='{CRITICAL}'/>"
                       + _text(130, ly, _t(texts, "overlap_worst"), 10, INK2))
    return _svg(width, height, "".join(out), _t(texts, "index_title"))
