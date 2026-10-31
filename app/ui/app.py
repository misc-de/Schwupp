"""Adw.Application – hält die langlebigen Dienste (Config, HTTP-Server, D-Bus-Dienst)."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from .. import APP_ID  # noqa: E402
from ..config import Config  # noqa: E402
from ..i18n import t  # noqa: E402
from ..server.httpserver import MediaServer  # noqa: E402
from ..service import CastService  # noqa: E402
from .window import MainWindow  # noqa: E402

# So lange bleibt die App im Dienstmodus nach dem letzten Aufruf noch stehen.
SERVICE_IDLE_MS = 30_000


class SchwuppApp(Adw.Application):
    def __init__(self, missing_optional=None) -> None:  # noqa: ANN001
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._missing_optional = list(missing_optional or [])
        self.config = Config()
        # Server für lokale Dateien, HLS und Live-Streams. Er lauscht auf allen
        # Interfaces; welche IP das jeweilige Gerät ansprechen muss, entscheidet
        # der Server beim Registrieren pro Gerät (siehe MediaServer.base_url).
        self.server = MediaServer()
        self.server.start()
        # Spiegeln ohne Fenster (Schnellschalter der Shell, siehe service.py)
        self.service = CastService(self)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # Im Dienstmodus (``--service``) gibt es kein Fenster, das die App hält:
        # Sie läuft, solange die Shell sucht oder gespiegelt wird, und noch kurz
        # danach – ein erneutes Öffnen der Liste muss sie dann nicht neu starten.
        if self.get_flags() & Gio.ApplicationFlags.IS_SERVICE:
            self.set_inactivity_timeout(SERVICE_IDLE_MS)

    def do_dbus_register(self, connection, object_path) -> bool:  # noqa: ANN001
        if not Adw.Application.do_dbus_register(self, connection, object_path):
            return False
        self.service.register(connection)
        return True

    def do_dbus_unregister(self, connection, object_path) -> None:  # noqa: ANN001
        self.service.unregister()
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_activate(self) -> None:
        win = self.props.active_window or MainWindow(self)
        win.present()
        if self._missing_optional:
            self._warn_optional(win)
            self._missing_optional = []  # nur einmal nachfragen

    def open_device_settings(self, info) -> None:  # noqa: ANN001
        """Fenster zeigen und die Einstellungen von *info* öffnen (None = allgemein).

        Aufruf aus der Shell („Streaming-Einstellungen“): Auflösung, Bitrate und
        Ton gelten pro Gerät und lassen sich so ohne Verbindung ändern.
        """
        self.activate()
        win = self.props.active_window
        if win is not None:
            win.open_device_settings(info)

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
