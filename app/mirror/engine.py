"""Gemeinsames Interface und Registry für Mirror-Engines.

Die Basisklasse übernimmt alles, was für jede Engine gleich ist:

* **Capture-Auswahl** über :class:`~app.mirror.capture.CaptureSelector`
  (X11 → Portal → wf-recorder) statt dreifach kopierter Fallback-Ketten,
* **Start im Worker-Thread** – Engines bauen Pipelines und warten auf Geräte,
  was den GTK-Hauptthread sekundenlang blockieren würde,
* **Fehlermeldung per Callback** – Startfehler landen sichtbar in der Oberfläche
  statt nur auf stdout (dort blieb eine gescheiterte Spiegelung unbemerkt, die
  UI zeigte weiter „läuft").
"""
from __future__ import annotations

import contextlib
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
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

    Unterklassen implementieren :meth:`run` (läuft bereits im Worker-Thread und
    bekommt die fertige Capture-Quelle) sowie :meth:`stop`.
    """

    name: str = "base"
    display_name: str = "Basis"

    def __init__(self, receiver, server, config, on_error=None,
                 on_started=None) -> None:  # noqa: ANN001
        self.receiver = receiver
        self.server = server
        self.config = config
        self._running = False
        self._on_error: Callable[[str], None] | None = on_error
        self._on_started: Callable[[], None] | None = on_started
        self._capture = None

    # -- Config ---------------------------------------------------------------
    def cfg(self, key: str):
        """Gerätespezifischer Config-Wert (mit globalem Default als Fallback).

        Liest über uuid *und* host-Fallback (siehe ``config.device_keys``), damit
        Einstellungen einen Wechsel der Cast-uuid (echte uuid <-> host) überleben.
        """
        info = getattr(self.receiver, "info", None)
        if info is not None and hasattr(self.config, "device_value_for"):
            return self.config.device_value_for(info, key)
        return self.config[key]

    def cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.cfg(key))
        except (KeyError, TypeError, ValueError):
            return default

    def video_params(self) -> tuple[int, int, int, int]:
        """(Breite, Höhe, FPS, Bitrate in kbit/s) aus der Config, 16:9-gerahmt."""
        height = self.cfg_int("mirror_height", 1080)
        width = (height * 16 // 9) // 2 * 2      # gerade Zahl, sonst mag x264 nicht
        return width, height, self.cfg_int("mirror_fps", 30), \
            self.cfg_int("mirror_bitrate_kbps", 6000)

    # -- Lebenszyklus ---------------------------------------------------------
    def start(self) -> None:
        """Startet Capture + Übertragung. Kehrt sofort zurück; Fehler kommen
        über den ``on_error``-Callback (nicht als Exception, weil der eigentliche
        Start asynchron in Worker-Threads passiert)."""
        if self._running:
            return
        self._running = True
        from .capture import CaptureSelector

        self._capture = CaptureSelector(fps=self.cfg_int("mirror_fps", 30))
        self._capture.start(on_ready=self._on_capture_ready, on_error=self.fail)

    def _on_capture_ready(self, source_desc: str) -> None:
        """Capture steht – ab hier im Worker-Thread weiterarbeiten.

        Der Aufruf kommt je nach Weg aus dem GTK-Hauptthread (X11 direkt) oder
        aus der GLib-MainLoop (Portal-Callback). Beides darf nicht blockieren,
        :meth:`run` wartet aber auf Geräte-Antworten und HLS-Segmente.
        """
        threading.Thread(target=self._run_guarded, args=(source_desc,),
                         daemon=True, name=f"mirror-{self.name}").start()

    def _run_guarded(self, source_desc: str) -> None:
        try:
            self.run(source_desc)
        except Exception as exc:  # noqa: BLE001
            self.fail(exc)
            return
        # Erst jetzt läuft die Übertragung wirklich – die Oberfläche meldet das
        # Ergebnis, statt beim Klick optimistisch "läuft" anzuzeigen.
        if self._running and self._on_started is not None:
            self._on_started()

    def fail(self, message) -> None:  # noqa: ANN001
        """Meldet einen Fehler an die Oberfläche und beendet die Engine.

        Darf aus jedem Thread gerufen werden; der Callback der GUI marshallt
        selbst in den Hauptthread.
        """
        self._running = False
        text = str(message)
        with contextlib.suppress(Exception):
            self.stop()
        if self._on_error is not None:
            self._on_error(text)
        else:
            print(f"[{self.name}] {text}")

    @abstractmethod
    def run(self, source_desc: str) -> None:
        """Baut die Pipeline und startet die Übertragung (im Worker-Thread).

        *source_desc* ist der fertige vordere Pipeline-Teil (rohes video/x-raw).
        Wirft bei Fehler eine Exception – sie wird als :meth:`fail` gemeldet.
        """

    @abstractmethod
    def stop(self) -> None:
        """Beendet die Übertragung und gibt Ressourcen frei (mehrfach aufrufbar)."""

    def _stop_capture(self) -> None:
        """Hilfsmethode für Unterklassen: Capture-Hilfsprozess beenden."""
        if self._capture is not None:
            self._capture.stop()
            self._capture = None

    @property
    def running(self) -> bool:
        return self._running

    @staticmethod
    @abstractmethod
    def check_available() -> tuple[bool, str]:
        """(verfügbar?, Hinweistext) – prüft die Abhängigkeiten der Engine."""


# --- Gemeinsame GStreamer-Helfer ---------------------------------------------

def gst_element_exists(name: str) -> bool:
    """Prüft, ob ein GStreamer-Element registriert ist (z. B. 'x264enc')."""
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)
        return Gst.ElementFactory.find(name) is not None
    except Exception:  # noqa: BLE001
        return False


def gst_init():
    """Initialisiert GStreamer einmalig und liefert das Gst-Modul."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)
    return Gst


# --- Registry ----------------------------------------------------------------

def _registry() -> dict[str, type[MirrorEngine]]:
    # Lazy-Import, damit fehlende optionale Abhängigkeiten nicht alles blockieren.
    from .dlnats import DlnaTsMirrorEngine
    from .hls import HlsMirrorEngine
    from .native import NativeMirrorEngine

    return {
        NativeMirrorEngine.name: NativeMirrorEngine,
        HlsMirrorEngine.name: HlsMirrorEngine,
        DlnaTsMirrorEngine.name: DlnaTsMirrorEngine,
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


# Welche Spiegel-Engines pro Gerätetyp sinnvoll sind. Reihenfolge = Vorzug.
# webOS-only und reine DLNA-TVs bieten von Linux aus kein zuverlässiges
# Live-Mirroring (siehe receivers/webos.py, docs/MIRRORING.md) -> leer.
# AirPlay-TVs spielen HLS nativ -> HLS-Engine (play_url auf die Live-Playlist).
_KIND_ENGINES: dict[str, tuple[str, ...]] = {
    "chromecast": ("native", "hls"),
    "webos": (),
    "airplay": ("hls",),
    "dlna": (),
}


def engines_for_kind(kind: str) -> list[EngineInfo]:
    """Engines, die für ein Gerät dieses Typs in Frage kommen (mit Status)."""
    reg = _registry()
    infos: list[EngineInfo] = []
    for name in _KIND_ENGINES.get(kind, ()):
        cls = reg[name]
        ok, detail = cls.check_available()
        infos.append(EngineInfo(name, cls.display_name, ok, detail))
    return infos
