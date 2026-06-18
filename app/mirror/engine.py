"""Gemeinsames Interface und Registry für Mirror-Engines."""
from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class EngineInfo:
    name: str            # interner Schlüssel (config)
    display_name: str    # für die UI
    available: bool      # Abhängigkeiten erfüllt?
    detail: str          # Hinweis (z. B. warum nicht verfügbar)


class MirrorEngine(ABC):
    """Basisklasse: spiegelt den Bildschirm auf ein verbundenes Cast-Gerät.

    Eine Engine kapselt Bildschirm-Capture, Encoding und den Transport zum Gerät.
    Sie bekommt den aktiven Receiver, den lokalen Media-Server und die Config.
    """

    name: str = "base"
    display_name: str = "Basis"

    def __init__(self, receiver, server, config) -> None:  # noqa: ANN001
        self.receiver = receiver
        self.server = server
        self.config = config
        self._running = False

    @abstractmethod
    def start(self) -> None:
        """Startet Capture + Übertragung. Wirft bei Fehler eine Exception."""

    @abstractmethod
    def stop(self) -> None:
        """Beendet die Übertragung und gibt Ressourcen frei."""

    @property
    def running(self) -> bool:
        return self._running

    @staticmethod
    @abstractmethod
    def check_available() -> tuple[bool, str]:
        """(verfügbar?, Hinweistext) – prüft die Abhängigkeiten der Engine."""


# --- Gemeinsame Capture-Helfer ----------------------------------------------

def session_is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


def gst_element_exists(name: str) -> bool:
    """Prüft, ob ein GStreamer-Element registriert ist (z. B. 'x264enc')."""
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)
        return Gst.ElementFactory.find(name) is not None
    except Exception:
        return False


# --- Registry ----------------------------------------------------------------

def _registry() -> dict[str, type[MirrorEngine]]:
    # Lazy-Import, damit fehlende optionale Abhängigkeiten nicht alles blockieren.
    from .dlnats import DlnaTsMirrorEngine
    from .hls import HlsMirrorEngine
    from .native import NativeMirrorEngine
    from .openscreen import OpenscreenMirrorEngine

    return {
        NativeMirrorEngine.name: NativeMirrorEngine,
        HlsMirrorEngine.name: HlsMirrorEngine,
        DlnaTsMirrorEngine.name: DlnaTsMirrorEngine,
        OpenscreenMirrorEngine.name: OpenscreenMirrorEngine,
    }


def get_engine_class(name: str) -> type[MirrorEngine]:
    reg = _registry()
    if name not in reg:
        raise KeyError(f"Unbekannte Mirror-Engine: {name!r}")
    return reg[name]


def available_engines() -> list[EngineInfo]:
    """Listet alle Engines mit Verfügbarkeits-Status (für die Einstellungen)."""
    infos: list[EngineInfo] = []
    for name, cls in _registry().items():
        ok, detail = cls.check_available()
        infos.append(EngineInfo(name, cls.display_name, ok, detail))
    return infos
