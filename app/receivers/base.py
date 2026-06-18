"""Einheitliche Receiver-Schnittstelle für alle Geräte-Backends.

Die GUI und die Mirror-Engines sprechen ausschließlich dieses Interface – egal,
ob dahinter ein Google Chromecast oder ein LG-webOS-TV steckt. Welche Funktionen
ein konkretes Gerät kann, meldet :meth:`Receiver.supports`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Context:
    """Geteilte Ressourcen, die Receiver beim Verbinden brauchen."""

    zconf: object = None    # zeroconf-Instanz (Chromecast)
    config: object = None   # App-Config (webOS client_key …)
    server: object = None   # lokaler HTTP-Server


class Feature:
    MEDIA = "media"                # lokale Datei / URL abspielen
    YOUTUBE = "youtube"           # YouTube-Video starten
    PLAYBACK = "playback"         # play/pause/stop
    VOLUME = "volume"             # Lautstärke setzen
    MIRROR_HLS = "mirror_hls"     # Bildschirm via HLS spiegeln (Chromecast)
    MIRROR_NATIVE = "mirror_native"  # Low-Latency-Spiegeln (Cast-Streaming)
    MIRROR_DLNA_TS = "mirror_dlna_ts"  # Bildschirm via DLNA-Live-MPEG-TS (LG)
    MIRROR_AIRPLAY = "mirror_airplay"  # Bildschirm via AirPlay spiegeln (LG, später)
    # alle Mirror-Wege (für GUI-Gating "kann spiegeln?")
    ANY_MIRROR = (MIRROR_HLS, MIRROR_NATIVE, MIRROR_DLNA_TS, MIRROR_AIRPLAY)


class Receiver(ABC):
    """Aktive (oder verbindbare) Sitzung zu genau einem Gerät."""

    kind: str = "base"

    def __init__(self, info, context) -> None:  # noqa: ANN001
        self.info = info
        self.context = context

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def host(self) -> str:
        return self.info.host

    # -- Lebenszyklus ---------------------------------------------------------
    @abstractmethod
    def connect(self, prompt_cb=None) -> None:  # noqa: ANN001
        """Verbindet. *prompt_cb* wird aufgerufen, falls am Gerät eine
        Bestätigung nötig ist (z. B. webOS-Pairing-Dialog)."""

    @abstractmethod
    def disconnect(self) -> None: ...

    # -- Inhalte --------------------------------------------------------------
    @abstractmethod
    def play_media(self, url: str, mime: str, *, title: str | None = None,
                   live: bool = False) -> None: ...

    def play_youtube(self, video_id: str) -> None:
        raise NotImplementedError("Dieses Gerät unterstützt kein YouTube-Casting")

    # -- Steuerung (optional je Backend) -------------------------------------
    def play(self) -> None: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...
    def set_volume(self, level: float) -> None: ...

    # -- Fähigkeiten ----------------------------------------------------------
    @abstractmethod
    def supports(self, feature: str) -> bool: ...
