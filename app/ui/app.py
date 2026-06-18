"""Adw.Application – hält die langlebigen Dienste (Config, HTTP-Server)."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from .. import APP_ID  # noqa: E402
from ..config import Config  # noqa: E402
from ..i18n import t  # noqa: E402
from ..net import lan_ip  # noqa: E402
from ..server.httpserver import MediaServer  # noqa: E402
from .window import MainWindow  # noqa: E402


class SchwuppApp(Adw.Application):
    def __init__(self, missing_optional=None) -> None:  # noqa: ANN001
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._missing_optional = list(missing_optional or [])
        self.config = Config()
        # Server für lokale Dateien + HLS; an die LAN-IP gebunden, damit das
        # Cast-Gerät ihn erreicht.
        self.server = MediaServer(lan_ip())
        self.server.start()

    def do_activate(self) -> None:
        win = self.props.active_window or MainWindow(self)
        win.present()
        if self._missing_optional:
            self._warn_optional(win)
            self._missing_optional = []  # nur einmal nachfragen

    # -- Hinweis auf fehlende optionale Komponenten (mit Weiter/Beenden-Wahl) --
    def _warn_optional(self, win) -> None:  # noqa: ANN001
        lines = "\n".join(f"  •  {d.package} — {t(d.feature_key)}"
                          for d in self._missing_optional)
        dialog = Adw.AlertDialog(
            heading=t("deps.optional.title"),
            body=t("deps.optional.body", list=lines),
        )
        dialog.add_response("quit", t("deps.quit"))
        dialog.add_response("start", t("deps.optional.start"))
        dialog.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("start")
        dialog.set_close_response("start")
        dialog.connect("response", self._on_optional_response)
        dialog.present(win)

    def _on_optional_response(self, _dialog, response: str) -> None:  # noqa: ANN001
        if response == "quit":
            self.quit()

    def do_shutdown(self) -> None:
        try:
            self.server.stop()
        finally:
            Adw.Application.do_shutdown(self)
