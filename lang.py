"""
Język interfejsu wtyczki i formatowanie liczb.

Wtyczka ma dwa języki: polski (teksty źródłowe) i angielski – używany dla
każdego innego języka interfejsu QGIS. Moduł nie ma ciężkich zależności,
dzięki czemu wtyczka ładuje się nawet przy problemach z numpy/scipy.
"""

import math

_LANG = None


def detect_language() -> str:
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QLocale, QSettings
    loc = QgsApplication.locale() or ""
    if not loc:
        loc = QSettings().value("locale/userLocale", "") or QLocale().name()
    return "pl" if str(loc).lower().startswith("pl") else "en"


def ui_language() -> str:
    global _LANG
    if _LANG is None:
        _LANG = detect_language()
    return _LANG


def set_language(lang: str):
    global _LANG
    _LANG = "pl" if str(lang).lower().startswith("pl") else "en"


def decimal_sep() -> str:
    return "," if ui_language() == "pl" else "."


def num(value, digits: int = 2, group: bool = True) -> str:
    """Liczba sformatowana zgodnie z językiem (PL: przecinek dziesiętny, spacja tysięcy)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "–"
    if not math.isfinite(v):
        return "–"
    s = f"{v:,.{digits}f}" if group else f"{v:.{digits}f}"
    s = s.replace(",", " ")
    if decimal_sep() == ",":
        s = s.replace(".", ",")
    return s
