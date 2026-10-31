"""Sonos: DLNA mit drei Besonderheiten (aus Emilia übernommen).

* Ein endloser MP3-Stream läuft nur unter Sonos' Radioschema
  ``x-rincon-mp3radio://`` – als normale ``http://``-URL bricht er ab.
* Gesteuert wird der Koordinator einer Raumgruppe; die Discovery listet nur
  ihn (siehe ``discovery._emit_sonos``).
* Die Lautstärke gilt für die ganze Gruppe (GroupRenderingControl).
"""
from __future__ import annotations

import contextlib

from ..dlna import DlnaRenderer
from .base import Feature, Receiver

GROUP_RENDERING = "urn:schemas-upnp-org:service:GroupRenderingControl:1"

_FEATURES = {Feature.MEDIA, Feature.PLAYBACK, Feature.VOLUME, Feature.SOUND}


class SonosReceiver(Receiver):
    kind = "sonos"

    def __init__(self, info, context) -> None:  # noqa: ANN001
        super().__init__(info, context)
        self._sonos = info.raw            # discovery.SonosInfo
        self._dlna = DlnaRenderer(info.host, control=self._sonos.avtransport)

    def connect(self, prompt_cb=None, pin_cb=None) -> None:  # noqa: ANN001
        if not self._dlna.ensure_ready():
            raise RuntimeError("Sonos antwortet nicht (AVTransport)")

    def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            self._dlna.stop()

    def play_media(self, url, mime, *, title=None, live=False) -> None:  # noqa: ANN001
        uri = None
        if live and mime == "audio/mpeg":
            uri = url.replace("http://", "x-rincon-mp3radio://", 1)
        self._dlna.play_url(url, mime, title or "Schwupp", live=live, uri=uri)

    def play(self) -> None:
        self._dlna.play()

    def pause(self) -> None:
        self._dlna.pause()

    def stop(self) -> None:
        self._dlna.stop()

    def set_volume(self, level: float) -> None:
        """Lautstärke der ganzen Raumgruppe, sonst die des Raums."""
        if not self._sonos.group_rendering:
            self._dlna.set_volume(level)
            return
        vol = int(max(0.0, min(1.0, level)) * 100)
        with contextlib.suppress(OSError):
            self._dlna._soap(  # noqa: SLF001 – gleicher SOAP-Weg, anderer Dienst
                "SetGroupVolume",
                f'<u:SetGroupVolume xmlns:u="{GROUP_RENDERING}"><InstanceID>0</InstanceID>'
                f"<DesiredVolume>{vol}</DesiredVolume></u:SetGroupVolume>",
                service=GROUP_RENDERING, control=self._sonos.group_rendering, timeout=4,
            )

    def supports(self, feature: str) -> bool:
        return feature in _FEATURES
