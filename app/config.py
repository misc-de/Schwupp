"""Einfache, persistente Konfiguration (JSON unter XDG_CONFIG_HOME/schwupp)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULTS: dict[str, Any] = {
    # Welche Engine fürs Bildschirm-Spiegeln genutzt wird:
    #   "native"     – eigenes Cast-Streaming (RTP, geringe Latenz) [Ziel]
    #   "hls"        – GStreamer -> HLS -> Default Media Receiver (robust, träge)
    #   "openscreen" – externes openscreen cast_sender-Binary
    # "native" = echtes Cast-Streaming (<1 s, live bestätigt); "hls" = robuster
    # Fallback (~7 s). "dlnats"/"openscreen" sind weitere Alternativen.
    "mirror_engine": "native",
    # Video-Parameter fürs Spiegeln
    "mirror_bitrate_kbps": 6000,
    "mirror_fps": 30,
    "mirror_height": 1080,   # 16:9-Zielhöhe (Breite wird daraus berechnet)
    "mirror_target_delay_ms": 150,  # Playout-Puffer am Receiver (native Engine)
    # Pfad zum openscreen cast_sender-Binary (nur für Engine "openscreen")
    "openscreen_sender_path": "",
    # Zuletzt genutztes Gerät (UUID) – für Auto-Reconnect-Komfort
    "last_device_uuid": "",
    # Zeitpunkt der letzten Update-Prüfung (ISO-String, vom Updater gesetzt)
    "last_update_check": "",
    # Gespeicherte webOS-client_keys je TV-Host: {"192.168.0.33": "<key>"}
    "webos_keys": {},
    # Gerätespezifische Overrides der DEVICE_KEYS je Geräte-UUID:
    #   {"cast:…": {"mirror_engine": "hls", "mirror_bitrate_kbps": 4000}}
    "device_overrides": {},
}

# Diese Schlüssel sind pro Gerät überschreibbar; sonst gilt der globale Default.
DEVICE_KEYS = (
    "mirror_engine", "mirror_bitrate_kbps", "mirror_fps",
    "mirror_height", "mirror_target_delay_ms",
)


def device_keys(info: Any) -> list[str]:
    """Kandidaten-Schlüssel für Geräte-Overrides eines ReceiverInfo.

    Die uuid eines Cast-Geräts ist nicht stabil: Es kann über den CastBrowser
    (echte ``cast:<uuid>``) ODER über den SSDP/AirPlay-Fallback (``cast:<host>``)
    erscheinen. Damit Geräte-Einstellungen über einen solchen Pfad-Wechsel hinweg
    erhalten bleiben, werden beide Schlüssel (uuid zuerst, dann host) gepflegt
    und gelesen.
    """
    keys: list[str] = []
    uuid = getattr(info, "uuid", None)
    if uuid:
        keys.append(uuid)
    host = getattr(info, "host", None)
    # Prefix aus der uuid ableiten ("cast"/"webos"/"dlna") – NICHT aus ``kind``:
    # bei Cast ist die uuid ``cast:…``, das kind aber ``chromecast`` (Discovery
    # setzt ``cast:<host>`` als host-Fallback, siehe discovery.py).
    if uuid and host and ":" in uuid:
        host_key = f"{uuid.split(':', 1)[0]}:{host}"
        if host_key not in keys:
            keys.append(host_key)
    return keys


def _config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "schwupp" / "config.json"


class Config:
    """Dict-ähnlicher Zugriff mit Defaults und Speichern auf Wunsch."""

    def __init__(self) -> None:
        self._path = _config_path()
        self._data: dict[str, Any] = dict(_DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            with self._path.open(encoding="utf-8") as fh:
                self._data.update(json.load(fh))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        tmp.replace(self._path)

    def __getitem__(self, key: str) -> Any:
        return self._data.get(key, _DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    # -- Gerätespezifische Werte (mit globalem Default als Fallback) ----------
    def device_value(self, uuid: str, key: str) -> Any:
        ov = (self._data.get("device_overrides") or {}).get(uuid, {})
        return ov[key] if key in ov else self[key]

    def set_device_value(self, uuid: str, key: str, value: Any) -> None:
        devs = self._data.setdefault("device_overrides", {})
        devs.setdefault(uuid, {})[key] = value

    # -- Robuster Zugriff über mehrere Geräte-Schlüssel (uuid + host) ----------
    def device_value_for(self, info: Any, key: str) -> Any:
        """Geräte-Override über alle :func:`device_keys` (uuid bevorzugt)."""
        overrides = self._data.get("device_overrides") or {}
        for dev_key in device_keys(info):
            ov = overrides.get(dev_key, {})
            if key in ov:
                return ov[key]
        return self[key]

    def set_device_value_for(self, info: Any, key: str, value: Any) -> None:
        """Override unter allen :func:`device_keys` setzen (überlebt uuid-Wechsel)."""
        for dev_key in device_keys(info):
            self.set_device_value(dev_key, key, value)
