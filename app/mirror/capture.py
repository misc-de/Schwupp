"""Bildschirm-Capture als GStreamer-Quelle (X11, Wayland-Portal, wf-recorder).

Liefert einer Engine den vorderen Teil einer GStreamer-Pipeline, der rohe
Videoframes vom Bildschirm produziert. Drei Wege, in dieser Reihenfolge:

* **X11** (z. B. XFCE): ``ximagesrc`` – sofort, ohne Berechtigungsdialog.
* **Wayland/Portal** (GNOME, KDE, Phosh mit xdg-desktop-portal-wlr): PipeWire
  über ``org.freedesktop.portal.ScreenCast``; der Nutzer bestätigt einmalig.
* **wf-recorder** (wlroots-Compositoren ohne Portal-Backend, z. B. phoc/FLX1).

:class:`CaptureSelector` kapselt die Auswahl samt Fallback-Kette, damit alle
Engines identisch starten und Fehler auf demselben Weg melden (Callback statt
``print``).

Der Systemton wird über :func:`audio_source_desc` mitgenommen (PipeWire-/Pulse-
Monitor); Engines, die Audio übertragen können, fragen ihn separat ab.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from collections.abc import Callable

from .engine import gst_element_exists


def is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


# --- H.264-Encoder-Auswahl ---------------------------------------------------
# Reihenfolge der Kandidaten. x264enc bleibt bewusst vorn: er ist auf allen
# getesteten Geräten verifiziert. Die übrigen greifen, wo x264enc fehlt (z. B.
# eine Runtime ohne gst-plugins-ugly) oder wenn per Config erzwungen.
_ENCODERS: dict[str, tuple[str, str]] = {
    # name: (GStreamer-Element, Parameter-Template mit {bitrate_kbps} und {fps})
    "x264": ("x264enc",
             "x264enc tune=zerolatency speed-preset=ultrafast "
             "bitrate={bitrate_kbps} key-int-max={fps}"),
    "openh264": ("openh264enc",
                 "openh264enc usage-type=screen complexity=low rate-control=bitrate "
                 "bitrate={bitrate_bps} gop-size={fps}"),
    "vaapi": ("vaapih264enc",
              "vaapih264enc rate-control=cbr bitrate={bitrate_kbps} keyframe-period={fps}"),
    "v4l2": ("v4l2h264enc",
             "v4l2h264enc extra-controls=\"controls,video_bitrate={bitrate_bps}\""),
}

ENCODER_CHOICES = ("auto", *_ENCODERS)


def available_encoders() -> list[str]:
    """Namen der H.264-Encoder, deren GStreamer-Element vorhanden ist."""
    return [name for name, (element, _) in _ENCODERS.items() if gst_element_exists(element)]


def h264_encoder_desc(bitrate_kbps: int, fps: int, preferred: str = "auto") -> str:
    """GStreamer-Beschreibung des H.264-Encoders (ohne umgebende ``!``).

    *preferred* ist ein Schlüssel aus :data:`ENCODER_CHOICES`; ``auto`` nimmt den
    ersten verfügbaren aus der Vorzugsreihenfolge. Ein ausdrücklich gewählter,
    aber nicht (mehr) vorhandener Encoder blockiert die Spiegelung nicht – die
    Kette läuft dann normal weiter. Wirft nur, wenn es gar keinen gibt.
    """
    order = list(_ENCODERS)
    if preferred in _ENCODERS:
        order.insert(0, preferred)
    for name in order:
        element, template = _ENCODERS[name]
        if gst_element_exists(element):
            return template.format(bitrate_kbps=bitrate_kbps,
                                   bitrate_bps=bitrate_kbps * 1000, fps=fps)
    raise RuntimeError(
        "Kein H.264-Encoder gefunden (x264enc, openh264enc, vaapih264enc oder "
        "v4l2h264enc) – bitte gst-plugins-ugly bzw. gst-plugins-bad installieren"
    )


# --- Systemton ---------------------------------------------------------------

def audio_source_desc() -> str | None:
    """Quelle für den Systemton (Monitor der Standard-Ausgabe) oder None.

    ``pulsesrc`` mit dem Default-Monitor funktioniert unter PulseAudio *und*
    PipeWire (dessen Pulse-Kompatibilität) und ist damit der breiteste Weg;
    ``pipewiresrc`` dient als Rückfall.
    """
    if gst_element_exists("pulsesrc"):
        # Ohne device= nimmt pulsesrc die Default-Quelle (Mikrofon). Der Monitor
        # der Standard-Senke wird über die Umgebungsvariable gewählt, die
        # PulseAudio/PipeWire beim Verbinden auflöst.
        return ("pulsesrc provide-clock=false do-timestamp=true "
                "! audio/x-raw,channels=2 ! audioconvert ! audioresample")
    if gst_element_exists("pipewiresrc"):
        return ("pipewiresrc ! audio/x-raw,channels=2 ! audioconvert ! audioresample")
    return None


def default_monitor_device() -> str | None:
    """Name des Monitor-Geräts der aktuellen Standard-Ausgabe (für pulsesrc)."""
    try:
        out = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                             text=True, timeout=3, check=False)
        sink = out.stdout.strip()
        return f"{sink}.monitor" if sink else None
    except (OSError, subprocess.SubprocessError):
        return None


# --- Quellbeschreibungen -----------------------------------------------------

def x11_source_desc(fps: int = 30, show_pointer: bool = True) -> str:
    """GStreamer-Quellbeschreibung für X11 (endet mit rohem video/x-raw)."""
    ptr = "true" if show_pointer else "false"
    return (
        f"ximagesrc use-damage=false show-pointer={ptr} "
        f"! video/x-raw,framerate={fps}/1 "
        f"! videoconvert ! videorate ! video/x-raw,framerate={fps}/1"
    )


def pipewire_source_desc(fd: int, node_id: int, fps: int = 30) -> str:
    """GStreamer-Quellbeschreibung für eine Portal-PipeWire-FD."""
    return (
        f"pipewiresrc fd={fd} path={node_id} "
        f"! videoconvert ! videorate ! video/x-raw,framerate={fps}/1"
    )


def x11_capture_available() -> bool:
    """True, wenn eine X11-Sitzung läuft *und* ximagesrc vorhanden ist.

    Die Element-Prüfung ist wichtig: In der Flatpak-Runtime fehlt ximagesrc,
    solange das Manifest es nicht mitliefert – dort führt der Portal-Weg.
    """
    return not is_wayland() and bool(os.environ.get("DISPLAY")) \
        and gst_element_exists("ximagesrc")


def wf_recorder_available() -> bool:
    """True, wenn wf-recorder als Wayland-Capture-Fallback nutzbar ist."""
    return shutil.which("wf-recorder") is not None


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


def capture_available() -> tuple[bool, str]:
    """(nutzbar?, Hinweis) – gibt es überhaupt einen Weg, den Bildschirm zu lesen?"""
    if x11_capture_available():
        return True, "X11 (ximagesrc)"
    if screencast_portal_available():
        return True, "Wayland (ScreenCast-Portal)"
    if wf_recorder_available():
        return True, "Wayland (wf-recorder)"
    if is_wayland():
        return False, ("Keine Bildschirmaufnahme möglich: weder ScreenCast-Portal "
                       "(xdg-desktop-portal-wlr/-gnome) noch wf-recorder gefunden")
    return False, ("Keine Bildschirmaufnahme möglich: GStreamer-Element ximagesrc "
                   "fehlt (gst-plugins-good mit X11-Unterstützung)")


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
                           "(xdg-desktop-portal-wlr/-gnome installiert und aktiv?)")
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


class WfRecorderCapture:
    """Wayland-Capture-Fallback ohne Portal: ``wf-recorder`` (wlr-screencopy).

    Startet wf-recorder, das H.264/MPEG-TS nach stdout schreibt; die
    GStreamer-Quelle liest sie per ``fdsrc``. Für Compositoren wie phoc, wo kein
    ScreenCast-Portal-Backend (xdg-desktop-portal-wlr) verfügbar ist.
    """

    def __init__(self, fps: int = 30, output: str | None = None) -> None:
        self.fps = fps
        self.output = output
        self._proc = None

    def source_desc(self) -> str:
        # MPEG-TS statt Matroska: streamt live (kein Cluster-Puffern -> kein Ruckeln).
        # -x yuv420p ist nötig (libx264 kommt mit RGB-Default nicht klar);
        # -r erzwingt konstante Framerate.
        # preset=ultrafast + tune=zerolatency: ohne preset nimmt libx264 "medium",
        # das auf dem ARM-Phone bei voller Display-Auflösung nur wenige fps schafft
        # (Hauptursache der Trägheit). Dieser wf-Encode wird ohnehin neu kodiert.
        cmd = ["wf-recorder", "-y", "--codec", "libx264", "-x", "yuv420p",
               "-r", str(self.fps), "-p", "preset=ultrafast", "-p", "tune=zerolatency",
               "--muxer", "mpegts", "-f", "/dev/stdout"]
        if self.output:
            cmd += ["-o", self.output]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        fd = self._proc.stdout.fileno()
        # leaky Queue entkoppelt wf-recorder vom (auf ARM langsameren) Re-Encode,
        # sonst blockiert die Pipe und das Bild friert ein.
        # pixel-aspect-ratio=1/1: der H.264-Stream kann ein abweichendes SAR tragen.
        return (
            f"fdsrc fd={fd} do-timestamp=true ! queue ! tsdemux ! h264parse "
            f"! avdec_h264 ! queue leaky=downstream max-size-buffers=2 "
            f"! videoconvert ! videorate "
            f"! video/x-raw,framerate={self.fps}/1,pixel-aspect-ratio=1/1"
        )

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    self._proc.kill()
            self._proc = None


class CaptureSelector:
    """Wählt den passenden Capture-Weg und meldet ihn per Callback.

    Alle Engines nutzen dieselbe Kette, damit sich Verhalten und Fehlermeldungen
    nicht je Engine unterscheiden:

    1. **X11 + ximagesrc** – sofort verfügbar, kein Dialog.
    2. **ScreenCast-Portal** – asynchron (Nutzerdialog); scheitert es, wird
       automatisch auf wf-recorder zurückgefallen.
    3. **wf-recorder** – letzter Ausweg für wlroots ohne Portal-Backend.

    ``start`` kehrt sofort zurück; genau einer der beiden Callbacks wird
    aufgerufen – ``on_ready(source_desc)`` oder ``on_error(text)``.
    """

    def __init__(self, fps: int = 30) -> None:
        self.fps = fps
        self._wf: WfRecorderCapture | None = None
        self._portal: PortalScreenCast | None = None
        self._cancelled = False
        self._kind: str | None = None       # "x11" | "portal" | "wf"
        self._last_kind: str | None = None  # tatsächlich gelieferter Weg

    def start(self, on_ready: Callable[[str], None],
              on_error: Callable[[str], None]) -> None:
        self._on_ready, self._on_error = on_ready, on_error
        if x11_capture_available():
            self._kind = "x11"
            self._deliver(x11_source_desc(fps=self.fps))
            return
        if screencast_portal_available():
            self._kind = "portal"
            self._portal = PortalScreenCast(fps=self.fps)
            self._portal.start(self._on_portal_ready)
            return
        self._try_wf_recorder()

    def _on_portal_ready(self, fd, node_or_err) -> None:  # noqa: ANN001
        if self._cancelled:
            return  # bereits gestoppt, während der Portal-Dialog lief
        if fd is None:
            # Kein/abgelehntes Portal -> letzter Versuch über wlr-screencopy.
            if wf_recorder_available():
                self._try_wf_recorder()
            else:
                self._on_error(f"Bildschirmfreigabe nicht möglich: {node_or_err}")
            return
        self._deliver(pipewire_source_desc(fd, node_or_err, self.fps))

    def _try_wf_recorder(self) -> None:
        if not wf_recorder_available():
            ok, detail = capture_available()
            self._on_error(detail)
            return
        try:
            self._kind = "wf"
            self._wf = WfRecorderCapture(fps=self.fps)
            self._deliver(self._wf.source_desc())
        except OSError as exc:
            self._on_error(f"wf-recorder konnte nicht gestartet werden: {exc}")

    def _deliver(self, desc: str) -> None:
        self._last_kind = self._kind
        if not self._cancelled:
            self._on_ready(desc)

    def restart_sync(self) -> str | None:
        """Baut dieselbe Quelle synchron neu auf (für die Auto-Recovery).

        Nur für Wege möglich, die ohne Nutzerdialog auskommen (X11, wf-recorder).
        Beim Portal-Weg ist eine erneute Aushandlung nötig -> ``None``.
        """
        if self._last_kind == "x11":
            return x11_source_desc(fps=self.fps)
        if self._last_kind == "wf":
            if self._wf is not None:
                self._wf.stop()
            self._wf = WfRecorderCapture(fps=self.fps)
            return self._wf.source_desc()
        return None

    def stop(self) -> None:
        """Beendet einen laufenden Hilfsprozess und verhindert späte Callbacks."""
        self._cancelled = True
        if self._wf is not None:
            self._wf.stop()
            self._wf = None
