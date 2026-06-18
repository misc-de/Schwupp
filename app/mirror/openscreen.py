"""openscreen-Engine: nutzt Googles ``cast_sender``-Binary als Hilfsprozess.

Echtes Low-Latency-Mirroring, ohne das Protokoll selbst zu implementieren –
allerdings muss das ``cast_sender``-Binary aus dem openscreen-Projekt
(https://github.com/chromium/openscreen) vorhanden und in den Einstellungen
hinterlegt sein. Der Bildschirm wird per GStreamer aufgenommen und an das
Binary weitergereicht.

Wird nach der nativen Engine verdrahtet; hier steht bereits die Verfügbarkeits-
prüfung, damit die Engine-Auswahl in den Einstellungen vollständig ist.
"""
from __future__ import annotations

import os

from .engine import MirrorEngine


class OpenscreenMirrorEngine(MirrorEngine):
    name = "openscreen"
    display_name = "openscreen (externes Binary)"

    @staticmethod
    def check_available() -> tuple[bool, str]:
        from ..config import Config

        path = Config()["openscreen_sender_path"]
        if not path:
            return False, "Pfad zum cast_sender-Binary in den Einstellungen setzen"
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            return False, f"cast_sender nicht ausführbar: {path}"
        return True, "Bereit"

    def start(self) -> None:
        raise NotImplementedError(
            "Die openscreen-Engine ist noch nicht verdrahtet. "
            "Bitte vorerst die HLS-Engine wählen."
        )

    def stop(self) -> None:
        self._running = False
