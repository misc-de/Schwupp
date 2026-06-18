"""Native Cast-Streaming-Engine — Low-Latency-Mirroring (<1 s, live bestätigt).

Sendet den Bildschirm per echtem Cast-Streaming-Protokoll statt über HLS:
  1. Mirroring-Receiver-App starten + OFFER/ANSWER über den webrtc-Namespace
     (-> ausgehandelter UDP-Port, AES-Key/IV).  [cast_streaming.control]
  2. H.264 mit GStreamer encoden (appsink), je Frame:
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
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time

from .capture import (PortalScreenCast, WfRecorderCapture, is_wayland,
                      pipewire_source_desc, screencast_portal_available,
                      wf_recorder_available, x11_source_desc)
from .cast_streaming import rtp
from .cast_streaming.control import (CastStreamingControl, MIRRORING_APP_ID,
                                     video_stream)
from .cast_streaming.crypto import encrypt_frame
from .engine import MirrorEngine, gst_element_exists

VIDEO_SSRC = 100001
VIDEO_PT = 96
NTP_EPOCH = 2208988800  # Sekunden zwischen 1900 und 1970
TARGET_DELAY_MS = 150   # Playout-Puffer am Receiver


# -- RTCP-Hilfsfunktionen ----------------------------------------------------
def _walk(d: bytes):
    """Iteriert die Sub-Pakete eines RTCP-Compound: (packet_type, offset, size)."""
    off = 0
    while off + 4 <= len(d):
        pt = d[off + 1]
        ln = struct.unpack("!H", d[off + 2:off + 4])[0]
        size = (ln + 1) * 4
        if size <= 0 or off + size > len(d):
            break
        yield pt, off, size
        off += size


def _ntp_to_unix(n: int) -> float:
    return (n >> 32) - NTP_EPOCH + ((n & 0xFFFFFFFF) / (1 << 32))


def _parse_nacks(d: bytes):
    """Cast-Feedback (PT=206, magic 'CAST') -> Liste (frame8, packet_id, bitmask)."""
    for pt, off, size in _walk(d):
        if pt == 206 and d[off + 12:off + 16] == b"CAST":
            fci = d[off + 12:off + size]
            fields = []
            o = 8
            while o + 4 <= len(fci):
                fields.append((fci[o], struct.unpack("!H", fci[o + 1:o + 3])[0], fci[o + 3]))
                o += 4
            return fields
    return None


def _find_xr_reftime(d: bytes):
    """XR (PT=207) Receiver Reference Time (BT=4) -> 64-bit-NTP der TV-Uhr."""
    for pt, off, size in _walk(d):
        if pt == 207:
            o = off + 8
            while o + 4 <= off + size:
                bt = d[o]
                blen = struct.unpack("!H", d[o + 2:o + 4])[0]
                if bt == 4 and o + 12 <= len(d):
                    return struct.unpack("!Q", d[o + 4:o + 12])[0]
                o += 4 + blen * 4
    return None


class NativeMirrorEngine(MirrorEngine):
    name = "native"
    display_name = "Nativ"

    def __init__(self, receiver, server, config) -> None:  # noqa: ANN001
        super().__init__(receiver, server, config)
        self._pipeline = None
        self._Gst = None
        self._ctrl: CastStreamingControl | None = None
        self._sock: socket.socket | None = None
        self._dest = None
        self._portal: PortalScreenCast | None = None
        self._wf: WfRecorderCapture | None = None
        self._source_desc = ""
        self._key = b""
        self._iv = b""
        self._state = {"fid": 0, "seq": 0, "pk": 0, "oct": 0,
                       "started": False, "coff": 0.0, "last_rtp": 0, "last_wall": 0.0}
        self._buf_pkts: dict[int, dict[int, bytes]] = {}
        self._buf_lock = threading.Lock()

    @staticmethod
    def check_available() -> tuple[bool, str]:
        if not gst_element_exists("x264enc"):
            return False, "GStreamer-Element x264enc fehlt (gst-plugins-ugly)"
        try:
            import cryptography  # noqa: F401
        except ImportError:
            return False, "Python-Paket 'cryptography' fehlt"
        return True, "Bereit"

    # -- Start ---------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        if getattr(self.receiver, "kind", None) != "chromecast":
            raise RuntimeError("Natives Cast-Streaming nur für Cast-Geräte verfügbar")
        self._running = True
        fps = int(self.cfg("mirror_fps"))
        if not is_wayland():
            self._begin(x11_source_desc(fps=fps))
        elif screencast_portal_available():
            # Wayland mit ScreenCast-Portal (PipeWire-Handshake, evtl. Dialog).
            self._portal = PortalScreenCast(fps=fps)
            self._portal.start(self._on_portal_ready)
        elif wf_recorder_available():
            # Kein Portal (z. B. phoc ohne xdg-desktop-portal-wlr) -> wf-recorder.
            self._wf = WfRecorderCapture(fps=fps)
            self._begin(self._wf.source_desc())
        else:
            print("[native] Kein Wayland-Capture verfügbar "
                  "(weder ScreenCast-Portal noch wf-recorder)")
            self._running = False

    def _on_portal_ready(self, fd, node_or_err) -> None:  # noqa: ANN001
        if not self._running:
            return  # bereits wieder gestoppt, während der Portal-Dialog lief
        if fd is None:
            # Kein ScreenCast-Portal -> Fallback auf wf-recorder (wlr-screencopy).
            if wf_recorder_available():
                print(f"[native] Portal nicht verfügbar ({node_or_err}); wf-recorder-Fallback")
                self._wf = WfRecorderCapture(fps=int(self.cfg("mirror_fps")))
                self._begin(self._wf.source_desc())
                return
            print(f"[native] Bildschirmfreigabe nicht möglich: {node_or_err}")
            self._running = False
            return
        self._begin(pipewire_source_desc(fd, node_or_err, int(self.cfg("mirror_fps"))))

    def _begin(self, source_desc: str) -> None:
        self._source_desc = source_desc
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            cc = self.receiver.session.chromecast  # pychromecast-Instanz
            self._ctrl = CastStreamingControl()
            cc.register_handler(self._ctrl)
            self._ctrl.launch()
            # Auf die Mirroring-App warten (langsamere Geräte wie das FLX1
            # brauchen länger), max. ~8 s.
            for _ in range(16):
                time.sleep(0.5)
                if getattr(cc.status, "app_id", None) == MIRRORING_APP_ID:
                    break
            time.sleep(1)

            self._key = os.urandom(16)
            self._iv = os.urandom(16)
            height = int(self.cfg("mirror_height"))
            width = (height * 16 // 9) // 2 * 2
            fps = int(self.cfg("mirror_fps"))
            bitrate = int(self.cfg("mirror_bitrate_kbps")) * 1000
            try:
                target_delay = int(self.cfg("mirror_target_delay_ms"))
            except (KeyError, TypeError, ValueError):
                target_delay = TARGET_DELAY_MS

            offer = video_stream(
                0, VIDEO_SSRC, self._key.hex(), self._iv.hex(), width, height, fps, bitrate,
                target_delay=target_delay)
            answer = None
            for _ in range(3):  # Retry: erstes OFFER wird manchmal verschluckt
                if not self._running:
                    return
                self._ctrl.send_offer(offer)
                answer = self._ctrl.wait_answer(10)
                if answer and "udpPort" in answer:
                    break
                time.sleep(1.5)
            if not answer or "udpPort" not in answer:
                raise RuntimeError(f"Cast-Streaming: kein gültiges ANSWER ({answer})")

            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(("", 0))
            self._dest = (self.receiver.host, answer["udpPort"])
            threading.Thread(target=self._rx_loop, daemon=True).start()
            threading.Thread(target=self._sr_loop, daemon=True).start()
            self._launch_encoder(width, height, fps, bitrate)
        except Exception as exc:  # noqa: BLE001
            print(f"[native] Start fehlgeschlagen: {exc}")
            self._running = False

    # -- RTCP: Feedback empfangen (NACK -> Retransmit, XR -> Uhr-Offset) ------
    def _rx_loop(self) -> None:
        self._sock.settimeout(0.3)
        while self._running:
            try:
                d, _ = self._sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            rn = _find_xr_reftime(d)
            if rn is not None:
                off = _ntp_to_unix(rn) - time.time()
                c = self._state["coff"]
                self._state["coff"] = off if c == 0.0 else c * 0.85 + off * 0.15
            fields = _parse_nacks(d)
            if not fields:
                continue
            for f8, pid, mask in fields:
                with self._buf_lock:
                    cands = [f for f in self._buf_pkts if (f & 0xFF) == f8]
                    if not cands:
                        continue
                    pkts = self._buf_pkts[max(cands)]
                    want = list(pkts) if pid == 0xFFFF else \
                        [pid] + [pid + 1 + i for i in range(8) if mask & (1 << i)]
                    resend = [pkts[p] for p in want if p in pkts]
                for p in resend:
                    try:
                        self._sock.sendto(p, self._dest)
                    except OSError:
                        break

    # -- RTCP: Sender Report (Uhr-Sync, auf TV-Uhr verschoben) ----------------
    def _sr_loop(self) -> None:
        while self._running:
            if self._state["started"] and self._state["last_wall"]:
                w = self._state["last_wall"] + self._state["coff"]
                sec = int(w) + NTP_EPOCH
                frac = int((w % 1) * (1 << 32)) & 0xFFFFFFFF
                sr = struct.pack("!BBHIIIIII", 0x80, 200, 6, VIDEO_SSRC, sec, frac,
                                 self._state["last_rtp"] & 0xFFFFFFFF,
                                 self._state["pk"] & 0xFFFFFFFF, self._state["oct"] & 0xFFFFFFFF)
                try:
                    self._sock.sendto(sr, self._dest)
                except OSError:
                    pass
            time.sleep(0.2)

    def _launch_encoder(self, width: int, height: int, fps: int, bitrate: int) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)
        self._Gst = Gst

        src = self._source_desc  # X11 (ximagesrc) oder Wayland (pipewiresrc)
        desc = (
            f"{src} ! videoscale add-borders=true ! videoconvert "
            f"! video/x-raw,width={width},height={height} "
            f"! x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate // 1000} "
            f"key-int-max={fps} ! video/x-h264,profile=main,stream-format=byte-stream "
            f"! h264parse config-interval=-1 "
            f"! appsink name=sink emit-signals=true sync=false max-buffers=1 drop=true"
        )
        self._pipeline = Gst.parse_launch(desc)
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        self._pipeline.set_state(Gst.State.PLAYING)

    def _on_sample(self, sink):  # noqa: ANN001
        sample = sink.emit("pull-sample")
        if sample is None or not self._running:
            return self._Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(self._Gst.MapFlags.READ)
        if not ok:
            return self._Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        is_key = not (buf.get_flags() & self._Gst.BufferFlags.DELTA_UNIT)
        # Erst ab dem ersten Keyframe senden -> dieses wird Frame 0.
        if not self._state["started"]:
            if not is_key:
                return self._Gst.FlowReturn.OK
            self._state["started"] = True
        fid = self._state["fid"]
        rtp_ts = int((buf.pts or 0) * 9 // 100000) & 0xFFFFFFFF  # 90 kHz
        enc = encrypt_frame(data, fid, self._key, self._iv)
        packets, self._state["seq"] = rtp.packetize(
            payload=enc, frame_id=fid, is_key=is_key, reference_frame_id=fid - 1,
            ssrc=VIDEO_SSRC, payload_type=VIDEO_PT, rtp_timestamp=rtp_ts,
            seq=self._state["seq"],
        )
        with self._buf_lock:
            self._buf_pkts[fid] = {i: p for i, p in enumerate(packets)}
            for old in [f for f in self._buf_pkts if f < fid - 90]:
                del self._buf_pkts[old]
        for p in packets:
            try:
                self._sock.sendto(p, self._dest)
            except OSError:
                break
            self._state["oct"] += len(p) - 12
        self._state["pk"] += len(packets)
        self._state["fid"] = fid + 1
        self._state["last_rtp"] = rtp_ts
        self._state["last_wall"] = time.time()
        return self._Gst.FlowReturn.OK

    # -- Stop ----------------------------------------------------------------
    def stop(self) -> None:
        self._running = False
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._pipeline = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._wf is not None:
            self._wf.stop()
            self._wf = None
        # Mirroring-App am TV beenden (-> zurück zum Home). media_controller.stop()
        # greift hier NICHT, da die Mirror-App läuft, nicht der Media-Receiver.
        try:
            self.receiver.session.quit_app()
        except Exception:  # noqa: BLE001
            pass
