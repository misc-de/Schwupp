"""Schwupp – Casten auf Chromecast/Google-TV unter Linux.

Pakete:
  cast/    – Geräte-Discovery und Steuerung (pychromecast)
  server/  – lokaler HTTP-Server (lokale Dateien + HLS-Segmente ausliefern)
  sources/ – Quellen (YouTube, …)
  mirror/  – austauschbare Bildschirm-Spiegel-Engines (native/hls/openscreen)
  ui/      – GTK4/libadwaita-Oberfläche (adaptiv: Desktop & Phosh-Phone)
"""

from pathlib import Path as _Path

APP_ID = "de.cais.Schwupp"
APP_NAME = "Schwupp"

# Version aus der VERSION-Datei im Projekt-Root (Single Source of Truth, auch
# vom Updater zum Versionsvergleich genutzt). Fallback, falls Datei fehlt.
_VERSION_FILE = _Path(__file__).parent.parent / "VERSION"
VERSION = _VERSION_FILE.read_text(encoding="utf-8").strip() if _VERSION_FILE.exists() else "0.1.0"
