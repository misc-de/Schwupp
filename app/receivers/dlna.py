"""Generisches DLNA/UPnP-Backend.

Spricht jeden UPnP-MediaRenderer über AVTransport an (Samsung, Sony, Panasonic,
Philips, viele Hisense …). Damit lassen sich **lokale Mediendateien** und per
yt-dlp aufgelöste **Web-/YouTube-Videos** abspielen sowie Play/Pause/Stopp
steuern. Kein YouTube-App-Start (DLNA kennt keine Apps) und keine
Bildschirmspiegelung (DLNA-Live ist auf TVs unzuverlässig, s. docs/MIRRORING.md).

Die Control-URL ist beim Verbinden meist schon aus der Discovery bekannt
(``info.raw``); andernfalls wird sie per SSDP aufgelöst.
"""
from __future__ import annotations

from ..dlna import DlnaRenderer
from .base import Feature, Receiver

_FEATURES = {Feature.MEDIA, Feature.PLAYBACK}


class DlnaReceiver(Receiver):
    kind = "dlna"

    def __init__(self, info, context) -> None:  # noqa: ANN001
        super().__init__(info, context)
        control = info.raw if isinstance(info.raw, str) else None
        self._dlna = DlnaRenderer(info.host, control=control)

    # -- Verbindung -----------------------------------------------------------
    def connect(self, prompt_cb=None) -> None:  # noqa: ANN001
        if self._dlna._control is None:
            if not self._dlna.resolve():
                raise RuntimeError("Kein DLNA-AVTransport am Gerät gefunden")

    def disconnect(self) -> None:
        try:
            self._dlna.stop()
        except Exception:  # noqa: BLE001
            pass

    # -- Inhalte --------------------------------------------------------------
    def play_media(self, url, mime, *, title=None, live=False) -> None:  # noqa: ANN001
        if self._dlna._control is None:
            self._dlna.resolve()
        self._dlna.play_url(url, mime, title or "Schwupp")

    # -- Steuerung ------------------------------------------------------------
    def play(self) -> None:
        self._dlna.play()

    def pause(self) -> None:
        self._dlna.pause()

    def stop(self) -> None:
        self._dlna.stop()

    def set_volume(self, level: float) -> None:
        self._dlna.set_volume(level)

    def supports(self, feature: str) -> bool:
        # Lautstärke nur, wenn der Renderer RenderingControl anbietet
        # (steht erst nach connect()/resolve() fest).
        if feature == Feature.VOLUME:
            return self._dlna.has_volume
        return feature in _FEATURES
