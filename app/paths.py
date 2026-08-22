"""Auffinden der mitgelieferten Datendateien (VERSION, lang/, logo.png).

Schwupp läuft aus drei verschiedenen Layouts, die alle bedient werden müssen:

* **Git-Klon** – die Daten liegen im Projekt-Root *neben* dem ``app``-Paket.
* **Flatpak / systemweite Installation** – sie liegen unter
  ``<prefix>/share/schwupp`` (``/app/share/schwupp`` im Flatpak).
* **Entwicklung mit abweichendem Layout** – ``SCHWUPP_DATA_DIR`` überschreibt alles.

Früher wurde hart ``Path(__file__).parent.parent`` angenommen; nach einem echten
``pip install`` (Paket in site-packages) zeigte das ins Leere, weshalb weder die
Version noch die Übersetzungen gefunden wurden.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Kandidaten in Reihenfolge ihrer Priorität. Ein Verzeichnis gilt als
# Datenverzeichnis, sobald es eine VERSION-Datei ODER ein lang/ enthält.
_MARKERS = ("VERSION", "lang")


def _looks_like_data_dir(path: Path) -> bool:
    return any((path / m).exists() for m in _MARKERS)


def _candidates() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("SCHWUPP_DATA_DIR")
    if env:
        out.append(Path(env))
    pkg = Path(__file__).resolve().parent
    out.append(pkg.parent)              # Git-Klon: Projekt-Root
    out.append(pkg)                     # Daten im Paket (setuptools package-data)
    out.append(Path("/app/share/schwupp"))                  # Flatpak
    out.append(Path(sys.prefix) / "share" / "schwupp")      # /usr, ~/.local, venv
    out.append(Path.home() / ".local" / "share" / "schwupp")
    return out


def data_dir() -> Path:
    """Verzeichnis mit den mitgelieferten Datendateien (erster Treffer)."""
    for cand in _candidates():
        try:
            if cand.is_dir() and _looks_like_data_dir(cand):
                return cand
        except OSError:
            continue
    return Path(__file__).resolve().parent.parent  # Fallback: altes Verhalten


def data_file(*parts: str) -> Path:
    """Pfad zu einer Datendatei; sucht alle Kandidaten ab, damit ein teilweise
    verteiltes Layout (z. B. VERSION im Paket, lang/ im Prefix) noch trägt."""
    rel = Path(*parts)
    for cand in _candidates():
        try:
            if (cand / rel).exists():
                return cand / rel
        except OSError:
            continue
    return data_dir() / rel


def project_root() -> Path:
    """Wurzel eines Git-Klons (für den Updater). ``None``-sicher: liefert im
    installierten Fall das Verzeichnis über dem Paket, das dort nicht schreibbar
    ist – der Updater prüft das selbst."""
    return Path(__file__).resolve().parent.parent
