"""Laufzeit-Abhängigkeiten beim Start prüfen.

* **Erforderlich** (ohne sie startet die App nicht): PyGObject/GTK4/libadwaita
  und pychromecast (Discovery + Cast-Kern).
* **Optional** (App startet, betroffene Funktion fehlt): GStreamer + x264enc
  (Spiegeln), cryptography (nativer Mirror), pywebostv (LG webOS), requests
  (Selbst-Updater), yt-dlp (Web-Video-Extraktion).
"""
from __future__ import annotations

import importlib
import importlib.util
import shutil
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Dep:
    package: str        # was zu installieren ist (für die Meldung)
    feature_key: str    # i18n-Schlüssel der betroffenen Funktion
    probe: Callable[[], bool]

    def ok(self) -> bool:
        try:
            return bool(self.probe())
        except Exception:  # noqa: BLE001
            return False


def _mod(name: str) -> Callable[[], bool]:
    return lambda: importlib.util.find_spec(name) is not None


def _gi(ns: str, ver: str) -> Callable[[], bool]:
    def chk() -> bool:
        import gi
        gi.require_version(ns, ver)
        importlib.import_module(f"gi.repository.{ns}")
        return True
    return chk


def _gst(element: str) -> Callable[[], bool]:
    def chk() -> bool:
        from .mirror.engine import gst_element_exists
        return gst_element_exists(element)
    return chk


def _cli(name: str) -> Callable[[], bool]:
    return lambda: shutil.which(name) is not None


REQUIRED: list[Dep] = [
    Dep("PyGObject + GTK 4", "deps.feat.gui", _gi("Gtk", "4.0")),
    Dep("libadwaita", "deps.feat.gui", _gi("Adw", "1")),
    Dep("pychromecast", "deps.feat.cast", _mod("pychromecast")),
]

OPTIONAL: list[Dep] = [
    Dep("GStreamer (gst-plugins-base/good)", "deps.feat.mirror", _gi("Gst", "1.0")),
    Dep("x264enc (gst-plugins-ugly)", "deps.feat.mirror", _gst("x264enc")),
    Dep("cryptography", "deps.feat.native", _mod("cryptography")),
    Dep("pywebostv", "deps.feat.webos", _mod("pywebostv")),
    Dep("requests", "deps.feat.updater", _mod("requests")),
    Dep("yt-dlp", "deps.feat.webvideo", lambda: _mod("yt_dlp")() or _cli("yt-dlp")()),
]


def check() -> tuple[list[Dep], list[Dep]]:
    """Gibt (fehlende_erforderliche, fehlende_optionale) zurück."""
    return ([d for d in REQUIRED if not d.ok()],
            [d for d in OPTIONAL if not d.ok()])


def gui_available() -> bool:
    """True, wenn GTK4 + libadwaita da sind (sonst ist kein GUI-Dialog möglich)."""
    try:
        return _gi("Gtk", "4.0")() and _gi("Adw", "1")()
    except Exception:  # noqa: BLE001
        return False
