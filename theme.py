"""
Kolory tekstu dopasowane do rzeczywistego tła widżetu (motyw jasny, ciemny, szary).

Motywy QGIS ustawiają tło przez arkusz stylów, np. „Night Mapping” daje QTextBrowser
tło #535353 (średnia szarość), a „Blend of Gray” – #b2b2b2. Stałe palety „jasna/ciemna”
dają na takich tłach kontrast ok. 2,5 : 1, dlatego kolory semantyczne (akcent, ok,
ostrzeżenie, błąd) są tu rozjaśniane lub przyciemniane – z zachowaniem odcienia – aż
osiągną wymagany kontrast WCAG względem wszystkich podanych teł.

Moduł nie zależy od Qt (kolory jako napisy #rrggbb).
"""

import colorsys

TEXT_MIN = 7.0       # tekst podstawowy (WCAG AAA)
SEMANTIC_MIN = 4.5   # kolory semantyczne i tekst przygaszony (WCAG AA)
DISABLED_MIN = 3.0   # elementy wyłączone – celowo słabsze, ale czytelne

# odcienie bazowe (wariant dla jasnego tła); dla ciemnego tła są rozjaśniane
BASE = {"accent": "#2b579a", "good": "#1a7f37", "warn": "#9a6700", "bad": "#cf222e",
        "hypothesis": "#c80078", "off": "#969696"}


def _rgb(c):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in rgb)


def luminance(c):
    def lin(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in _rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_dark(bg):
    """Tło, na którym jasny tekst ma większy kontrast niż ciemny."""
    return contrast("#ffffff", bg) > contrast("#000000", bg)


def mix(a, b, t):
    """Kolor a przesunięty o ułamek t w stronę b."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def _min_contrast(fg, bgs):
    return min(contrast(fg, b) for b in bgs)


def ensure_contrast(fg, *bgs, ratio=SEMANTIC_MIN):
    """Zmienia jasność koloru fg (odcień i nasycenie bez zmian), aż kontrast względem
    każdego z teł bgs wyniesie co najmniej ratio. Zwraca najlepszy osiągalny wariant."""
    bgs = [b for b in bgs if b] or ["#ffffff"]
    if _min_contrast(fg, bgs) >= ratio:
        return fg
    h, l, s = colorsys.rgb_to_hls(*_rgb(fg))
    lighten = is_dark(bgs[0])
    best, best_c = fg, _min_contrast(fg, bgs)
    for direction in ((1, -1) if lighten else (-1, 1)):
        ll = l
        for _ in range(100):
            ll += 0.01 * direction
            if not 0.0 <= ll <= 1.0:
                break
            cand = _hex(colorsys.hls_to_rgb(h, ll, s))
            c = _min_contrast(cand, bgs)
            if c > best_c:
                best, best_c = cand, c
            if c >= ratio:
                return cand
    return best


def report_palette(bg, text, font_pt=9):
    """Paleta raportu HTML dla tła bg i domyślnego koloru tekstu widżetu."""
    text = ensure_contrast(text, bg, ratio=TEXT_MIN)
    head = mix(bg, text, 0.12)
    text = ensure_contrast(text, bg, head, ratio=TEXT_MIN)
    pal = {
        "font_pt": font_pt,
        "text": text,
        "head": head,
        "border": mix(bg, text, 0.30),
        "muted": ensure_contrast(mix(text, bg, 0.35), bg, ratio=SEMANTIC_MIN),
    }
    for k in ("accent", "good", "warn", "bad"):
        pal[k] = ensure_contrast(BASE[k], bg, ratio=SEMANTIC_MIN)
    return pal


def foreground(name_or_hex, *bgs, ratio=None):
    """Kolor semantyczny (klucz z BASE lub #rrggbb) czytelny na podanych tłach."""
    c = BASE.get(name_or_hex, name_or_hex)
    if ratio is None:
        ratio = DISABLED_MIN if name_or_hex == "off" else SEMANTIC_MIN
    return ensure_contrast(c, *bgs, ratio=ratio)
