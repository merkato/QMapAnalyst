"""
Generowanie raportów HTML (panel wtyczki i eksport PDF).
"""

import math
from html import escape

import numpy as np
from qgis.PyQt.QtCore import QCoreApplication

from .math_engine import MODELS, OUTLIER_THRESHOLD, scale_denominator
from .projections import PARAM_LABELS, format_dms, prime_meridian_variants


def tr(text):
    return QCoreApplication.translate("QMapAnalystReport", text)


LIGHT = {
    "font_pt": 9,
    "text": "#1f2328", "muted": "#59636e", "accent": "#2b579a", "border": "#d0d7de",
    "head": "#eef2f7", "good": "#1a7f37", "warn": "#9a6700", "bad": "#cf222e",
}


def message_text(msg) -> str:
    """Komunikat estymatora: (szablon, parametry) -> przetłumaczony tekst."""
    if isinstance(msg, str):
        return tr(msg)
    template, params = msg
    params = {k: (", ".join(tr(x) for x in v) if isinstance(v, list) else v) for k, v in params.items()}
    return tr(template).format(**params)


def palette_colors(widget):
    """Kolory raportu dopasowane do motywu QGIS (jasny / ciemny)."""
    pal = widget.palette()
    text = pal.color(pal.ColorRole.Text)
    base = pal.color(pal.ColorRole.Base)
    dark = base.lightness() < 128
    if not dark:
        return dict(LIGHT)
    return {
        "text": text.name(), "muted": "#9aa4ae", "accent": "#6cb6ff", "border": "#444c56",
        "head": "#2d333b", "good": "#57ab5a", "warn": "#c69026", "bad": "#f47067",
    }


def fmt(v, digits=2, unit=""):
    if v is None or not np.isfinite(v):
        return "–"
    s = f"{v:,.{digits}f}".replace(",", " ")
    return f"{s} {unit}" if unit else s


def fmt_pm(v, s, digits=3, unit=""):
    if not np.isfinite(v):
        return "–"
    base = fmt(v, digits)
    if s is not None and np.isfinite(s):
        base += f" ± {fmt(s, digits)}"
    return f"{base} {unit}" if unit else base


def fmt_scale(m):
    if not np.isfinite(m) or m <= 0:
        return "–"
    mag = 10 ** max(0, int(math.floor(math.log10(m))) - 2)
    return "1 : " + f"{round(m / mag) * mag:,.0f}".replace(",", " ")


def _css(c):
    return f"""
    <style>
      body {{ color:{c['text']}; font-size:{c.get('font_pt', 9)}pt; }}
      h2 {{ color:{c['accent']}; font-size:12pt; margin:2px 0 4px 0; }}
      h3 {{ color:{c['accent']}; font-size:10pt; margin:10px 0 3px 0; border-bottom:1px solid {c['border']}; }}
      table {{ border-collapse:collapse; margin:2px 0; }}
      td, th {{ padding:2px 6px; border-bottom:1px solid {c['border']}; vertical-align:top; }}
      th {{ background:{c['head']}; text-align:left; font-weight:600; }}
      td.k {{ color:{c['muted']}; }}
      td.n {{ text-align:right; white-space:nowrap; }}
      .muted {{ color:{c['muted']}; }}
      .good {{ color:{c['good']}; font-weight:600; }}
      .warn {{ color:{c['warn']}; font-weight:600; }}
      .bad {{ color:{c['bad']}; font-weight:600; }}
      .note {{ color:{c['muted']}; font-size:8pt; }}
    </style>"""


def _kv(rows):
    out = ["<table width='100%'>"]
    for k, v in rows:
        out.append(f"<tr><td class='k'>{k}</td><td class='n'>{v}</td></tr>")
    out.append("</table>")
    return "".join(out)


def _quality_badge(sigma_mm):
    """Ocena dokładności graficznej (błąd średni na papierze)."""
    if not np.isfinite(sigma_mm):
        return ""
    if sigma_mm <= 0.5:
        return f"<span class='good'>{tr('wysoka dokładność graficzna')}</span>"
    if sigma_mm <= 1.5:
        return f"<span class='warn'>{tr('umiarkowana dokładność graficzna')}</span>"
    return f"<span class='bad'>{tr('niska dokładność graficzna')}</span>"


def analysis_html(ctx: dict, colors: dict) -> str:
    """ctx: fit, comparison, distortion, moran, n_total, crs_label, dpi, labels (id punktów)."""
    fit = ctx["fit"]
    st = fit.stats
    d = fit.derived
    dpi = ctx["dpi"]
    labels = ctx["labels"]
    unit = ctx.get("unit", "m")
    is_helmert = fit.model == "helmert"

    mpu = ctx.get("m_per_unit", 1.0)
    M = scale_denominator(d["scale"] * mpu, dpi)
    sigma_mm = st["sigma0"] / M * 1000.0 if np.isfinite(M) and M > 0 else float("nan")
    rmse_mm = st["rmse"] / M * 1000.0 if np.isfinite(M) and M > 0 else float("nan")
    loo_rmse = float(np.sqrt(np.nanmean(fit.loo_errors ** 2))) if np.any(np.isfinite(fit.loo_errors)) else float("nan")

    h = [_css(colors)]
    h.append(f"<h2>{escape(tr(MODELS[fit.model]['name']))}</h2>")
    h.append(f"<div class='muted'>{tr('Aktywne punkty')}: <b>{st['n']}</b> / {ctx['n_total']} · "
             f"{tr('układ odniesienia')}: <b>{escape(ctx['crs_label'])}</b> · {_quality_badge(sigma_mm)}</div>")

    # --- dokładność
    h.append(f"<h3>{tr('Dokładność dopasowania')}</h3>")
    rows = [
        (tr("Liczba obserwacji nadliczbowych (r)"), f"{st['redundancy']}"),
        (tr("Błąd średni jednostkowy σ₀"), fmt(st["sigma0"], 2, unit)),
        (tr("Średni błąd położenia punktu m<sub>p</sub> = σ₀·√2"), fmt(st["mean_position_error"], 2, unit)),
        (tr("RMSE całkowity / X / Y"), f"{fmt(st['rmse'], 2)} / {fmt(st['rmse_x'], 2)} / {fmt(st['rmse_y'], 2)} {unit}"),
        (tr("Błąd średni / mediana / odch. std."), f"{fmt(st['mean'], 2)} / {fmt(st['median'], 2)} / {fmt(st['std'], 2)} {unit}"),
        (tr("Błąd min / max"), f"{fmt(st['min'], 2)} / {fmt(st['max'], 2)} {unit} "
                                f"<span class='muted'>(#{labels[st['max_index']]})</span>"),
        (tr("CE90 / CE95 (percentyle błędu)"), f"{fmt(st['p90'], 2)} / {fmt(st['p95'], 2)} {unit}"),
        (tr("RMSE walidacji krzyżowej (LOO)"), fmt(loo_rmse, 2, unit)),
        (tr("σ₀ / RMSE na papierze") + f" ({dpi:g} DPI)", f"{fmt(sigma_mm, 2)} / {fmt(rmse_mm, 2)} mm"),
    ]
    h.append(_kv(rows))

    # --- parametry
    h.append(f"<h3>{tr('Parametry transformacji')} <span class='note'>({tr('wartość ± błąd średni')})</span></h3>")
    rows = []
    if is_helmert:
        rows.append((tr("Skala pikselowa"), fmt_pm(d["scale"], d["scale_std"], 4, f"{unit}/px")))
    else:
        rows.append((tr("Skala X / Y"), f"{fmt_pm(d['scale_x'], d['scale_x_std'], 4)} / "
                                       f"{fmt_pm(d['scale_y'], d['scale_y_std'], 4)} {unit}/px"))
        ratio = d["scale_x"] / d["scale_y"] if d["scale_y"] else float("nan")
        rows.append((tr("Stosunek skal X/Y (skurcz papieru?)"), f"{fmt(ratio, 4)} "
                     f"<span class='muted'>({fmt((ratio - 1) * 100, 2)} %)</span>"))
        rows.append((tr("Skos osi (shear)"), fmt_pm(d["shear_deg"], d["shear_deg_std"], 3, "°")))
    rows.append((tr("Kąt obrotu arkusza"), fmt_pm(d["rotation_deg"], d["rotation_deg_std"], 3, "°")))
    if fit.model == "poly2":
        rows.append((tr("Uwaga"), f"<span class='muted'>{tr('parametry liczone w centroidzie punktów')}</span>"))
    h.append(_kv(rows))

    h.append(f"<h3>{tr('Szacowana skala mianowana mapy')}</h3>")
    rows = []
    for v in sorted({150, 300, 600, int(dpi)}):
        m = scale_denominator(d["scale"] * mpu, v)
        ms = scale_denominator(d["scale_std"] * mpu, v) if np.isfinite(d["scale_std"]) else float("nan")
        mark = " ◂" if v == int(dpi) else ""
        rows.append((f"{v} DPI{mark}", f"{fmt_scale(m)} <span class='muted'>(± {fmt(ms, 0)})</span>"))
    h.append(_kv(rows))

    # --- punkty odstające
    h.append(f"<h3>{tr('Punkty odstające')}</h3>")
    w = fit.std_residuals
    if w is None or not np.any(np.isfinite(w)):
        h.append(f"<div class='muted'>{tr('Za mało obserwacji nadliczbowych do testu.')}</div>")
    else:
        idx = [i for i in np.argsort(-np.nan_to_num(w, nan=-1)) if np.isfinite(w[i]) and w[i] > OUTLIER_THRESHOLD]
        if not idx:
            h.append(f"<div class='good'>{tr('Brak punktów odstających')} "
                     f"<span class='muted'>(|w| ≤ {OUTLIER_THRESHOLD})</span></div>")
        else:
            h.append("<table width='100%'><tr>"
                     f"<th>#</th><th>{tr('Błąd')}</th><th>{tr('Błąd LOO')}</th><th>w</th><th>{tr('nadwyżkowość')}</th></tr>")
            for i in idx[:15]:
                h.append(f"<tr><td>{labels[i]}</td><td class='n'>{fmt(fit.errors[i], 2, unit)}</td>"
                         f"<td class='n'>{fmt(fit.loo_errors[i], 2, unit)}</td>"
                         f"<td class='n bad'>{fmt(w[i], 2)}</td>"
                         f"<td class='n'>{fmt(fit.redundancy_numbers[i], 2)}</td></tr>")
            h.append("</table>")
            h.append(f"<div class='note'>{tr('Test Baardy: reszta standaryzowana w > 3.29 (α = 0.001). Sprawdź identyfikację punktu lub wyłącz go i przelicz.')}</div>")

    # --- porównanie modeli
    cmp = ctx.get("comparison") or []
    if len(cmp) > 1:
        h.append(f"<h3>{tr('Porównanie modeli transformacji')}</h3>")
        h.append("<table width='100%'><tr>"
                 f"<th>{tr('Model')}</th><th>p</th><th>σ₀</th><th>RMSE</th><th>LOO</th><th>ΔAICc</th></tr>")
        for c in cmp:
            best = np.isfinite(c["delta_aicc"]) and c["delta_aicc"] == 0
            cls = " class='good'" if best else ""
            h.append(f"<tr><td{cls}>{escape(tr(c['name']))}</td><td class='n'>{c['n_params']}</td>"
                     f"<td class='n'>{fmt(c['sigma0'], 2)}</td><td class='n'>{fmt(c['rmse'], 2)}</td>"
                     f"<td class='n'>{fmt(c['loo_rmse'], 2)}</td><td class='n'>{fmt(c['delta_aicc'], 1)}</td></tr>")
        h.append("</table>")
        h.append(f"<div class='note'>{tr('ΔAICc < 2 – modele równorzędne; > 10 – model wyraźnie gorszy. Niższy błąd LOO oznacza lepszą zdolność predykcji.')}</div>")

    # --- zniekształcenia lokalne
    dist = ctx.get("distortion")
    if dist:
        a = dist["area_scale"]
        h.append(f"<h3>{tr('Zniekształcenia lokalne (TPS, wewnątrz otoczki punktów)')}</h3>")
        rows = [
            (tr("Skala lokalna (5–95 percentyl)") + f" @ {dpi:g} DPI",
             f"{fmt_scale(scale_denominator(a['p05'] * mpu, dpi))} … {fmt_scale(scale_denominator(a['p95'] * mpu, dpi))}"),
            (tr("Zmienność skali (p95/p05)"), fmt(dist["scale_variation_pct"], 1, "%")),
            (tr("Zakres rotacji lokalnej (p05–p95)"), f"{fmt(dist['rotation_deg']['p05'], 2)}° … {fmt(dist['rotation_deg']['p95'], 2)}° "
                                                   f"<span class='muted'>(Δ {fmt(dist['rotation_range_deg'], 2)}°)</span>"),
            (tr("Zniekształcenie kątowe 2ω średnie / maks."), f"{fmt(dist['omega_deg']['mean'], 2)}° / {fmt(dist['omega_deg']['max'], 2)}°"),
            (tr("Anizotropia skali a/b średnia / maks."), f"{fmt(dist['anisotropy']['mean'], 3)} / {fmt(dist['anisotropy']['max'], 3)}"),
        ]
        h.append(_kv(rows))

    # --- autokorelacja
    moran = ctx.get("moran")
    if moran:
        h.append(f"<h3>{tr('Autokorelacja przestrzenna reszt (Moran I)')}</h3>")
        rows = []
        significant = False
        for lab, res in (("vX", moran.get("x")), ("vY", moran.get("y")), ("|v|", moran.get("abs"))):
            if not res:
                continue
            I, E, p = res
            sig = p < 0.05 and I > E
            significant |= sig
            cls = "warn" if sig else "muted"
            rows.append((f"I({lab})", f"{fmt(I, 3)} <span class='{cls}'>(E = {fmt(E, 3)}, p = {fmt(p, 3)})</span>"))
        h.append(_kv(rows))
        if significant:
            h.append(f"<div class='note'>{tr('Reszty sąsiednich punktów są podobne – błędy mają charakter systematyczny (lokalne zniekształcenia, łączenie arkuszy, różne źródła pomiarów). Rozważ model wyższego rzędu lub analizę siatki zniekształceń.')}</div>")
        else:
            h.append(f"<div class='note'>{tr('Brak istotnej autokorelacji – reszty mają charakter losowy.')}</div>")

    proj = ctx.get("projection_html")
    if proj:
        h.append(proj)
    return "".join(h)


def projection_summary_html(est, dpi: float, colors: dict, with_css=False) -> str:
    h = [_css(colors)] if with_css else []
    h.append(f"<h3>{tr('Estymacja odwzorowania kartograficznego')}</h3>")
    best = est.best
    if best is None:
        h.append(f"<div class='bad'>{tr('Nie udało się dopasować żadnego odwzorowania.')}</div>")
        return "".join(h)
    conf = best.weight
    if est.conclusive:
        cls = "good" if conf > 0.6 else "warn"
        h.append(f"<div>{tr('Najlepiej dopasowane')}: <b>{escape(tr(best.name))}</b> "
                 f"<span class='{cls}'>({tr('waga Akaike')} {fmt(conf * 100, 0)} %)</span></div>")
    else:
        names = ", ".join(escape(tr(c.name)) for c in est.equivalent()) or escape(tr(best.name))
        h.append(f"<div class='warn'>{tr('Wynik nierozstrzygnięty – odwzorowania równorzędne (ΔAICc < 2)')}: </div>"
                 f"<div><b>{names}</b></div>"
                 f"<div class='note'>{tr('Kolejność w rankingu nie przesądza o odwzorowaniu. Proste odwzorowania bez dopasowywanych parametrów wygrywają, gdy dane nie wnoszą dodatkowej informacji. Porównaj siatkę geograficzną kandydatów z siatką (lub ramką z podziałką) narysowaną na mapie.')}</div>")
    rows = [(tr("Zasięg punktów"),
             f"λ {fmt(est.lon_range[0], 2)}° … {fmt(est.lon_range[1], 2)}°, φ {fmt(est.lat_range[0], 2)}° … {fmt(est.lat_range[1], 2)}°"),
            (tr("Dopasowanie"), tr("afiniczne (z anizotropią papieru)") if est.fit_mode == "affine" else tr("Helmerta (przez podobieństwo)"))]
    h.append(_kv(rows))
    h.append("<table width='100%'><tr>"
             f"<th>{tr('Odwzorowanie')}</th><th>σ₀ [px]</th><th>ΔAICc</th><th>{tr('waga')}</th><th>{tr('skala')}</th></tr>")
    for c in est.candidates[:8]:
        cls = " class='good'" if c is best else ""
        label = escape(tr(c.name)) + (f" <i>{tr('(hipoteza)')}</i>" if c.is_hypothesis else "")
        h.append(f"<tr><td{cls}>{label}</td><td class='n'>{fmt(c.sigma0_px, 2)}</td>"
                 f"<td class='n'>{fmt(c.delta_aicc, 1)}</td><td class='n'>{fmt(c.weight * 100, 1)}%</td>"
                 f"<td class='n'>{fmt_scale(c.scale_denominator(dpi))}</td></tr>")
    h.append("</table>")
    for m in est.messages:
        h.append(f"<div class='warn'>⚠ {escape(message_text(m))}</div>")
    return "".join(h)


def candidate_html(c, est, dpi: float, colors: dict) -> str:
    h = [_css(colors)]
    h.append(f"<h2>{escape(tr(c.name))}" + (f" <i>{tr('(hipoteza)')}</i>" if c.is_hypothesis else "") + "</h2>")
    h.append(f"<div class='muted'>{escape(tr(c.family))} · {escape(tr(c.era))}</div>")
    h.append(f"<p>{escape(tr(c.note))}</p>")
    sig_mm = c.sigma0_px * 25.4 / dpi if dpi > 0 else float("nan")
    rows = [
        (tr("Błąd średni σ₀"), f"{fmt(c.sigma0_px, 2)} px · {fmt(sig_mm, 2)} mm @ {dpi:g} DPI"),
        ("ΔAICc / " + tr("waga Akaike"), f"{fmt(c.delta_aicc, 2)} / {fmt(c.weight * 100, 1)} %"),
        (tr("Skala w środku obszaru punktów"), fmt_scale(c.scale_denominator(dpi))),
    ]
    if not c.is_reference:
        k = c.local_scale_factor()
        if np.isfinite(k) and abs(k - 1.0) > 0.005:
            rows.append((tr("Skala główna odwzorowania") + f" <span class='muted'>(k = {fmt(k, 4)})</span>",
                         fmt_scale(c.principal_scale_denominator(dpi))))
    if not c.is_reference:
        rot = math.degrees(math.atan2(c.A[1, 0], c.A[0, 0]))
        rows.append((tr("Obrót siatki względem skanu"), fmt(rot, 3, "°")))
    h.append(_kv(rows))

    if c.params_deg:
        h.append(f"<h3>{tr('Parametry odwzorowania')}</h3>")
        rows = []
        for k, v in c.params_deg.items():
            s = c.params_std_deg.get(k)
            label = tr(PARAM_LABELS.get(k, k))
            if s is None:
                label += f" <span class='muted'>({tr('ustalony')})</span>"
            val = format_dms(v, "E", "W") if k == "lon0" else format_dms(v, "N", "S")
            if s is not None and np.isfinite(s):
                cls = "bad" if s > 10 else ("warn" if s > 2 else "muted")
                val += f" <span class='{cls}'>± {fmt(s, 2)}°</span>"
            rows.append((label, val))
        h.append(_kv(rows))
        if "lon0" in c.params_deg and ("lon0" in c.params_std_deg or c.is_hypothesis):
            pm = prime_meridian_variants(c.params_deg["lon0"])
            txt = ", ".join(f"{escape(tr(n))}: {fmt(v, 2)}°" for n, v in pm)
            h.append(f"<div class='note'>{tr('λ₀ względem historycznych południków zerowych')}: {txt}</div>")

    if c.is_hypothesis and est is not None:
        h.append(f"<h3>{tr('Ocena hipotezy')}</h3>")
        free = est.free_fit(c.key)
        rows = []
        if free is not None and np.isfinite(free.sigma0_px):
            rows.append((tr("σ₀ swobodnego dopasowania tego odwzorowania"), f"{fmt(free.sigma0_px, 2)} px"))
            rows.append((tr("ΔAICc hipoteza − swobodne dopasowanie"), fmt(c.aicc - free.aicc, 1)))
            if free.params_deg:
                txt = ", ".join(f"{escape(tr(PARAM_LABELS.get(k, k)))} = {format_dms(v, 'E', 'W') if k == 'lon0' else format_dms(v, 'N', 'S')}"
                                for k, v in free.params_deg.items() if k in free.params_std_deg)
                rows.append((tr("Parametry swobodnego dopasowania"), txt or "–"))
        h.append(_kv(rows))
        if np.isfinite(c.delta_aicc) and c.delta_aicc < 2:
            h.append(f"<div class='good'>{tr('Hipoteza jest zgodna z danymi (ΔAICc < 2 względem najlepszego modelu).')}</div>")
        elif np.isfinite(c.delta_aicc) and c.delta_aicc < 10:
            h.append(f"<div class='warn'>{tr('Hipoteza słabiej wspierana przez dane (2 ≤ ΔAICc < 10) – sprawdź parametry i punkty odstające.')}</div>")
        else:
            h.append(f"<div class='bad'>{tr('Hipoteza wyraźnie gorsza od najlepszego modelu (ΔAICc ≥ 10).')}</div>")
        h.append(f"<div class='note'>{tr('Parametry ustalone – dopasowano tylko położenie, skalę i obrót siatki względem skanu. Porównaj siatkę hipotezy z podziałką ramki mapy.')}</div>")

    if est and est.messages and c is est.best:
        for m in est.messages:
            h.append(f"<div class='warn'>⚠ {escape(message_text(m))}</div>")
    h.append(f"<div class='note'>{tr('Obliczenia na sferze; współrzędne geograficzne punktów wyznaczone z układu odniesienia projektu.')}</div>")
    return "".join(h)
