"""Native Cast-Streaming-Engine — Low-Latency-Mirroring (<1 s, live bestätigt).

Sendet den Bildschirm per echtem Cast-Streaming-Protokoll statt über HLS:
  1. Mirroring-Receiver-App starten + OFFER/ANSWER über den webrtc-Namespace
     (-> ausgehandelter UDP-Port, AES-Key/IV).  [cast_streaming.control]
  2. H.264 (Video) und Opus (Systemton) mit GStreamer encoden (appsink), je Frame:
     AES-128-CTR verschlüsseln [crypto] -> in Cast-RTP-Pakete zerlegen [rtp]
     -> per UDP an den ausgehandelten Port senden.

Drei hart erarbeitete Bausteine, ohne die der Receiver schwarz bleibt/abbricht:
  • frame_id 0-BASIERT, erstes gesendetes Frame MUSS ein Keyframe sein
    (sonst wartet der Receiver ewig auf das nie existierende Frame 0).
  • RTCP-Sender-Report (PT=200) mit einer Uhr, die an den zuletzt gesendeten
    Frame gekoppelt UND um den TV-Uhr-Offset korrigiert ist (sonst spielt der
    Receiver die Frames "aus der Zukunft" mit ~0,8 s Extra-Latenz ab).
  • Retransmission: der Receiver fordert fehlende Pakete per Cast-NACK an
    (PT=206, magic 'CAST'); ohne erneutes Senden bleibt der Decoder stehen.
Der TV-Uhr-Offset wird laufend aus den XR-Paketen (PT=207, Receiver Reference
Time) des Receivers gemessen.

Audio läuft als zweiter RTP-Stream (Opus, eigener SSRC und frame_id-Raum) und
ist strikt optional: Lehnt der Receiver den Audio-Stream im ANSWER ab oder gibt
es keine Monitor-Quelle, wird nur Video gesendet – die Spiegelung läuft weiter.
"""
from __future__ import annotations

import contextlib
import os
import socket
import threading
import time

from .capture import audio_source_desc, default_monitor_device, h264_encoder_desc
from .cast_streaming import rtcp, rtp
from .cast_streaming.control import (
    MIRRORING_APP_ID,
    CastStreamingControl,
    audio_stream,
    video_stream,
)
from .cast_streaming.crypto import encrypt_frame
from .engine import MirrorEngine, gst_element_exists, gst_init

VIDEO_SSRC = 100001
AUDIO_SSRC = 100003
VIDEO_PT = 96
AUDIO_PT = 127
VIDEO_CLOCK = 90000
AUDIO_CLOCK = 48000
TARGET_DELAY_MS = 150   # Playout-Puffer am Receiver
AUDIO_TARGET_DELAY_MS = 150
FREEZE_TIMEOUT_S = 4.0  # so lange ohne neues Frame = Capture eingefroren -> Neustart
MAX_RECOVERIES = 5      # mehr Neustarts in 60 s -> aufgeben (Capture dauerhaft kaputt)
RETAIN_FRAMES = 90      # Paketpuffer für Retransmits (~3 s bei 30 fps)


class _StreamState:
    """Sendezustand eines RTP-Streams (Video oder Audio).

    Video und Audio haben je einen eigenen frame_id-, Sequenz- und
    Zeitstempel-Raum; gemeinsam ist nur der UDP-Socket und die Uhr-Korrektur.
    """

    def __init__(self, ssrc: int, payload_type: int, clock: int) -> None:
        self.ssrc = ssrc
        self.payload_type = payload_type
        self.clock = clock
        self.fid = 0
        self.seq = 0
        self.packets = 0
        self.octets = 0
        self.started = False
        self.last_rtp = 0
        self.last_wall = 0.0
        self.rtp_offset = 0
        self.rebase = False
        self.buffer: dict[int, dict[int, bytes]] = {}
        self.lock = threading.Lock()


class NativeMirrorEngine(MirrorEngine):
    name = "native"
    display_name = "Nativ"

    def __init__(self, receiver, server, config, on_error=None) -> None:  # noqa: ANN001
        super().__init__(receiver, server, config, on_error)
        self._pipeline = None
        self._audio_pipeline = None
        self._Gst = None
        self._ctrl: CastStreamingControl | None = None
        self._sock: socket.socket | None = None
        self._dest = None
        self._source_desc = ""
        self._key = b""
        self._iv = b""
        self._video = _StreamState(VIDEO_SSRC, VIDEO_PT, VIDEO_CLOCK)
        self._audio = _StreamState(AUDIO_SSRC, AUDIO_PT, AUDIO_CLOCK)
        self._audio_active = False
        self._clock_offset = 0.0
        self._recovering = False
        self._recover_times: list[float] = []
        self._streams_by_ssrc = {VIDEO_SSRC: self._video, AUDIO_SSRC: self._audio}

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

    # -- Start ---------------------------------------------------------------
    def start(self) -> None:
        if getattr(self.receiver, "kind", None) != "chromecast":
            self.fail("Natives Cast-Streaming ist nur für Cast-Geräte verfügbar")
            return
        super().start()
        threading.Thread(target=self._watchdog_loop, daemon=True,
                         name="mirror-watchdog").start()

    def run(self, source_desc: str) -> None:
        """Handshake mit dem Gerät und Start der Encoder (im Worker-Thread)."""
        self._source_desc = source_desc
        cc = self.receiver.session.chromecast  # pychromecast-Instanz
        self._ctrl = CastStreamingControl()
        cc.register_handler(self._ctrl)
        # Hängenden Mirror-App-Zustand (z. B. aus abgebrochenem Lauf) sauber
        # zurücksetzen – ohne das antwortet der Receiver oft nicht aufs OFFER.
        with contextlib.suppress(Exception):
            cc.quit_app()
            time.sleep(2)
        self._ctrl.launch()
        # Auf die Mirroring-App warten (langsamere Geräte wie das FLX1
        # brauchen länger), max. ~8 s.
        for _ in range(16):
            if not self._running:
                return
            time.sleep(0.5)
            if getattr(cc.status, "app_id", None) == MIRRORING_APP_ID:
                break
        time.sleep(1)

        self._key = os.urandom(16)
        self._iv = os.urandom(16)
        width, height, fps, bitrate_kbps = self.video_params()
        bitrate = bitrate_kbps * 1000
        target_delay = self.cfg_int("mirror_target_delay_ms", TARGET_DELAY_MS)

        want_audio = bool(self.cfg("mirror_audio")) and self._audio_available()
        offer_video = video_stream(
            0, VIDEO_SSRC, self._key.hex(), self._iv.hex(), width, height, fps, bitrate,
            target_delay=target_delay)
        offer_audio = audio_stream(
            1, AUDIO_SSRC, self._key.hex(), self._iv.hex(),
            target_delay=AUDIO_TARGET_DELAY_MS) if want_audio else None

        answer = None
        for attempt in range(3):  # Retry: erstes OFFER wird manchmal verschluckt
            if not self._running:
                return
            self._ctrl.send_offer(offer_video, offer_audio)
            answer = self._ctrl.wait_answer(10)
            if answer and "udpPort" in answer:
                break
            # Manche Receiver verschlucken ein OFFER mit Audio-Stream komplett;
            # ab dem zweiten Versuch daher nur noch Video anbieten.
            if attempt == 0 and offer_audio is not None:
                offer_audio = None
            time.sleep(1.5)
        if not answer or "udpPort" not in answer:
            raise RuntimeError(f"Cast-Streaming: kein gültiges ANSWER ({answer})")

        # Der Receiver nennt in sendIndexes, welche der angebotenen Streams er
        # tatsächlich will. Fehlt der Audio-Index, läuft die Spiegelung stumm.
        send_indexes = answer.get("sendIndexes")
        self._audio_active = bool(
            offer_audio is not None
            and (send_indexes is None or 1 in send_indexes)
        )

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("", 0))
        self._dest = (self.receiver.host, answer["udpPort"])
        threading.Thread(target=self._rx_loop, daemon=True, name="mirror-rtcp").start()
        threading.Thread(target=self._sr_loop, daemon=True, name="mirror-sr").start()
        self._launch_encoder(width, height, fps, bitrate)
        if self._audio_active:
            self._launch_audio_encoder()

    # -- Verfügbarkeit Systemton ---------------------------------------------
    @staticmethod
    def _audio_available() -> bool:
        return gst_element_exists("opusenc") and audio_source_desc() is not None

    # -- RTCP: Feedback empfangen (NACK -> Retransmit, XR -> Uhr-Offset) ------
    def _rx_loop(self) -> None:
        self._sock.settimeout(0.3)
        while self._running:
            try:
                d, _ = self._sock.recvfrom(2048)
            except (TimeoutError, OSError):
                continue
            rn = rtcp.find_xr_reftime(d)
            if rn is not None:
                off = rtcp.ntp_to_unix(rn) - time.time()
                c = self._clock_offset
                self._clock_offset = off if c == 0.0 else c * 0.85 + off * 0.15
            media_ssrc, fields = rtcp.parse_nacks(d)
            if not fields:
                continue
            stream = self._streams_by_ssrc.get(media_ssrc, self._video)
            self._retransmit(stream, fields)

    def _retransmit(self, stream: _StreamState, fields) -> None:  # noqa: ANN001
        for f8, pid, mask in fields:
            with stream.lock:
                cands = [f for f in stream.buffer if (f & 0xFF) == f8]
                if not cands:
                    continue
                pkts = stream.buffer[max(cands)]
                want = list(pkts) if pid == 0xFFFF else \
                    [pid] + [pid + 1 + i for i in range(8) if mask & (1 << i)]
                resend = [pkts[p] for p in want if p in pkts]
            for p in resend:
                try:
                    self._sock.sendto(p, self._dest)
                except OSError:
                    return

    # -- RTCP: Sender Report (Uhr-Sync, auf TV-Uhr verschoben) ----------------
    def _sr_loop(self) -> None:
        while self._running:
            for stream in (self._video, self._audio):
                if stream is self._audio and not self._audio_active:
                    continue
                if stream.started and stream.last_wall:
                    self._send_sender_report(stream)
            time.sleep(0.2)

    def _send_sender_report(self, stream: _StreamState) -> None:
        sr = rtcp.sender_report(stream.ssrc, stream.last_wall + self._clock_offset,
                                stream.last_rtp, stream.packets, stream.octets)
        with contextlib.suppress(OSError):
            self._sock.sendto(sr, self._dest)

    # -- Encoder --------------------------------------------------------------
    def _launch_encoder(self, width: int, height: int, fps: int, bitrate: int) -> None:
        Gst = gst_init()
        self._Gst = Gst

        encoder = h264_encoder_desc(bitrate // 1000, fps,
                                    str(self.cfg("mirror_encoder") or "auto"))
        src = self._source_desc  # X11 (ximagesrc) oder Wayland (pipewiresrc/wf-recorder)
        desc = (
            f"{src} ! videoconvert ! videoscale add-borders=true "
            f"! video/x-raw,width={width},height={height},pixel-aspect-ratio=1/1 "
            f"! {encoder} "
            f"! video/x-h264,profile=main,stream-format=byte-stream "
            f"! h264parse config-interval=-1 "
            f"! appsink name=sink emit-signals=true sync=false max-buffers=1 drop=true"
        )
        self._pipeline = Gst.parse_launch(desc)
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_video_sample)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_video_error)
        self._pipeline.set_state(Gst.State.PLAYING)

    def _launch_audio_encoder(self) -> None:
        """Systemton (Monitor der Standard-Ausgabe) als Opus-Stream.

        Bewusst eine eigene Pipeline: Fällt die Audioquelle aus (Gerätewechsel,
        fehlender Monitor), stirbt nur sie – das Bild läuft weiter.
        """
        Gst = self._Gst
        src = audio_source_desc()
        if src is None:
            self._audio_active = False
            return
        device = default_monitor_device()
        if device and src.startswith("pulsesrc"):
            src = src.replace("pulsesrc ", f'pulsesrc device="{device}" ', 1)
        desc = (
            f"{src} ! audio/x-raw,rate=48000,channels=2 "
            f"! opusenc bitrate=128000 frame-size=10 inband-fec=false "
            f"! appsink name=asink emit-signals=true sync=false max-buffers=8 drop=true"
        )
        try:
            self._audio_pipeline = Gst.parse_launch(desc)
        except Exception as exc:  # noqa: BLE001
            print(f"[native] Systemton nicht verfügbar ({exc}) – Spiegelung bleibt stumm")
            self._audio_active = False
            return
        self._audio_pipeline.get_by_name("asink").connect("new-sample", self._on_audio_sample)
        bus = self._audio_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_audio_error)
        self._audio_pipeline.set_state(Gst.State.PLAYING)

    def _on_video_error(self, _bus, message) -> None:  # noqa: ANN001
        err, dbg = message.parse_error()
        self.fail(f"GStreamer-Fehler beim Spiegeln: {err}")

    def _on_audio_error(self, _bus, message) -> None:  # noqa: ANN001
        err, _dbg = message.parse_error()
        print(f"[native] Systemton-Pipeline gestoppt: {err} – Bild läuft weiter")
        self._audio_active = False

    # -- Samples -> RTP -------------------------------------------------------
    def _pull(self, sink):  # noqa: ANN001
        """Sample abholen und als (bytes, pts, is_keyframe) liefern."""
        sample = sink.emit("pull-sample")
        if sample is None or not self._running:
            return None
        buf = sample.get_buffer()
        ok, info = buf.map(self._Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        is_key = not (buf.get_flags() & self._Gst.BufferFlags.DELTA_UNIT)
        return data, (buf.pts or 0), is_key

    def _on_video_sample(self, sink):  # noqa: ANN001
        got = self._pull(sink)
        if got is None:
            return self._Gst.FlowReturn.OK
        data, pts, is_key = got
        # Erst ab dem ersten Keyframe senden -> dieses wird Frame 0.
        if not self._video.started:
            if not is_key:
                return self._Gst.FlowReturn.OK
            self._video.started = True
        self._send_frame(self._video, data, is_key=is_key, pts_ns=pts)
        return self._Gst.FlowReturn.OK

    def _on_audio_sample(self, sink):  # noqa: ANN001
        if not self._audio_active:
            return self._Gst.FlowReturn.OK
        got = self._pull(sink)
        if got is None:
            return self._Gst.FlowReturn.OK
        data, pts, _ = got
        # Opus-Frames sind voneinander unabhängig -> immer "Keyframe".
        self._audio.started = True
        self._send_frame(self._audio, data, is_key=True, pts_ns=pts)
        return self._Gst.FlowReturn.OK

    def _send_frame(self, stream: _StreamState, data: bytes, *, is_key: bool,
                    pts_ns: int) -> None:
        fid = stream.fid
        raw_ts = int(pts_ns * stream.clock // 1_000_000_000)
        if stream.rebase:
            # Nach einem Capture-Neustart beginnt die Pipeline-PTS wieder bei ~0.
            # Offset so wählen, dass die RTP-Zeit nahtlos vorwärts weiterläuft
            # (sonst springt der Zeitstempel zurück -> Receiver verwirft/bricht ab).
            step = stream.clock // 30 or 1
            stream.rtp_offset = (stream.last_rtp + step - raw_ts) & 0xFFFFFFFF
            stream.rebase = False
        rtp_ts = (raw_ts + stream.rtp_offset) & 0xFFFFFFFF
        enc = encrypt_frame(data, fid, self._key, self._iv)
        packets, stream.seq = rtp.packetize(
            payload=enc, frame_id=fid, is_key=is_key, reference_frame_id=fid - 1,
            ssrc=stream.ssrc, payload_type=stream.payload_type, rtp_timestamp=rtp_ts,
            seq=stream.seq,
        )
        with stream.lock:
            stream.buffer[fid] = dict(enumerate(packets))
            for old in [f for f in stream.buffer if f < fid - RETAIN_FRAMES]:
                del stream.buffer[old]
        for p in packets:
            try:
                self._sock.sendto(p, self._dest)
            except OSError:
                break
            stream.octets += len(p) - 12
        stream.packets += len(packets)
        stream.fid = fid + 1
        stream.last_rtp = rtp_ts
        stream.last_wall = time.time()

    # -- Selbstheilung: eingefrorene Capture erkennen + neu starten ----------
    def _watchdog_loop(self) -> None:
        """Erkennt eine eingefrorene Capture (kein neues Frame) und startet sie neu.

        Auf dem FLX1 kann eine Output-Zustandsänderung (Dimmen/Rotation/…) die
        wlr-screencopy-Capture killen ("invalid buffer dimensions") -> wf-recorder
        stirbt, das TV-Bild friert ein. Die Cast-Session bleibt dabei intakt, also
        genügt ein Neustart von Capture + Encoder-Pipeline.
        """
        while self._running:
            time.sleep(2)
            if self._recovering or not self._video.started:
                continue
            last = self._video.last_wall
            if last and time.time() - last > FREEZE_TIMEOUT_S:
                self._recover()

    def _recover(self) -> None:
        if not self._running or self._recovering:
            return
        now = time.time()
        self._recover_times = [t for t in self._recover_times if now - t < 60]
        if len(self._recover_times) >= MAX_RECOVERIES:
            self.fail("Bildschirmaufnahme friert wiederholt ein – Spiegelung beendet")
            return
        self._recover_times.append(now)
        self._recovering = True
        print("[native] Capture eingefroren – starte neu (Auto-Recovery)")
        with contextlib.suppress(Exception):
            if self._pipeline is not None:
                self._pipeline.set_state(self._Gst.State.NULL)
                self._pipeline = None
        # Cast-Session/Socket/Schlüssel bleiben; nur neu erfassen + neuer Keyframe.
        self._video.started = False   # nächstes gesendetes Frame wird Keyframe
        self._video.rebase = True     # RTP-Zeit nahtlos fortsetzen
        self._video.last_wall = 0.0
        try:
            source = self._capture.restart_sync() if self._capture else None
            if source is None:
                self.fail("Bildschirmaufnahme abgebrochen – bitte erneut starten "
                          "(Freigabe muss neu bestätigt werden)")
                return
            self._source_desc = source
            width, height, fps, bitrate_kbps = self.video_params()
            self._launch_encoder(width, height, fps, bitrate_kbps * 1000)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"Neustart der Bildschirmaufnahme fehlgeschlagen: {exc}")
            return
        finally:
            self._recovering = False

    # -- Stop ----------------------------------------------------------------
    def stop(self) -> None:
        self._running = False
        self._audio_active = False
        for attr in ("_pipeline", "_audio_pipeline"):
            pipeline = getattr(self, attr, None)
            if pipeline is not None:
                with contextlib.suppress(Exception):
                    pipeline.set_state(self._Gst.State.NULL)
                setattr(self, attr, None)
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._stop_capture()
        # Mirroring-App am TV beenden (-> zurück zum Home). media_controller.stop()
        # greift hier NICHT, da die Mirror-App läuft, nicht der Media-Receiver.
        with contextlib.suppress(Exception):
            self.receiver.session.quit_app()
