"""Schwupp – Casten auf Chromecast/Google-TV, LG webOS, AirPlay und DLNA.

Pakete:
  cast/    – Geräte-Discovery und Steuerung (pychromecast)
  server/  – lokaler HTTP-Server (lokale Dateien + HLS-Segmente ausliefern)
  sources/ – Quellen (YouTube, …)
  mirror/  – austauschbare Bildschirm-Spiegel-Engines (native/hls/dlnats)
  ui/      – GTK4/libadwaita-Oberfläche (adaptiv: Desktop & Phosh-Phone)
"""

from .paths import data_file as _data_file

APP_ID = "de.cais.Schwupp"
APP_NAME = "Schwupp"


def _read_version() -> str:
    """Version aus der VERSION-Datei (Single Source of Truth, auch vom Updater
    zum Versionsvergleich genutzt). Der Pfad wird über :mod:`app.paths` gesucht,
    damit Git-Klon, Flatpak und pip-Installation gleichermaßen funktionieren."""
    try:
        return _data_file("VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


VERSION = _read_version()
