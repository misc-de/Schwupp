"""Adw.Application – hält die langlebigen Dienste (Config, HTTP-Server)."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from .. import APP_ID  # noqa: E402
from ..config import Config  # noqa: E402
from ..net import lan_ip  # noqa: E402
from ..server.httpserver import MediaServer  # noqa: E402
from .window import MainWindow  # noqa: E402


class SchwuppApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.config = Config()
        # Server für lokale Dateien + HLS; an die LAN-IP gebunden, damit das
        # Cast-Gerät ihn erreicht.
        self.server = MediaServer(lan_ip())
        self.server.start()

    def do_activate(self) -> None:
        win = self.props.active_window or MainWindow(self)
        win.present()

    def do_shutdown(self) -> None:
        try:
            self.server.stop()
        finally:
            Adw.Application.do_shutdown(self)
