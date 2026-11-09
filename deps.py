"""
Sprawdzanie zależności zewnętrznych (numpy, scipy) i wskazówki instalacji dla platformy.

numpy i scipy są częścią oficjalnych instalatorów QGIS (Windows: instalator samodzielny / OSGeo4W,
macOS), ale w minimalnej instalacji OSGeo4W oraz w pakietach dystrybucji Linuksa scipy bywa
osobnym pakietem. Wtyczka nie instaluje niczego sama – instalacja przez pip do Pythona QGIS
może uszkodzić środowisko (niezgodne wersje numpy); podpowiada właściwe polecenie systemowe.

Moduł nie importuje numpy/scipy na poziomie modułu, więc działa także, gdy ich brakuje.
"""

import importlib
import os
import platform
import sys

# moduł -> funkcje wtyczki, które od niego zależą
OPTIONAL = {
    "scipy.optimize": "estymacja odwzorowania",
    "scipy.interpolate": "lokalne zniekształcenia (TPS), izolinie, siatki zniekształceń",
    "scipy.spatial": "otoczka punktów (obszar wiarygodny)",
}


def _importable(name):
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


def check():
    """Zwraca (brakujące wymagane pakiety, brakujące moduły opcjonalne)."""
    required = [] if _importable("numpy") else ["numpy"]
    optional = [m for m in OPTIONAL if not required and not _importable(m)]
    return required, optional


def _linux_ids():
    try:
        info = platform.freedesktop_os_release()
    except (OSError, AttributeError):
        return set()
    return {info.get("ID", "")} | set(info.get("ID_LIKE", "").split())


def install_commands(packages):
    """Lista (opis platformy, polecenie) dla bieżącego systemu."""
    pkgs = sorted(set(packages))
    pip_names = " ".join(pkgs)
    if os.path.exists(os.path.join(sys.prefix, "conda-meta")):
        return [("conda", f"conda install -c conda-forge {pip_names}")]
    if sys.platform.startswith("win"):
        osgeo = " ".join(f"python3-{p}" for p in pkgs)
        return [("OSGeo4W Setup", f"osgeo4w-setup.exe → Advanced Install → Libs → {osgeo}"),
                ("OSGeo4W Shell", f"python -m pip install {pip_names}")]
    if sys.platform == "darwin":
        return [("macOS (QGIS.app)", f"/Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install --user {pip_names}")]
    if os.path.exists("/.flatpak-info") or os.environ.get("FLATPAK_ID"):
        return [("Flatpak", f"flatpak run --command=pip3 org.qgis.qgis install --user {pip_names}")]
    ids = _linux_ids()
    deb = " ".join(f"python3-{p}" for p in pkgs)
    table = [
        ({"debian", "ubuntu"}, "Debian / Ubuntu", f"sudo apt install {deb}"),
        ({"fedora", "rhel", "centos"}, "Fedora / RHEL", f"sudo dnf install {deb}"),
        ({"opensuse", "suse", "opensuse-leap", "opensuse-tumbleweed"}, "openSUSE", f"sudo zypper install {deb}"),
        ({"arch"}, "Arch Linux", "sudo pacman -S " + " ".join(f"python-{p}" for p in pkgs)),
    ]
    matched = [(label, cmd) for keys, label, cmd in table if keys & ids]
    return matched or [(label, cmd) for _keys, label, cmd in table]


def packages_of(modules):
    return sorted({m.split(".")[0] for m in modules})
