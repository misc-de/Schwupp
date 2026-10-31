"""AirPlay-Lautsprecher (RAOP, ``_raop._tcp``): nur der Systemton.

Den Ton schiebt PipeWires RAOP-Ausgabe selbst zum Lautsprecher (siehe
speaker/pipewire.py); Schwupp verbindet sich dafür nicht eigens. Dateien und
Links spielt Schwupp auf AirPlay-Geräten über das ``airplay``-Backend (pyatv).
"""
from __future__ import annotations

from .base import Feature, Receiver


class RaopReceiver(Receiver):
    kind = "raop"

    def connect(self, prompt_cb=None, pin_cb=None) -> None:  # noqa: ANN001
        """Nichts zu tun: PipeWire baut die Verbindung beim ersten Ton auf."""

    def disconnect(self) -> None:
        pass

    def play_media(self, url, mime, *, title=None, live=False) -> None:  # noqa: ANN001
        raise NotImplementedError("AirPlay-Lautsprecher bekommen nur den Systemton")

    def supports(self, feature: str) -> bool:
        return feature == Feature.SOUND
