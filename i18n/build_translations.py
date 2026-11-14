"""
Generuje pliki i18n/QMapAnalyst_{pl,en}.ts oraz kompiluje je do .qm.

Teksty źródłowe (polskie) zbierane są z wywołań self.tr("...") / tr("...") w kodzie
wtyczki oraz z tekstów dynamicznych (nazwy modeli, odwzorowań, parametrów).
Tłumaczenia angielskie znajdują się w i18n/en.py (słownik EN).

Uruchomienie (OSGeo4W):  python-qgis.bat i18n/build_translations.py [--list]
"""
import ast
import os
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(ROOT))

CONTEXTS = {
    "dockwidget.py": "QMapAnalystDockWidget",
    "plugin.py": "QMapAnalystPlugin",
    "report.py": "QMapAnalystReport",
    "export.py": "QMapAnalystExport",
    "layers.py": "QMapAnalystLayers",
    "library_panel.py": "QMapAnalystLibrary",
    "analysis_store.py": "QMapAnalystDockWidget",
    "frame.py": "QMapAnalystDockWidget",
    "import_dialog.py": "QMapAnalystLibrary",
    "coverage_dialog.py": "QMapAnalystLibrary",
    "maps_dialog.py": "QMapAnalystLibrary",
    "sets_dialog.py": "QMapAnalystLibrary",
}


def collect():
    found = {}

    def add(ctx, text):
        found.setdefault(ctx, [])
        if text not in found[ctx]:
            found[ctx].append(text)

    for fname, ctx in CONTEXTS.items():
        tree = ast.parse(open(os.path.join(ROOT, fname), encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if name != "tr":
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                add(ctx, arg.value)

    for fname in ("math_engine.py", "projections.py"):
        tree = ast.parse(open(os.path.join(ROOT, fname), encoding="utf-8").read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "EngineError"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                add("QMapAnalystEngine", node.args[0].value)
    for m in __import__(f"{os.path.basename(ROOT)}.math_engine", fromlist=["*"]).MODELS.values():
        add("QMapAnalystEngine", m["name"])

    pkg = os.path.basename(ROOT)
    me = __import__(f"{pkg}.math_engine", fromlist=["*"])
    pj = __import__(f"{pkg}.projections", fromlist=["*"])
    dynamic = [m["name"] for m in me.MODELS.values()]
    for p in pj.PROJECTIONS:
        dynamic += [p.name, p.family, p.era, p.note]
    dynamic += list(pj.PARAM_LABELS.values()) + [n for n, _ in pj.PRIME_MERIDIANS]
    for pr in pj.PRESETS:
        dynamic += [pr["name"], pr["source"]]
    import re
    src = open(os.path.join(ROOT, "projections.py"), encoding="utf-8").read()
    for m in re.finditer(r'msgs\.append\(\((.*?),\s*(?:\{|\{\}|dict)', src, flags=re.S):
        dynamic.append("".join(ast.literal_eval(f"[{m.group(1)}]")))
    dynamic += ["układ odniesienia", "współczesny",
                "Układ współrzędnych projektu, w którym zebrano punkty referencyjne (punkt odniesienia porównania)."]
    for ctx in ("QMapAnalystDockWidget", "QMapAnalystReport"):
        for t in dynamic:
            add(ctx, t)
    for t in __import__(f"{pkg}.deps", fromlist=["*"]).OPTIONAL.values():
        add("QMapAnalystPlugin", t)
    return found


def write_ts(path, lang, found, translations):
    out = ['<?xml version="1.0" encoding="utf-8"?>', "<!DOCTYPE TS>", f'<TS version="2.1" language="{lang}">']
    missing = []
    for ctx, texts in found.items():
        out.append("<context>")
        out.append(f"    <name>{ctx}</name>")
        for t in texts:
            out.append("    <message>")
            out.append(f"        <source>{escape(t)}</source>")
            if translations is None:
                out.append(f"        <translation>{escape(t)}</translation>")
            elif t in translations:
                out.append(f"        <translation>{escape(translations[t])}</translation>")
            else:
                missing.append(t)
                out.append('        <translation type="unfinished"></translation>')
            out.append("    </message>")
        out.append("</context>")
    out.append("</TS>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return missing


def find_lrelease():
    for cand in ("lrelease", "pyside6-lrelease", "lrelease-qt6"):
        p = shutil.which(cand)
        if p:
            return p
    try:
        import PySide6
        p = os.path.join(os.path.dirname(PySide6.__file__), "lrelease.exe")
        if os.path.exists(p):
            return p
    except ImportError:
        pass
    return None


def main():
    found = collect()
    if "--list" in sys.argv:
        for ctx, texts in found.items():
            print(f"## {ctx} ({len(texts)})")
            for t in texts:
                print(repr(t))
        return
    sys.path.insert(0, HERE)
    from en import EN
    missing = write_ts(os.path.join(HERE, "QMapAnalyst_en.ts"), "en_US", found, EN)
    write_ts(os.path.join(HERE, "QMapAnalyst_pl.ts"), "pl_PL", found, None)
    if missing:
        print(f"Brak tłumaczeń EN ({len(missing)}):")
        for t in missing:
            print("  ", repr(t))
    lrel = find_lrelease()
    if not lrel:
        print("Nie znaleziono lrelease – pliki .qm nie zostały skompilowane.")
        return
    for lang in ("en", "pl"):
        ts = os.path.join(HERE, f"QMapAnalyst_{lang}.ts")
        subprocess.run([lrel, ts, "-qm", ts[:-3] + ".qm"], check=True)


if __name__ == "__main__":
    main()
