"""Bildschirm-Capture als GStreamer-Quelle (X11 und Wayland/Portal).

Liefert für eine Engine den vorderen Teil einer GStreamer-Pipeline, der rohe
Videoframes vom Bildschirm produziert:

* **X11** (z. B. XFCE): ``ximagesrc`` – einfach und ohne Berechtigungsdialog.
* **Wayland** (Phosh/FLX1, GNOME, …): ``pipewiresrc`` gefüttert über das
  ``org.freedesktop.portal.ScreenCast``-Portal. Der Nutzer bestätigt einmalig
  per Dialog, welcher Bildschirm geteilt wird.

Audio (System-Ton mit-casten) ist bewusst noch nicht enthalten – kommt als
eigener Pfad (pulse/pipewire-Monitor), sobald das Video steht.
"""
from __future__ import annotations

import os


def is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


def x11_source_desc(fps: int = 30, show_pointer: bool = True) -> str:
    """GStreamer-Quellbeschreibung für X11 (endet mit rohem video/x-raw)."""
    ptr = "true" if show_pointer else "false"
    return (
        f"ximagesrc use-damage=false show-pointer={ptr} "
        f"! video/x-raw,framerate={fps}/1 "
        f"! videoconvert ! videorate ! video/x-raw,framerate={fps}/1"
    )


class PortalScreenCast:
    """Wayland-Bildschirmfreigabe über xdg-desktop-portal (ScreenCast).

    Führt den asynchronen D-Bus-Handshake (CreateSession → SelectSources → Start
    → OpenPipeWireRemote) aus und liefert eine PipeWire-Remote-FD plus Node-ID,
    aus der eine ``pipewiresrc``-Quelle gebaut werden kann.

    Nutzung benötigt eine laufende GLib-MainLoop (in der GUI vorhanden). Der
    Handshake ist signalbasiert; ``start(callback)`` ruft *callback(fd, node_id)*
    bzw. *callback(None, fehlertext)* auf.
    """

    PORTAL_BUS = "org.freedesktop.portal.Desktop"
    PORTAL_OBJ = "/org/freedesktop/portal/desktop"
    SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"

    # cursor_mode: 1=hidden, 2=embedded, 4=metadata; source type 1=monitor, 2=window
    def __init__(self, fps: int = 30) -> None:
        self.fps = fps
        self._bus = None
        self._session_handle: str | None = None
        self._token_counter = 0

    def _new_token(self, prefix: str) -> str:
        self._token_counter += 1
        return f"schwupp_{prefix}_{self._token_counter}"

    def start(self, callback) -> None:  # noqa: ANN001
        """Startet den Portal-Handshake. *callback(fd:int|None, node_id_or_err)*."""
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self._GLib = GLib
        self._Gio = Gio
        self._callback = callback
        self._done = False
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._unique = self._bus.get_unique_name().lstrip(":").replace(".", "_")
        self._create_session()
        # Nicht ewig hängen, falls kein ScreenCast-Portal-Backend antwortet.
        GLib.timeout_add_seconds(15, self._on_timeout)

    def _on_timeout(self) -> bool:
        self._finish(None, "ScreenCast-Portal antwortet nicht "
                           "(xdg-desktop-portal-wlr installiert/aktiv?)")
        return False

    def _finish(self, fd, info) -> None:  # noqa: ANN001
        """Ruft den Callback genau einmal (Timeout vs. echtes Ergebnis)."""
        if self._done:
            return
        self._done = True
        self._callback(fd, info)

    # -- Schritt 1: Session erstellen ----------------------------------------
    def _create_session(self) -> None:
        token = self._new_token("create")
        session_token = self._new_token("session")
        self._await_response(token, self._on_session_created)
        opts = {
            "handle_token": self._GLib.Variant("s", token),
            "session_handle_token": self._GLib.Variant("s", session_token),
        }
        self._call("CreateSession", self._GLib.Variant("(a{sv})", (opts,)))

    def _on_session_created(self, response: int, results: dict) -> None:
        if response != 0:
            return self._fail("Bildschirmfreigabe abgebrochen")
        self._session_handle = results["session_handle"]
        self._select_sources()

    # -- Schritt 2: Quellen wählen -------------------------------------------
    def _select_sources(self) -> None:
        token = self._new_token("select")
        self._await_response(token, self._on_sources_selected)
        opts = {
            "handle_token": self._GLib.Variant("s", token),
            "types": self._GLib.Variant("u", 1),       # 1 = Monitor
            "multiple": self._GLib.Variant("b", False),
            "cursor_mode": self._GLib.Variant("u", 2),  # 2 = embedded
        }
        self._call(
            "SelectSources",
            self._GLib.Variant("(oa{sv})", (self._session_handle, opts)),
        )

    def _on_sources_selected(self, response: int, results: dict) -> None:
        if response != 0:
            return self._fail("Quellenauswahl fehlgeschlagen")
        self._start_cast()

    # -- Schritt 3: Start ----------------------------------------------------
    def _start_cast(self) -> None:
        token = self._new_token("start")
        self._await_response(token, self._on_started)
        opts = {"handle_token": self._GLib.Variant("s", token)}
        self._call(
            "Start",
            self._GLib.Variant("(osa{sv})", (self._session_handle, "", opts)),
        )

    def _on_started(self, response: int, results: dict) -> None:
        if response != 0:
            return self._fail("Start der Bildschirmfreigabe fehlgeschlagen")
        streams = results.get("streams")
        if not streams:
            return self._fail("Portal lieferte keinen Stream")
        node_id = streams[0][0]
        self._open_remote(node_id)

    # -- Schritt 4: PipeWire-FD holen ----------------------------------------
    def _open_remote(self, node_id: int) -> None:
        opts = self._GLib.Variant("(oa{sv})", (self._session_handle, {}))
        self._bus.call_with_unix_fd_list(
            self.PORTAL_BUS,
            self.PORTAL_OBJ,
            self.SCREENCAST_IFACE,
            "OpenPipeWireRemote",
            opts,
            self._GLib.VariantType("(h)"),
            self._Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
            self._on_remote_opened,
            node_id,
        )

    def _on_remote_opened(self, source, res, node_id) -> None:  # noqa: ANN001
        try:
            ret, fd_list = self._bus.call_with_unix_fd_list_finish(res)
            handle_index = ret.unpack()[0]
            fd = fd_list.get(handle_index)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"PipeWire-FD-Fehler: {exc}")
        self._finish(fd, node_id)

    # -- D-Bus-Hilfen --------------------------------------------------------
    def _call(self, method: str, params) -> None:  # noqa: ANN001
        self._bus.call(
            self.PORTAL_BUS, self.PORTAL_OBJ, self.SCREENCAST_IFACE,
            method, params, self._GLib.VariantType("(o)"),
            self._Gio.DBusCallFlags.NONE, -1, None, None,
        )

    def _await_response(self, token: str, handler) -> None:  # noqa: ANN001
        """Abonniert das Response-Signal des Request-Objekts zu *token*."""
        request_path = (
            f"/org/freedesktop/portal/desktop/request/{self._unique}/{token}"
        )
        sub_id = {"id": None}

        def on_signal(conn, sender, path, iface, signal, params):  # noqa: ANN001
            response, results = params.unpack()
            self._bus.signal_unsubscribe(sub_id["id"])
            handler(response, results)

        sub_id["id"] = self._bus.signal_subscribe(
            self.PORTAL_BUS, "org.freedesktop.portal.Request", "Response",
            request_path, None, self._Gio.DBusSignalFlags.NONE, on_signal,
        )

    def _fail(self, msg: str) -> None:
        self._finish(None, msg)


def pipewire_source_desc(fd: int, node_id: int, fps: int = 30) -> str:
    """GStreamer-Quellbeschreibung für eine Portal-PipeWire-FD."""
    return (
        f"pipewiresrc fd={fd} path={node_id} "
        f"! videoconvert ! videorate ! video/x-raw,framerate={fps}/1"
    )


def screencast_portal_available() -> bool:
    """True, wenn das D-Bus-ScreenCast-Portal erreichbar ist (sonst -> Fallback)."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.freedesktop.portal.ScreenCast", "version")),
            None, Gio.DBusCallFlags.NONE, 2000, None,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def wf_recorder_available() -> bool:
    """True, wenn wf-recorder als Wayland-Capture-Fallback nutzbar ist."""
    import shutil
    return shutil.which("wf-recorder") is not None


class WfRecorderCapture:
    """Wayland-Capture-Fallback ohne Portal: ``wf-recorder`` (wlr-screencopy).

    Startet wf-recorder, das rohe Frames als Matroska nach stdout schreibt; die
    GStreamer-Quelle liest sie per ``fdsrc``. Für Compositoren wie phoc, wo kein
    ScreenCast-Portal-Backend (xdg-desktop-portal-wlr) verfügbar ist.
    """

    def __init__(self, fps: int = 30, output: str | None = None) -> None:
        self.fps = fps
        self.output = output
        self._proc = None

    def source_desc(self) -> str:
        import subprocess
        cmd = ["wf-recorder", "--codec", "rawvideo", "--muxer", "matroska",
               "-f", "/dev/stdout"]
        if self.output:
            cmd += ["-o", self.output]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        fd = self._proc.stdout.fileno()
        return (
            f"fdsrc fd={fd} ! matroskademux ! videoconvert ! videorate "
            f"! video/x-raw,framerate={self.fps}/1"
        )

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None
