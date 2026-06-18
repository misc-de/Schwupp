"""LG-webOS-Backend.

Steuerung, App-Start und YouTube laufen über die webOS-WebSocket-API
(``pywebostv``, wss auf Port 3001 mit gespeichertem client_key). Lokale Medien /
URLs / HLS werden über DLNA-AVTransport (:mod:`app.dlna`) im TV-Player geöffnet,
weil die öffentliche webOS-API kein direktes „spiele URL ab" bietet.

Erst-Pairing: Beim ersten Verbinden ohne gespeicherten Key erscheint am TV ein
Bestätigungsdialog; ``connect`` wartet darauf. Der Key wird in der Config
abgelegt, danach verbindet sich die App ohne Rückfrage.
"""
from __future__ import annotations

from ..dlna import DlnaRenderer
from .base import Feature, Receiver

# Bildschirmspiegelung auf LG webOS ist von Linux aus (Stand: gründlich erprobt)
# nicht sauber lösbar – daher KEIN Mirror-Feature für webOS:
#   * DLNA-Live (TS/fMP4/WebM, auch mit fake-size): TV verwirft jeden
#     größenlosen Live-Stream (Chrome-Player braucht indizierte Datei).
#   * TV-Browser (HLS via hls.js): com.webos.app.browser ignoriert die Ziel-URL.
#   * AirPlay: erfordert FairPlay v3/v4 (Apples proprietärer Code) – legacy aus.
#   * Miracast: WFD-Discovery + GO-Negotiation gelingen, aber die P2P-Gruppe
#     ist unter NetworkManager nicht stabil zu halten (siehe docs/MIRRORING.md).
# Bildschirmspiegelung zuverlässig nur über ein Chromecast-/Google-TV-Gerät.
_FEATURES = {
    Feature.MEDIA, Feature.YOUTUBE, Feature.PLAYBACK, Feature.VOLUME,
}

YOUTUBE_APP = "youtube.leanback.v4"


class WebosReceiver(Receiver):
    kind = "webos"

    def __init__(self, info, context) -> None:  # noqa: ANN001
        super().__init__(info, context)
        self._client = None
        self._app = None
        self._media = None
        self._sys = None
        self._dlna = DlnaRenderer(info.host)

    # -- Verbindung -----------------------------------------------------------
    def connect(self, prompt_cb=None) -> None:  # noqa: ANN001
        from pywebostv.connection import WebOSClient
        from pywebostv.controls import (ApplicationControl, MediaControl,
                                        SystemControl)

        keys = self.context.config["webos_keys"] or {}
        store = {"client_key": keys[self.host]} if self.host in keys else {}

        client = WebOSClient(self.host, secure=True)
        client.connect()
        for status in client.register(store, timeout=60):
            if status == WebOSClient.PROMPTED and prompt_cb:
                prompt_cb()
        keys[self.host] = store.get("client_key")
        self.context.config["webos_keys"] = keys
        self.context.config.save()

        self._client = client
        self._app = ApplicationControl(client)
        self._media = MediaControl(client)
        self._sys = SystemControl(client)
        # DLNA-Renderer für Medien-URLs vorbereiten (best effort)
        self._dlna.resolve()

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    # -- Inhalte --------------------------------------------------------------
    def play_media(self, url, mime, *, title=None, live=False) -> None:  # noqa: ANN001
        if self._dlna._control is None:
            self._dlna.resolve()
        self._dlna.play_url(url, mime, title or "Schwupp")

    def play_youtube(self, video_id: str) -> None:
        apps = self._app.list_apps()
        yt = next((a for a in apps if a["id"] == YOUTUBE_APP), None)
        if yt is None:
            raise RuntimeError("YouTube-App auf dem TV nicht gefunden")
        self._app.launch(yt, params={"contentTarget": f"v={video_id}"})

    # -- Steuerung ------------------------------------------------------------
    def play(self) -> None:
        if self._media:
            self._media.play()

    def pause(self) -> None:
        if self._media:
            self._media.pause()

    def stop(self) -> None:
        if self._media:
            try:
                self._media.stop()
            except Exception:  # noqa: BLE001
                pass
        self._dlna.stop()

    def set_volume(self, level: float) -> None:
        if self._media:
            self._media.set_volume(int(max(0.0, min(1.0, level)) * 100))

    def supports(self, feature: str) -> bool:
        return feature in _FEATURES

    def notify(self, text: str) -> None:
        """Kleine Bildschirm-Nachricht am TV (für Status/Hinweise)."""
        if self._sys:
            try:
                self._sys.notify(text)
            except Exception:  # noqa: BLE001
                pass
