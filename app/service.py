"""Spiegeln und Ton senden ohne Fenster: D-Bus-Schnittstelle für Schnellschalter.

Eine Shell (z. B. die Cast-Kachel von Phosh) zeigt Fernseher und Lautsprecher in
einer Liste. Ein Tipp auf einen Fernseher spiegelt den Bildschirm, ein Tipp auf
einen Lautsprecher schickt den ganzen Ton des Rechners dorthin (speaker/); ein
zweiter Tipp beendet es. Die Einstellungen (Standardwerte und pro Gerät:
Auflösung, Bitrate, Ton …) lassen sich über ``GetSettings``/``SetSetting`` von
außen lesen und ändern, etwa aus den GNOME-Einstellungen – dieselbe Config,
dieselben Engines, nur ohne Fenster. Die Config bleibt so allein in diesem
Prozess; niemand sonst schreibt die Datei, während der Dienst sie im Speicher hält.

Die Schnittstelle hängt am Bus-Namen der App (``de.cais.Schwupp``), Objekt
``/de/cais/Schwupp/Cast``. Ohne laufende App startet D-Bus sie im Dienstmodus
(``schwupp --service``): Dann gibt es kein Fenster, und die App beendet sich
von selbst, sobald weder gesucht noch gespiegelt wird. Öffnet man Schwupp
währenddessen, erscheint das Fenster im selben Prozess.

Gesucht wird nur, solange ein Aufrufer ``StartDiscovery`` hält (Akku); fällt
der Aufrufer vom Bus, endet seine Suche mit ihm.

Desktop auf dem Fernseher (``ConnectDesktop``): Gibt es auf dem Rechner den
Dienst ``plinius-desktop-cast.service`` (Plinius-Telefon, Paket plinius-desktop),
startet Schwupp ihn vor dem Spiegeln. Er legt einen virtuellen Bildschirm mit
Leiste an; Schwupp schreibt dessen Namen in den Einwilligungsstempel der
Bildschirmauswahl (xdg-desktop-portal-wlr-plinius), die dann genau diesen
Bildschirm teilt statt des Telefons. Endet das Spiegeln, endet der Dienst, und
die Fenster kehren aufs Telefon zurück.

Videos und Links (``PlayFile``, ``PlayLink``; ``ChooseFile`` und ``ChooseLink``
fragen selbst nach Datei bzw. Link): Das Gerät spielt eine Datei dieses
Rechners (über Schwupps Medienserver) oder einen Link – YouTube in der
YouTube-App des Geräts, sonst über yt-dlp. ``Actions`` sagt je Gerät, was geht.
"""
from __future__ import annotations

import mimetypes
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .config import DEVICE_KEYS  # noqa: E402
from .discovery import Discovery, ReceiverInfo  # noqa: E402
from .i18n import t  # noqa: E402
from .mirror import engines_for_kind, get_engine_class  # noqa: E402
from .mirror.capture import ENCODER_CHOICES, available_encoders  # noqa: E402
from .receivers import Feature, create_receiver, static_features  # noqa: E402
from .receivers.base import Context  # noqa: E402
from .sources import youtube  # noqa: E402
from .speaker import SoundCast, is_speaker  # noqa: E402
from .speaker import check_available as sound_available  # noqa: E402

OBJECT_PATH = "/de/cais/Schwupp/Cast"
INTERFACE = "de.cais.Schwupp.Cast1"

_XML = f"""
<node>
  <interface name="{INTERFACE}">
    <method name="StartDiscovery"/>
    <method name="StopDiscovery"/>
    <method name="Connect">
      <arg name="id" type="s" direction="in"/>
    </method>
    <!-- Wie Connect, aber der Fernseher zeigt einen eigenen Desktop statt des
         Telefonbildschirms (nur mit DesktopAvailable, nur Fernseher) -->
    <method name="ConnectDesktop">
      <arg name="id" type="s" direction="in"/>
    </method>
    <!-- Eine Videodatei dieses Rechners auf dem Gerät abspielen -->
    <method name="PlayFile">
      <arg name="id" type="s" direction="in"/>
      <arg name="path" type="s" direction="in"/>
    </method>
    <!-- Einen Link abspielen: YouTube in der YouTube-App des Geräts, sonst yt-dlp -->
    <method name="PlayLink">
      <arg name="id" type="s" direction="in"/>
      <arg name="url" type="s" direction="in"/>
    </method>
    <!-- Wie PlayFile/PlayLink, aber Schwupp fragt selbst nach Datei bzw. Link -->
    <method name="ChooseFile">
      <arg name="id" type="s" direction="in"/>
    </method>
    <method name="ChooseLink">
      <arg name="id" type="s" direction="in"/>
    </method>
    <method name="Disconnect"/>
    <method name="SubmitPin">
      <arg name="pin" type="s" direction="in"/>
    </method>
    <method name="OpenSettings">
      <arg name="id" type="s" direction="in"/>
    </method>
    <!-- Einstellungen eines Geräts, id "" = Standardwerte für alle Geräte.
         Schlüssel: die Werte aus config.DEVICE_KEYS, dazu "name", "kind",
         "custom" (b, eigene Werte), "engines" (a(ssbs): Name, Anzeige,
         verfügbar, Grund), "encoders" (as), "resolutions" (ai) -->
    <method name="GetSettings">
      <arg name="id" type="s" direction="in"/>
      <arg name="settings" type="a{{sv}}" direction="out"/>
    </method>
    <method name="SetSetting">
      <arg name="id" type="s" direction="in"/>
      <arg name="key" type="s" direction="in"/>
      <arg name="value" type="v" direction="in"/>
    </method>
    <!-- Eigene Werte des Geräts löschen, es gelten wieder die Standardwerte -->
    <method name="ResetDevice">
      <arg name="id" type="s" direction="in"/>
    </method>
    <!-- Gerät samt eigenen Werten vergessen -->
    <method name="ForgetDevice">
      <arg name="id" type="s" direction="in"/>
    </method>
    <!-- (id, Name, Beschreibung, Symbolname) je Gerät, das spiegeln kann -->
    <property name="Devices" type="a(ssss)" access="read"/>
    <property name="ActiveDevice" type="s" access="read"/>
    <!-- idle | connecting | pin | starting | streaming -->
    <property name="State" type="s" access="read"/>
    <property name="Error" type="s" access="read"/>
    <property name="Discovering" type="b" access="read"/>
    <!-- Fernseher mit Einstellungen oder schon einmal gespiegelt, auch wenn sie
         gerade nicht gefunden werden: (id, Name, Beschreibung, Symbolname) -->
    <property name="KnownDevices" type="a(ssss)" access="read"/>
    <!-- Ob ConnectDesktop hier geht (plinius-desktop-cast.service vorhanden) -->
    <property name="DesktopAvailable" type="b" access="read"/>
    <!-- Was das aktive Gerät bekommt: "" (nichts), "screen", "desktop", "video"
         (Datei) oder "link" -->
    <property name="Source" type="s" access="read"/>
    <!-- Was je Gerät geht: id -> Auswahl aus "mirror", "youtube", "video", "desktop"
         (in dieser Reihenfolge) -->
    <property name="Actions" type="a{{sas}}" access="read"/>
  </interface>
</node>
"""

_KIND_ICON = {"chromecast": "video-display-symbolic", "webos": "tv-symbolic",
              "airplay": "tv-symbolic", "dlna": "tv-symbolic"}
SPEAKER_ICON = "audio-speakers-symbolic"

# So lange wartet ein Verbindungsaufbau auf die PIN vom Fernseher.
PIN_TIMEOUT_S = 180

# Desktop auf dem Fernseher (plinius-desktop)
DESKTOP_UNIT = "plinius-desktop-cast.service"
DESKTOP_UNIT_DIRS = ("/usr/lib/systemd/user", "/etc/systemd/user")
# Bis der virtuelle Bildschirm steht (der Dienst meldet sich per sd_notify)
DESKTOP_START_TIMEOUT_S = 30


def _runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def desktop_available() -> bool:
    """Gibt es hier einen Desktop für den Fernseher?"""
    return any(Path(d, DESKTOP_UNIT).is_file() for d in DESKTOP_UNIT_DIRS)


def start_desktop() -> str:
    """Startet den Desktop-Dienst und gibt den Namen seines Bildschirms zurück.

    Blockiert bis zum Ende des Start-Jobs; läuft im Worker-Thread.
    """
    try:
        result = subprocess.run(["systemctl", "--user", "start", DESKTOP_UNIT],
                                capture_output=True, text=True,
                                timeout=DESKTOP_START_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"systemctl: {result.returncode}")
    try:
        name = (_runtime_dir() / "plinius-desktop-screen" / "output").read_text().strip()
    except OSError:
        name = ""
    if not name:
        raise RuntimeError(t("service.no_desktop_screen"))
    return name


def stop_desktop() -> None:
    """Beendet den Desktop-Dienst (wartet nicht darauf)."""
    try:
        subprocess.run(["systemctl", "--user", "stop", "--no-block", DESKTOP_UNIT],
                       capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[service] Desktop ließ sich nicht beenden: {exc}")


def write_consent(screen: str) -> None:
    """Sagt der Bildschirmauswahl, welcher Bildschirm geteilt wird (gilt 15 s, einmal)."""
    path = _runtime_dir() / "plinius-screencast-consent"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(screen)

# Wählbare Zielhöhen (16:9) und Grenzen für SetSetting – wie im Einstellungs-Dialog
RESOLUTIONS = (720, 1080, 2160)
_INT_RANGES = {"mirror_bitrate_kbps": (1000, 20000), "mirror_fps": (10, 60),
               "mirror_target_delay_ms": (0, 2000)}


def can_mirror(info: ReceiverInfo) -> bool:
    """Kommt das Gerät als Fernseher (Spiegeln) in Frage?

    Cast-Lautsprecher und -Gruppen melden sich wie Fernseher, haben aber kein
    Bild; sie stehen als Lautsprecher in der Liste.
    """
    if is_speaker(info):
        return False
    return bool(engines_for_kind(info.kind))


def device_actions(info: ReceiverInfo) -> list[str]:
    """Was ein Fernseher kann: "mirror", "youtube", "video", "desktop".

    Lautsprecher bekommen nichts davon (dort gibt es nur den Ton).
    """
    if is_speaker(info) or not can_mirror(info):
        return []
    features = static_features(info.kind)
    media_video = Feature.MEDIA in features and Feature.VIDEO in features
    actions = ["mirror"]
    if Feature.YOUTUBE in features or (media_video and youtube.yt_dlp_available()):
        actions.append("youtube")
    if media_video:
        actions.append("video")
    if desktop_available():
        actions.append("desktop")
    return actions


def is_listed(info: ReceiverInfo) -> bool:
    """Fernseher zum Spiegeln oder Lautsprecher für den Ton."""
    return can_mirror(info) or is_speaker(info)


class CastService:
    """Hält Suche, Verbindung und Spiegel-Engine für die D-Bus-Schnittstelle.

    Alle Zustandsänderungen laufen im GLib-Hauptthread; Discovery-, Connect-
    und Engine-Callbacks kommen aus Worker-Threads und werden per
    ``GLib.idle_add`` herübergereicht.
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app
        self._conn: Gio.DBusConnection | None = None
        self._reg_id = 0
        self._discovery: Discovery | None = None
        self._devices: dict[str, ReceiverInfo] = {}
        # Aufrufer, die gerade suchen lassen (eindeutiger Bus-Name -> Watch-ID)
        self._holders: dict[str, int] = {}
        self._held = False
        # Cookie der Leerlauf-Sperre, solange gespiegelt wird (0 = keine)
        self._inhibit = 0
        self.receiver = None
        self.engine = None
        self._active: ReceiverInfo | None = None
        self._state = "idle"
        self._error = ""
        self._pin: str | None = None
        self._pin_event: threading.Event | None = None
        # Desktop auf dem Fernseher: gewünscht, gestartet, Name des Bildschirms
        self._desktop = False
        self._desktop_started = False
        self._desktop_screen = ""
        # Was das aktive Gerät bekommt (Source) und die dafür freigegebene Datei
        self._source = ""
        self._shared_url: str | None = None
        # Beobachter in der App (Fenster), gerufen bei jeder Zustandsänderung
        self._listeners: list[Callable[[], None]] = []

    # ====================================================================
    # D-Bus
    # ====================================================================
    def register(self, connection: Gio.DBusConnection) -> None:
        node = Gio.DBusNodeInfo.new_for_xml(_XML)
        self._conn = connection
        self._reg_id = connection.register_object(
            OBJECT_PATH, node.interfaces[0], self._on_method_call, self._on_get_property, None)

    def unregister(self) -> None:
        self.stop_streaming()
        self._stop_discovery()
        for watch in self._holders.values():
            Gio.bus_unwatch_name(watch)
        self._holders.clear()
        if self._conn is not None and self._reg_id:
            self._conn.unregister_object(self._reg_id)
        self._reg_id = 0
        self._conn = None

    def _on_method_call(self, _conn, sender, _path, _iface, method, params,  # noqa: ANN001
                        invocation) -> None:
        args = params.unpack()
        handlers = {
            "StartDiscovery": lambda: self._hold_discovery(sender),
            "StopDiscovery": lambda: self._release_discovery(sender),
            "Connect": lambda: self.connect(args[0]),
            "ConnectDesktop": lambda: self.connect(args[0], desktop=True),
            "PlayFile": lambda: self.play(args[0], "video", args[1]),
            "PlayLink": lambda: self.play(args[0], "link", args[1]),
            "ChooseFile": lambda: self.choose_file(args[0]),
            "ChooseLink": lambda: self.choose_link(args[0]),
            "Disconnect": self.stop_streaming,
            "SubmitPin": lambda: self.submit_pin(args[0]),
            "OpenSettings": lambda: self.app.open_device_settings(self._devices.get(args[0])),
            "GetSettings": lambda: GLib.Variant("(a{sv})", (self.get_settings(args[0]),)),
            "SetSetting": lambda: self.set_setting(*args),
            "ResetDevice": lambda: self.reset_device(args[0]),
            "ForgetDevice": lambda: self.forget_device(args[0]),
        }
        try:
            result = handlers[method]()
        except LookupError as exc:
            invocation.return_dbus_error(f"{INTERFACE}.Error.NotFound", str(exc))
            return
        except ValueError as exc:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", str(exc))
            return
        invocation.return_value(result if isinstance(result, GLib.Variant) else None)

    def _on_get_property(self, _conn, _sender, _path, _iface, name):  # noqa: ANN001
        return self._property(name)

    def _property(self, name: str) -> GLib.Variant | None:
        if name == "Devices":
            rows = [(info.uuid, info.name, self._describe(info),
                     SPEAKER_ICON if is_speaker(info)
                     else _KIND_ICON.get(info.kind, "video-display-symbolic"))
                    for info in sorted(self._listed(), key=lambda i: i.name.lower())]
            return GLib.Variant("a(ssss)", rows)
        if name == "ActiveDevice":
            return GLib.Variant("s", self._active.uuid if self._active else "")
        if name == "State":
            return GLib.Variant("s", self._state)
        if name == "Error":
            return GLib.Variant("s", self._error)
        if name == "Discovering":
            return GLib.Variant("b", self._discovery is not None)
        if name == "KnownDevices":
            rows = [(info.uuid, info.name, self._describe(info),
                     _KIND_ICON.get(info.kind, "video-display-symbolic"))
                    for info in sorted(self._known(), key=lambda i: i.name.lower())]
            return GLib.Variant("a(ssss)", rows)
        if name == "DesktopAvailable":
            return GLib.Variant("b", desktop_available())
        if name == "Source":
            return GLib.Variant("s", self._source if self._active is not None else "")
        if name == "Actions":
            return GLib.Variant("a{sas}", {info.uuid: device_actions(info)
                                           for info in self._listed()})
        return None

    def _changed(self, *names: str) -> None:
        if "Devices" in names and "Actions" not in names:
            names = (*names, "Actions")       # was geht, hängt an der Geräteliste
        if self._conn is not None:
            changed = {n: self._property(n) for n in names}
            self._conn.emit_signal(
                None, OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (INTERFACE, changed, [])))
        for listener in list(self._listeners):
            listener()

    # ====================================================================
    # Für das Fenster
    # ====================================================================
    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    @property
    def active(self) -> ReceiverInfo | None:
        return self._active

    @property
    def state(self) -> str:
        return self._state

    # ====================================================================
    # Suche
    # ====================================================================
    def _listed(self) -> list[ReceiverInfo]:
        devices = dict(self._devices)
        # Das aktive Gerät bleibt sichtbar, auch wenn die Suche ruht
        if self._active is not None:
            devices.setdefault(self._active.uuid, self._active)
        return [info for info in devices.values() if is_listed(info)]

    @staticmethod
    def _describe(info: ReceiverInfo) -> str:
        if is_speaker(info):
            return t("service.speaker_desc", kind=t(f"device.{info.kind}"))
        return f"{t(f'device.{info.kind}')} · {info.model or info.host}"

    def _hold_discovery(self, sender: str) -> None:
        if sender not in self._holders:
            # Fällt der Aufrufer vom Bus (Shell abgestürzt), endet seine Suche
            self._holders[sender] = Gio.bus_watch_name_on_connection(
                self._conn, sender, Gio.BusNameWatcherFlags.NONE, None,
                lambda _c, name: self._release_discovery(name))
        self._start_discovery()

    def _release_discovery(self, sender: str) -> None:
        watch = self._holders.pop(sender, None)
        if watch is not None:
            Gio.bus_unwatch_name(watch)
        if not self._holders:
            self._stop_discovery()

    def _start_discovery(self) -> None:
        if self._discovery is not None:
            return
        self._discovery = Discovery(
            on_add=lambda info: GLib.idle_add(self._device_added, info),
            on_remove=lambda uuid: GLib.idle_add(self._device_removed, uuid))
        self._discovery.start()
        self._update_hold()
        self._changed("Discovering")

    def _stop_discovery(self) -> None:
        # Die zeroconf-Instanz der Suche trägt auch eine laufende Cast-Sitzung;
        # gestoppt wird erst, wenn nicht mehr gespiegelt wird.
        if self._discovery is None or self.receiver is not None or self._holders:
            return
        discovery, self._discovery = self._discovery, None
        discovery.stop()
        self._devices.clear()
        self._update_hold()
        self._changed("Discovering", "Devices")

    def _device_added(self, info: ReceiverInfo) -> bool:
        if self._discovery is not None:
            self._devices[info.uuid] = info
            self._changed("Devices")
        return False

    def _device_removed(self, uuid: str) -> bool:
        if self._devices.pop(uuid, None) is not None:
            self._changed("Devices")
        return False

    # ====================================================================
    # Einstellungen (für GNOME-Einstellungen u. a., ohne Fenster)
    # ====================================================================
    def _known(self) -> list[ReceiverInfo]:
        """Gemerkte Fernseher als ReceiverInfo (ohne ``raw``)."""
        infos = []
        for uuid, entry in (self.app.config["known_devices"] or {}).items():
            info = ReceiverInfo(kind=entry.get("kind", ""), uuid=uuid,
                                name=entry.get("name") or uuid, host=entry.get("host", ""),
                                port=0, model=entry.get("model", ""))
            if engines_for_kind(info.kind):
                infos.append(info)
        return infos

    def _info_for(self, dev_id: str) -> ReceiverInfo:
        """Gefundenes, aktives oder gemerktes Gerät mit dieser id."""
        if dev_id in self._devices:
            return self._devices[dev_id]
        if self._active is not None and self._active.uuid == dev_id:
            return self._active
        for info in self._known():
            if info.uuid == dev_id:
                return info
        raise LookupError(t("service.unknown_device"))

    def get_settings(self, dev_id: str) -> dict[str, GLib.Variant]:
        cfg = self.app.config
        if dev_id:
            info = self._info_for(dev_id)
            if not can_mirror(info):
                raise LookupError(t("service.unknown_device"))
            value = lambda key: cfg.device_value_for(info, key)  # noqa: E731
        else:
            info = None
            value = lambda key: cfg[key]  # noqa: E731
        out = {
            "mirror_height": GLib.Variant("i", int(value("mirror_height"))),
            "mirror_bitrate_kbps": GLib.Variant("i", int(value("mirror_bitrate_kbps"))),
            "mirror_fps": GLib.Variant("i", int(value("mirror_fps"))),
            "mirror_audio": GLib.Variant("b", bool(value("mirror_audio"))),
            "mirror_encoder": GLib.Variant("s", str(value("mirror_encoder") or "auto")),
            "encoders": GLib.Variant("as", available_encoders()),
            "resolutions": GLib.Variant("ai", list(RESOLUTIONS)),
        }
        if info is not None:
            engines = engines_for_kind(info.kind)
            names = [e.name for e in engines]
            chosen = value("mirror_engine")
            out.update({
                "name": GLib.Variant("s", info.name),
                "kind": GLib.Variant("s", info.kind),
                "description": GLib.Variant("s", self._describe(info)),
                "custom": GLib.Variant("b", cfg.has_device_values(info)),
                "mirror_engine": GLib.Variant("s", chosen if chosen in names else names[0]),
                "engines": GLib.Variant("a(ssbs)", [(e.name, e.display_name, e.available,
                                                     e.detail or "") for e in engines]),
                "airplay_video": GLib.Variant("b", bool(value("airplay_video"))),
            })
        return out

    @staticmethod
    def _checked(key: str, value, engines: list[str] | None):  # noqa: ANN001, ANN205
        """Wert für *key* prüfen und in den Config-Typ bringen (sonst ValueError)."""
        def in_range(low: int, high: int):  # noqa: ANN202
            return lambda v: isinstance(v, int) and not isinstance(v, bool) and low <= v <= high

        valid = {
            "mirror_height": lambda v: v in RESOLUTIONS and not isinstance(v, bool),
            "mirror_audio": lambda v: isinstance(v, bool),
            "airplay_video": lambda v: isinstance(v, bool),
            "mirror_encoder": lambda v: v in ENCODER_CHOICES,
            "mirror_engine": lambda v: engines is not None and v in engines,
            "mirror_airplay_codec": lambda v: v in ("h264", "hevc"),
            **{k: in_range(*r) for k, r in _INT_RANGES.items()},
        }
        check = valid.get(key)
        if check is None or not check(value):
            raise ValueError(f"{key}: {value!r}")
        return value

    def set_setting(self, dev_id: str, key: str, value) -> None:  # noqa: ANN001
        cfg = self.app.config
        if key not in DEVICE_KEYS:
            raise ValueError(f"unknown key {key}")
        if not dev_id:
            if key in ("mirror_engine", "airplay_video"):
                raise ValueError(f"{key} only exists per device")
            cfg[key] = self._checked(key, value, None)
            cfg.save()
            return
        info = self._info_for(dev_id)
        engines = [e.name for e in engines_for_kind(info.kind)]
        cfg.set_device_value_for(info, key, self._checked(key, value, engines))
        cfg.remember_device(info)
        cfg.save()
        self._changed("KnownDevices")

    def reset_device(self, dev_id: str) -> None:
        info = self._info_for(dev_id)
        self.app.config.reset_device_values(info)
        self.app.config.save()

    def forget_device(self, dev_id: str) -> None:
        info = self._info_for(dev_id)
        self.app.config.reset_device_values(info)
        self.app.config.forget_device(info.uuid)
        self.app.config.save()
        self._changed("KnownDevices")

    # ====================================================================
    # Spiegeln
    # ====================================================================
    def connect(self, uuid: str, desktop: bool = False) -> None:
        info = self._devices.get(uuid)
        if info is None and self._active is not None and self._active.uuid == uuid:
            info = self._active
        if info is None:
            raise LookupError(t("service.unknown_device"))
        if desktop and (is_speaker(info) or not can_mirror(info) or not desktop_available()):
            raise ValueError(t("service.no_desktop"))
        self.stop_streaming()
        self._active = info
        self._desktop = desktop
        self._source = "desktop" if desktop else "screen"
        self._error = ""
        self._set_state("connecting")
        self._changed("ActiveDevice", "Error", "Devices", "Source")

        context = Context(zconf=self._discovery.zconf if self._discovery else None,
                          config=self.app.config, server=self.app.server)
        receiver = create_receiver(info, context)
        self.receiver = receiver
        # Ab hier räumt stop_streaming den Desktop-Dienst ab
        self._desktop_started = desktop
        self._update_hold()

        def work() -> None:
            if desktop:
                try:
                    screen = start_desktop()
                except Exception as exc:  # noqa: BLE001
                    GLib.idle_add(self._failed, receiver, t("service.desktop_failed", error=exc))
                    return
                GLib.idle_add(self._desktop_ready, receiver, screen)
            try:
                receiver.connect(prompt_cb=None, pin_cb=self._wait_for_pin)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._failed, receiver, t("window.connect_failed", error=exc))
                return
            GLib.idle_add(self._connected, receiver)

        threading.Thread(target=work, daemon=True, name="schwupp-connect").start()

    def _desktop_ready(self, receiver, screen: str) -> bool:  # noqa: ANN001
        if receiver is self.receiver:
            self._desktop_screen = screen
        elif not self._desktop_started:
            # Abgebrochen, während er startete: Niemand zeigt ihn mehr
            stop_desktop()
        return False

    def _connected(self, receiver) -> bool:  # noqa: ANN001
        if receiver is not self.receiver:
            return False            # inzwischen abgebrochen
        if is_speaker(receiver.info):
            return self._start_sound(receiver)
        name = self._engine_name(receiver)
        if name is None:
            return self._failed(receiver, t("window.unknown_engine"))
        cls = get_engine_class(name)
        ok, detail = cls.check_available()
        if not ok:
            return self._failed(receiver, f"{cls.display_name}: {detail}")
        self.app.config["last_device_uuid"] = receiver.info.uuid
        known_changed = self.app.config.remember_device(receiver.info)
        self.app.config.save()
        if known_changed:
            self._changed("KnownDevices")
        self.engine = cls(receiver, self.app.server, self.app.config,
                          on_error=lambda text: GLib.idle_add(self._failed, receiver, text),
                          on_started=lambda: GLib.idle_add(self._started, receiver))
        self._set_state("starting")
        if self._desktop:
            # Die Bildschirmauswahl teilt diesen Bildschirm, nie ersatzweise das Telefon
            try:
                write_consent(self._desktop_screen)
            except OSError as exc:
                return self._failed(receiver, t("service.desktop_failed", error=exc))
        self.engine.start()
        return False

    def _start_sound(self, receiver) -> bool:  # noqa: ANN001
        """Lautsprecher: den ganzen Ton dieses Rechners dorthin schicken."""
        ok, detail = sound_available(receiver.kind)
        if not ok:
            return self._failed(receiver, detail)
        self.engine = SoundCast(receiver, self.app.server,
                                on_error=lambda text: GLib.idle_add(self._failed, receiver, text),
                                on_started=lambda: GLib.idle_add(self._started, receiver))
        self._set_state("starting")
        self.engine.start()
        return False

    # ====================================================================
    # Videos und Links
    # ====================================================================
    def _lookup(self, uuid: str) -> ReceiverInfo:
        info = self._devices.get(uuid)
        if info is None and self._active is not None and self._active.uuid == uuid:
            info = self._active
        if info is None:
            raise LookupError(t("service.unknown_device"))
        return info

    def play(self, uuid: str, source: str, value: str) -> None:
        """Spielt eine Datei (source "video") oder einen Link ("link") ab."""
        info = self._lookup(uuid)
        action = "video" if source == "video" else "youtube"
        if action not in device_actions(info):
            raise ValueError(t("service.no_media"))
        value = value.strip()
        if source == "video" and not os.path.isfile(value):
            raise ValueError(t("service.no_file", path=value))
        if source != "video" and not value.lower().startswith(("http://", "https://")):
            raise ValueError(t("service.no_link"))
        self.stop_streaming()
        self._active = info
        self._source = source
        self._error = ""
        self._set_state("connecting")
        self._changed("ActiveDevice", "Error", "Devices", "Source")

        context = Context(zconf=self._discovery.zconf if self._discovery else None,
                          config=self.app.config, server=self.app.server)
        receiver = create_receiver(info, context)
        self.receiver = receiver
        self._update_hold()

        def work() -> None:
            try:
                receiver.connect(prompt_cb=None, pin_cb=self._wait_for_pin)
                shared = self._play_on(receiver, source, value)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._failed, receiver, t("service.play_failed", error=exc))
                return
            GLib.idle_add(self._media_started, receiver, shared)

        threading.Thread(target=work, daemon=True, name="schwupp-play").start()

    def _play_on(self, receiver, source: str, value: str) -> str | None:  # noqa: ANN001
        """Startet die Wiedergabe (im Worker); gibt die freigegebene Datei-URL zurück."""
        if source == "video":
            url = self.app.server.add_file(value, client_host=receiver.host)
            mime = mimetypes.guess_type(value)[0] or "video/mp4"
            try:
                receiver.play_media(url, mime, title=os.path.basename(value))
            except Exception:
                self.app.server.remove_file(url)
                raise
            return url
        video_id = youtube.parse_video_id(value)
        if video_id and receiver.supports(Feature.YOUTUBE):
            receiver.play_youtube(video_id)
        elif value.lower().split("?")[0].endswith((".mp4", ".webm", ".mkv", ".m3u8")):
            receiver.play_media(value, mimetypes.guess_type(value)[0] or "video/mp4", title=value)
        else:
            stream = youtube.resolve_stream(value)
            receiver.play_media(stream.url, stream.mime, title=stream.title)
        return None

    def _media_started(self, receiver, shared: str | None) -> bool:  # noqa: ANN001
        if receiver is not self.receiver:
            # Inzwischen abgebrochen: Die Datei ist nicht mehr gefragt
            if shared:
                self.app.server.remove_file(shared)
            return False
        self._shared_url = shared
        self._set_state("streaming")
        # Der Fernseher spielt selbst ab: Das Display darf aus, nur der Medienserver
        # muss die Datei weiter liefern
        if not self._inhibit:
            self._inhibit = self.app.inhibit(None, Gtk.ApplicationInhibitFlags.SUSPEND,
                                             t("service.inhibit_media", name=receiver.name))
        return False

    def choose_file(self, uuid: str) -> None:
        """Fragt nach einer Videodatei und spielt sie auf dem Gerät ab."""
        info = self._lookup(uuid)
        if "video" not in device_actions(info):
            raise ValueError(t("service.no_media"))
        videos = Gtk.FileFilter()
        videos.set_name(t("window.filter_media"))
        videos.add_mime_type("video/*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(videos)
        dialog = Gtk.FileDialog(title=t("service.choose_video", name=info.name), filters=filters)
        movies = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
        if movies and os.path.isdir(movies):
            dialog.set_initial_folder(Gio.File.new_for_path(movies))
        self.app.hold()                  # im Dienstmodus nicht beenden, solange gefragt wird

        def chosen(dlg, result) -> None:  # noqa: ANN001
            self.app.release()
            try:
                path = dlg.open_finish(result).get_path()
            except GLib.Error:
                return                   # abgebrochen
            self._play_chosen(uuid, "video", path or "")

        dialog.open(None, None, chosen)

    def choose_link(self, uuid: str) -> None:
        """Fragt nach einem Link (vorbelegt aus der Zwischenablage) und spielt ihn ab."""
        info = self._lookup(uuid)
        if "youtube" not in device_actions(info):
            raise ValueError(t("service.no_media"))
        entry = Gtk.Entry(placeholder_text="https://youtu.be/…", activates_default=True,
                          input_purpose=Gtk.InputPurpose.URL)
        dialog = Adw.AlertDialog(heading=t("service.link_heading"),
                                 body=t("service.link_body", name=info.name),
                                 extra_child=entry)
        dialog.add_response("cancel", t("service.cancel"))
        dialog.add_response("play", t("service.play"))
        dialog.set_response_appearance("play", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("play")
        dialog.set_close_response("cancel")

        def pasted(clipboard, result) -> None:  # noqa: ANN001
            try:
                text = (clipboard.read_text_finish(result) or "").strip()
            except GLib.Error:
                return
            if text.lower().startswith(("http://", "https://")) and not entry.get_text():
                entry.set_text(text)

        def mapped(_entry) -> None:  # noqa: ANN001
            # Unter Wayland gibt es die Zwischenablage erst mit sichtbarem Fenster
            display = Gdk.Display.get_default()
            if display is not None:
                display.get_clipboard().read_text_async(None, pasted)

        def answered(_dialog, response: str) -> None:  # noqa: ANN001
            self.app.release()
            if response == "play":
                self._play_chosen(uuid, "link", entry.get_text())

        entry.connect("map", mapped)
        dialog.connect("response", answered)
        self.app.hold()
        dialog.present(None)

    def _play_chosen(self, uuid: str, source: str, value: str) -> None:
        """Nach einer Auswahl: abspielen, Fehler in Error statt als D-Bus-Antwort."""
        try:
            self.play(uuid, source, value)
        except (LookupError, ValueError) as exc:
            self._error = str(exc)
            self._changed("Error")

    def _engine_name(self, receiver) -> str | None:  # noqa: ANN001
        """Die für das Gerät gewählte Engine (sonst die erste passende)."""
        engines = engines_for_kind(receiver.kind, receiver)
        if not engines:
            return None
        names = [e.name for e in engines]
        chosen = self.app.config.device_value_for(receiver.info, "mirror_engine")
        return chosen if chosen in names else names[0]

    def _started(self, receiver) -> bool:  # noqa: ANN001
        if receiver is self.receiver:
            self._set_state("streaming")
            # Schaltet die Shell das Display ab, liefert die Bildschirmaufnahme
            # keine Bilder mehr und der Fernseher zeigt ein Standbild. Beim Ton
            # darf das Display aus, nur schlafen darf der Rechner nicht.
            if not self._inhibit:
                speaker = is_speaker(receiver.info)
                flag = Gtk.ApplicationInhibitFlags.SUSPEND if speaker \
                    else Gtk.ApplicationInhibitFlags.IDLE
                key = "service.inhibit_sound" if speaker else "service.inhibit_reason"
                self._inhibit = self.app.inhibit(None, flag, t(key, name=receiver.name))
        return False

    def _failed(self, receiver, text: str) -> bool:  # noqa: ANN001
        if receiver is not self.receiver:
            return False
        self.stop_streaming()
        self._error = text
        self._changed("Error")
        return False

    def stop_streaming(self) -> None:
        """Beendet Spiegelung und Verbindung (mehrfach aufrufbar)."""
        if self._pin_event is not None:
            self._pin_event.set()           # wartenden Verbindungsaufbau lösen
        if self._inhibit:
            self.app.uninhibit(self._inhibit)
            self._inhibit = 0
        engine, self.engine = self.engine, None
        receiver, self.receiver = self.receiver, None
        media = self._source in ("video", "link")
        if engine is not None:
            try:
                engine.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[service] Engine ließ sich nicht sauber stoppen: {exc}")
        if receiver is not None:
            threading.Thread(target=self._disconnect, args=(receiver, media),
                             daemon=True).start()
        shared, self._shared_url = self._shared_url, None
        if shared:
            # Sonst bliebe die Datei bis zum Ende der App im Netz abrufbar
            self.app.server.remove_file(shared)
        if self._desktop_started:
            self._desktop_started = False
            stop_desktop()
        self._desktop_screen = ""
        if self._active is None and self._state == "idle":
            self._desktop = False
            self._source = ""
            return
        self._active = None
        self._desktop = False
        self._source = ""
        self._set_state("idle")
        self._changed("ActiveDevice", "Devices", "Source")
        self._update_hold()
        if not self._holders:
            self._stop_discovery()

    @staticmethod
    def _disconnect(receiver, media: bool = False) -> None:  # noqa: ANN001
        if media:
            # Der Fernseher spielt selbst ab: Ohne stop liefe das Video weiter
            try:
                receiver.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[service] Wiedergabe ließ sich nicht stoppen: {exc}")
        try:
            receiver.disconnect()
        except Exception as exc:  # noqa: BLE001
            print(f"[service] Trennen fehlgeschlagen: {exc}")

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._changed("State")

    # -- PIN (AirPlay-Kopplung) ----------------------------------------------
    def _wait_for_pin(self) -> str | None:
        """pin_cb für ``Receiver.connect``: blockiert den Worker-Thread, bis die
        Shell die am Fernseher angezeigte PIN mit ``SubmitPin`` liefert."""
        event = threading.Event()
        self._pin, self._pin_event = None, event
        GLib.idle_add(self._set_state, "pin")
        event.wait(timeout=PIN_TIMEOUT_S)
        self._pin_event = None
        pin, self._pin = self._pin, None
        if pin:
            GLib.idle_add(self._set_state, "connecting")
        return pin

    def submit_pin(self, pin: str) -> None:
        if self._pin_event is None:
            raise LookupError(t("service.no_pin_requested"))
        self._pin = pin.strip() or None
        self._pin_event.set()

    # ====================================================================
    # Lebensdauer der App
    # ====================================================================
    def _update_hold(self) -> None:
        """Hält die App am Leben, solange gesucht oder gespiegelt wird.

        Im Dienstmodus beendet sich die App danach von selbst (Inaktivitäts-
        Timeout); mit offenem Fenster hält ohnehin das Fenster sie.
        """
        busy = self._discovery is not None or self.receiver is not None
        if busy and not self._held:
            self.app.hold()
        elif not busy and self._held:
            self.app.release()
        self._held = busy
