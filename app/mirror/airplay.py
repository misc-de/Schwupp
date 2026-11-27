"""AirPlay-Bildschirmspiegelung – eigenes Protokoll, eigene Aufnahme.

Spiegelt auf AirPlay-Fernseher (Hisense, Samsung, Sony, Roku, Apple TV …)
ohne Hilfsprogramm: Kopplung, Sitzung und Videostrom stecken in
:mod:`.airplay_stream`, Bild und Encoder kommen aus derselben Kette wie beim
Cast-Spiegeln (Portal/X11 → Hardware- oder Software-H.264). Auf dem Telefon
heißt das: Hardware-Encoder statt x264 in Software.

Kopplung: Fernseher anderer Hersteller nehmen dieselbe HAP-Kopplung wie die
Wiedergabe über pyatv – die beim Verbinden gespeicherte gilt also auch hier,
der Fernseher fragt nicht zweimal nach einem Code. Fehlt sie oder gilt sie
nicht mehr, zeigt der Fernseher einen Code, den derselbe Dialog abfragt.

Netzwerk: Bei NTP-Empfängern fragt der Fernseher während des Aufbaus per UDP
unsere Uhr ab. Dafür gilt ein fester Portbereich (:data:`PORT_RANGE`), damit
sich eine Firewall-Regel angeben lässt (siehe docs/MIRRORING.md).

Ton überträgt diese Engine noch nicht; der Ton-Stream wird nur als Platzhalter
angemeldet, wie es die Empfänger erwarten.
"""
from __future__ import annotations

import contextlib
import socket
import threading
import time

from .airplay_stream import hap
from .airplay_stream.session import MirrorSession, PtpUnavailable
from .capture import h264_encoder_desc, retime_portal_source
from .engine import MirrorEngine, gst_init

PORT_RANGE = (60000, 60010)
FREEZE_TIMEOUT_S = 4.0
MAX_RECOVERIES = 5


class AirplayMirrorEngine(MirrorEngine):
    name = "airplay"
    display_name = "AirPlay"

    def __init__(self, receiver, server, config, on_error=None,
                 on_started=None) -> None:  # noqa: ANN001
        super().__init__(receiver, server, config, on_error, on_started)
        self._session: MirrorSession | None = None
        self._pipeline = None
        self._Gst = None
        self._source_desc = ""
        self._size = (0, 0)
        self._last_frame = 0.0
        self._recovering = False
        self._recover_times: list[float] = []

    @staticmethod
    def check_available() -> tuple[bool, str]:
        from .capture import available_encoders, capture_available

        if not available_encoders():
            return False, ("Kein H.264-Encoder in GStreamer gefunden "
                           "(x264enc aus gst-plugins-ugly empfohlen)")
        try:
            import cryptography  # noqa: F401
        except ImportError:
            return False, "Python-Paket 'cryptography' fehlt"
        ok, detail = capture_available()
        if not ok:
            return False, detail
        return True, "Bereit"

    # -- Kopplungsdaten --------------------------------------------------------
    def _host(self) -> str:
        return str(self.receiver.host)

    def _stored(self, key: str) -> hap.Credentials | None:
        return hap.Credentials.parse((self.config[key] or {}).get(self._host()))

    def _credentials(self) -> hap.Credentials | None:
        """Erst die eigene Spiegel-Kopplung, dann die der Wiedergabe (pyatv)."""
        return self._stored("airplay_mirror_creds") or self._stored("airplay_creds")

    def _store_credentials(self, creds: hap.Credentials, legacy: bool) -> None:
        # HKP-3-Kopplungen teilt sich die Engine mit der Wiedergabe über pyatv;
        # die eigene (Typ 5) bekommt einen eigenen Platz, pyatv kennt sie nicht.
        key = "airplay_creds" if legacy else "airplay_mirror_creds"
        stored = dict(self.config[key] or {})
        stored[self._host()] = str(creds)
        self.config[key] = stored
        with contextlib.suppress(Exception):
            self.config.save()

    # -- Start ----------------------------------------------------------------
    def start(self) -> None:
        if getattr(self.receiver, "kind", None) != "airplay":
            self.fail("AirPlay-Spiegelung ist nur für AirPlay-Geräte verfügbar")
            return
        super().start()
        threading.Thread(target=self._watchdog_loop, daemon=True,
                         name="mirror-airplay-watchdog").start()

    def _new_session(self, force_ntp: bool = False) -> MirrorSession:
        return MirrorSession(
            self._host(),
            credentials=self._credentials(),
            store_credentials=self._store_credentials,
            pin_callback=getattr(self.receiver, "pin_callback", None),
            name=socket.gethostname() or "Schwupp",
            port_range=PORT_RANGE,
            latency_ms=self.cfg_int("mirror_target_delay_ms", 150),
            force_ntp=force_ntp,
            on_failure=self._on_session_failure,
        )

    def run(self, source_desc: str) -> None:
        self._source_desc = source_desc
        width, height, fps, bitrate_kbps = self.video_params()
        self._size = (width, height)
        session = self._new_session()
        self._session = session
        try:
            session.connect()
            if not self._running:
                session.close()
                return
            try:
                session.setup(width, height)
            except PtpUnavailable as exc:
                # Manche Empfänger bieten PTP an und liefern dann keine Uhr –
                # neue Verbindung mit NTP, wie es echte Sender auch tun.
                print(f"[airplay] {exc} – neuer Versuch mit NTP")
                session.close()
                session = self._new_session(force_ntp=True)
                self._session = session
                session.connect()
                session.setup(width, height)
        except Exception:
            session.close()
            raise
        if not self._running:
            session.close()
            return
        self._launch_encoder(fps, bitrate_kbps)

    def _on_session_failure(self, message: str) -> None:
        if self._running:
            self.fail(message)

    # -- Encoder --------------------------------------------------------------
    def _launch_encoder(self, fps: int, bitrate_kbps: int) -> None:
        Gst = gst_init()
        self._Gst = Gst
        width, height = self._size
        encoder = h264_encoder_desc(bitrate_kbps, fps, str(self.cfg("mirror_encoder") or "auto"))
        desc = (
            f"{self._source_desc} ! videoconvert ! videoscale add-borders=true "
            f"! video/x-raw,width={width},height={height},pixel-aspect-ratio=1/1 "
            f"! {encoder} "
            f"! video/x-h264,profile=main,stream-format=byte-stream "
            f"! h264parse config-interval=-1 "
            f"! video/x-h264,stream-format=byte-stream,alignment=au "
            f"! appsink name=sink emit-signals=true sync=false max-buffers=1 drop=true async=false"
        )
        self._pipeline = Gst.parse_launch(desc)
        retime_portal_source(self._pipeline, Gst)
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        self._pipeline.set_state(Gst.State.PLAYING)

    def _on_bus_error(self, _bus, message) -> None:  # noqa: ANN001
        err, _dbg = message.parse_error()
        self.fail(f"GStreamer-Fehler beim Spiegeln: {err}")

    def _on_sample(self, sink):  # noqa: ANN001
        Gst = self._Gst
        sample = sink.emit("pull-sample")
        session = self._session
        if sample is None or not self._running or session is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        # Zeitstempel = Sendezeitpunkt (wie beim Cast-Spiegeln): Das Kodieren
        # dauert unterschiedlich lange, mit der Aufnahmezeit kämen die Bilder für
        # den Fernseher unregelmäßig spät.
        if session.send_access_unit(data):
            self._last_frame = time.monotonic()
        return Gst.FlowReturn.OK

    # -- Selbstheilung: eingefrorene Aufnahme neu starten -----------------------
    def _watchdog_loop(self) -> None:
        while self._running:
            time.sleep(2)
            if self._recovering or not self._last_frame:
                continue
            if time.monotonic() - self._last_frame > FREEZE_TIMEOUT_S:
                self._recover()

    def _recover(self) -> None:
        now = time.time()
        self._recover_times = [t for t in self._recover_times if now - t < 60]
        if len(self._recover_times) >= MAX_RECOVERIES:
            self.fail("Bildschirmaufnahme friert wiederholt ein – Spiegelung beendet")
            return
        self._recover_times.append(now)
        self._recovering = True
        print("[airplay] Aufnahme eingefroren – starte neu")
        with contextlib.suppress(Exception):
            if self._pipeline is not None:
                self._pipeline.set_state(self._Gst.State.NULL)
                self._pipeline = None
        self._last_frame = 0.0
        try:
            source = self._capture.restart_sync() if self._capture else None
            if source is None:
                self.fail("Bildschirmaufnahme abgebrochen – bitte erneut starten "
                          "(Freigabe muss neu bestätigt werden)")
                return
            self._source_desc = source
            _w, _h, fps, bitrate_kbps = self.video_params()
            # Sitzung und Datenkanal bleiben stehen; der neue Encoder beginnt
            # mit einem Schlüsselbild und eigener Konfiguration.
            if self._session is not None:
                self._session.restart_video()
            self._launch_encoder(fps, bitrate_kbps)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"Neustart der Bildschirmaufnahme fehlgeschlagen: {exc}")
        finally:
            self._recovering = False

    # -- Stop -----------------------------------------------------------------
    def stop(self) -> None:
        self._running = False
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            with contextlib.suppress(Exception):
                pipeline.set_state(self._Gst.State.NULL)
        session, self._session = self._session, None
        if session is not None:
            session.close()
        self._stop_capture()
