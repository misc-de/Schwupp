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
"""
from __future__ import annotations

import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .config import DEVICE_KEYS  # noqa: E402
from .discovery import Discovery, ReceiverInfo  # noqa: E402
from .i18n import t  # noqa: E402
from .mirror import engines_for_kind, get_engine_class  # noqa: E402
from .mirror.capture import ENCODER_CHOICES, available_encoders  # noqa: E402
from .receivers import create_receiver  # noqa: E402
from .receivers.base import Context  # noqa: E402
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
  </interface>
</node>
"""

_KIND_ICON = {"chromecast": "video-display-symbolic", "webos": "tv-symbolic",
              "airplay": "tv-symbolic", "dlna": "tv-symbolic"}
SPEAKER_ICON = "audio-speakers-symbolic"

# So lange wartet ein Verbindungsaufbau auf die PIN vom Fernseher.
PIN_TIMEOUT_S = 180

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
        return None

    def _changed(self, *names: str) -> None:
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
    def connect(self, uuid: str) -> None:
        info = self._devices.get(uuid)
        if info is None and self._active is not None and self._active.uuid == uuid:
            info = self._active
        if info is None:
            raise LookupError(t("service.unknown_device"))
        self.stop_streaming()
        self._active = info
        self._error = ""
        self._set_state("connecting")
        self._changed("ActiveDevice", "Error", "Devices")

        context = Context(zconf=self._discovery.zconf if self._discovery else None,
                          config=self.app.config, server=self.app.server)
        receiver = create_receiver(info, context)
        self.receiver = receiver
        self._update_hold()

        def work() -> None:
            try:
                receiver.connect(prompt_cb=None, pin_cb=self._wait_for_pin)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._failed, receiver, t("window.connect_failed", error=exc))
                return
            GLib.idle_add(self._connected, receiver)

        threading.Thread(target=work, daemon=True, name="schwupp-connect").start()

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
        if engine is not None:
            try:
                engine.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[service] Engine ließ sich nicht sauber stoppen: {exc}")
        if receiver is not None:
            threading.Thread(target=self._disconnect, args=(receiver,), daemon=True).start()
        if self._active is None and self._state == "idle":
            return
        self._active = None
        self._set_state("idle")
        self._changed("ActiveDevice", "Devices")
        self._update_hold()
        if not self._holders:
            self._stop_discovery()

    @staticmethod
    def _disconnect(receiver) -> None:  # noqa: ANN001
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
