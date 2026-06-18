"""Chromecast-/Google-TV-Backend (auf Basis von pychromecast)."""
from __future__ import annotations

from ..cast.controller import CastSession
from .base import Feature, Receiver

_FEATURES = {
    Feature.MEDIA, Feature.YOUTUBE, Feature.PLAYBACK, Feature.VOLUME,
    Feature.MIRROR_HLS, Feature.MIRROR_NATIVE,
}


class ChromecastReceiver(Receiver):
    kind = "chromecast"

    def __init__(self, info, context) -> None:  # noqa: ANN001
        super().__init__(info, context)
        if info.raw is not None:
            # echtes Cast-Gerät aus mDNS (CastInfo)
            self._session = CastSession(info.raw, context.zconf)
        else:
            # versteckter Cast-Receiver (per Host, z. B. LG-TV auf 8009)
            self._session = CastSession(host=info.host, port=info.port or 8009,
                                        name=info.name, model=info.model)

    def connect(self, prompt_cb=None) -> None:  # noqa: ANN001
        self._session.connect()

    def disconnect(self) -> None:
        self._session.disconnect()

    def play_media(self, url, mime, *, title=None, live=False) -> None:  # noqa: ANN001
        self._session.play_media(url, mime, title=title, live=live)

    def play_youtube(self, video_id: str) -> None:
        self._session.play_youtube(video_id)

    def play(self) -> None:
        self._session.play()

    def pause(self) -> None:
        self._session.pause()

    def stop(self) -> None:
        self._session.stop()

    def set_volume(self, level: float) -> None:
        self._session.set_volume(level)

    def supports(self, feature: str) -> bool:
        return feature in _FEATURES

    @property
    def session(self) -> CastSession:
        """Roh-Session – von der nativen Cast-Streaming-Mirror-Engine genutzt."""
        return self._session
