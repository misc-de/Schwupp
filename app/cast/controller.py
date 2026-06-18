"""Verbindung zu einem Cast-Gerät und Wiedergabe-Steuerung.

Kapselt pychromecast: verbinden, Medien abspielen, play/pause/stop/seek/Lautstärke
und Statusmeldungen. Die Status-Callbacks kommen aus dem pychromecast-Socket-Thread
und müssen von der UI per ``GLib.idle_add`` gemarshallt werden.
"""
from __future__ import annotations

from typing import Callable, Optional

import pychromecast
from pychromecast.controllers.media import MediaStatusListener
from pychromecast.controllers.receiver import CastStatusListener


class _MediaBridge(MediaStatusListener):
    def __init__(self, cb: Callable[[object], None]) -> None:
        self._cb = cb

    def new_media_status(self, status) -> None:  # noqa: ANN001
        self._cb(status)

    def load_media_failed(self, item: int, error_code: int) -> None:
        self._cb(None)


class _CastBridge(CastStatusListener):
    def __init__(self, cb: Callable[[object], None]) -> None:
        self._cb = cb

    def new_cast_status(self, status) -> None:  # noqa: ANN001
        self._cb(status)


class CastSession:
    """Aktive Verbindung zu genau einem Gerät."""

    def __init__(self, cast_info=None, zconf=None, *, host=None, port=8009,
                 name=None, model=None) -> None:  # noqa: ANN001
        # Zwei Modi: per mDNS-CastInfo ODER direkt per Host (versteckte
        # Cast-Receiver wie LG-TVs, die sich nicht via _googlecast ankündigen).
        self._info = cast_info
        self._zconf = zconf
        self._host = host
        self._port = port
        self._name = name
        self._model = model
        self._cc: Optional[pychromecast.Chromecast] = None

    # -- Verbindung -----------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> None:
        if self._info is not None:
            cc = pychromecast.get_chromecast_from_cast_info(self._info, self._zconf)
        else:
            cc = pychromecast.get_chromecast_from_host(
                (self._host, self._port, None, self._model, self._name)
            )
        cc.wait(timeout=timeout)
        self._cc = cc

    def disconnect(self) -> None:
        if self._cc is not None:
            try:
                self._cc.disconnect()
            finally:
                self._cc = None

    @property
    def connected(self) -> bool:
        return self._cc is not None

    # -- Status-Listener ------------------------------------------------------
    def on_media_status(self, cb: Callable[[object], None]) -> None:
        assert self._cc is not None
        self._cc.media_controller.register_status_listener(_MediaBridge(cb))

    def on_cast_status(self, cb: Callable[[object], None]) -> None:
        assert self._cc is not None
        self._cc.register_status_listener(_CastBridge(cb))

    # -- Wiedergabe -----------------------------------------------------------
    def play_media(
        self,
        url: str,
        mime: str,
        *,
        title: str | None = None,
        thumb: str | None = None,
        live: bool = False,
    ) -> None:
        assert self._cc is not None, "Nicht verbunden"
        # Eine hängende Mirroring-App blockiert den Default Media Receiver -> beenden.
        import time

        status = self._cc.status
        if status and status.app_id in ("0F5096E8", "674A0243"):
            self._cc.quit_app()
            time.sleep(2)
        self._cc.media_controller.play_media(
            url,
            mime,
            title=title,
            thumb=thumb,
            stream_type="LIVE" if live else "BUFFERED",
        )
        self._cc.media_controller.block_until_active(timeout=10)

    def play(self) -> None:
        if self._cc:
            self._cc.media_controller.play()

    def pause(self) -> None:
        if self._cc:
            self._cc.media_controller.pause()

    def stop(self) -> None:
        if self._cc:
            self._cc.media_controller.stop()

    def seek(self, seconds: float) -> None:
        if self._cc:
            self._cc.media_controller.seek(seconds)

    def quit_app(self) -> None:
        if self._cc:
            self._cc.quit_app()

    def play_youtube(self, video_id: str) -> None:
        """Startet die native YouTube-App auf dem Gerät und spielt *video_id*."""
        assert self._cc is not None, "Nicht verbunden"
        from pychromecast.controllers.youtube import YouTubeController

        yt = YouTubeController()
        self._cc.register_handler(yt)
        yt.play_video(video_id)

    def set_volume(self, level: float) -> None:
        """Lautstärke 0.0–1.0."""
        if self._cc:
            self._cc.set_volume(max(0.0, min(1.0, level)))

    # -- Roh-Zugriff (für Engines, die eigene Controller registrieren) --------
    @property
    def chromecast(self) -> pychromecast.Chromecast:
        assert self._cc is not None, "Nicht verbunden"
        return self._cc
