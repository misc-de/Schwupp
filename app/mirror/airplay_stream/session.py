"""AirPlay-Bildschirmspiegelung: Sitzungsaufbau und Videostrom.

Ablauf einer Sitzung (Steuerkanal TCP 7000, danach HAP-verschlüsselt):

1. ``GET /info`` – Fähigkeiten des Empfängers (Feature-Bits, Statusbits,
   Versionsstand, Bildschirmgröße).
2. Pair-Verify mit der gespeicherten Kopplung, sonst Pair-Setup mit der PIN,
   die der Fernseher zeigt (:mod:`.hap`).
3. ``SETUP`` der Sitzung (Uhr: NTP oder PTP), dann der Ton-Stream (Typ 96,
   ALAC; ohne Ton ein Platzhalter ohne Pakete – Empfänger erwarten ihn), dann
   der Videostrom (Typ 110). Empfänger, die die Sitzung nicht vor den Streams
   annehmen, bekommen die ältere Reihenfolge (Ton-Stream legt die Sitzung an).
4. TCP zum ``dataPort``: je Bild ein 128-Byte-Kopf und die H.264-Daten in
   AVCC, mit ChaCha20-Poly1305 verschlüsselt (Kopf = AAD). SPS/PPS gehen
   unverschlüsselt als eigenes Konfigurationspaket voraus.
5. Am Leben halten: ``POST /feedback`` alle 2 s, Heartbeat auf dem Datenkanal
   jede Sekunde, Antworten auf die Zeitanfragen (NTP) bzw. Ereignisse.

Die Zeitstempel im Bildkopf sind die Anzeigezeitpunkte auf der Uhr des
Empfängers: Bei NTP ist das unsere Uhr seit dem Systemstart (der Empfänger
gleicht sie über die Zeitanfragen ab), bei PTP leitet sich die Uhr aus den
Zeitstempeln ab, die der Empfänger in seine Antworten schreibt.

Das Protokoll ist anhand öffentlicher Beschreibungen und des Verhaltens
echter Geräte umgesetzt; doubletake (LGPL) hat gezeigt, welche Wege
Fernseher ohne FairPlay annehmen.
"""
from __future__ import annotations

import contextlib
import os
import plistlib
import socket
import struct
import threading
import time
import uuid
from collections.abc import Callable

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from . import hap
from .audio import FEATURE_RFC2198, AudioSender, receiver_takes_alac
from .h264 import Packetizer
from .rtsp import RtspClient, RtspError, StatusError

SOURCE_VERSION = "980.71.1"
DEFAULT_PORT = 7000
NTP_EPOCH_OFFSET = 2208988800          # 1900 -> 1970

# Feature-Bits (Bit-Nummern) und Maskeneigenschaften aus /info
FEATURE_FAIRPLAY = 14
FEATURE_PTP = 41
FEATURE_STREAM_CONNECTIONS = 59
_CORE_UTILS_PAIRING_MASK = (1 << 38) | (1 << 43) | (1 << 46) | (1 << 48)
_THIRD_PARTY_MASK = (1 << 26) | (1 << 51)
_STATUS_PASSWORD = 1 << 7
_MIN_PTP_VERSION = (354, 54, 6)

# Status, mit denen ein Empfänger eine Form oder Reihenfolge ablehnt
_ORDER_REJECTED = {400, 405, 406, 415, 455, 501}
_SHAPE_REJECTED = {400, 406, 415, 455, 501}
_PAIR_SETUP_UNSUPPORTED = {400, 404, 405, 415, 455, 501}

# Platzhalter-Ton: ALAC 44,1 kHz Stereo
_AUDIO_CT_ALAC = 2
_AUDIO_SPF = 352
_AUDIO_FORMAT_ALAC = 0x40000

_FEEDBACK_INTERVAL = 2.0
# Pausen vor den weiteren Versuchen (s): Der Hisense nahm nach dem Schließen der
# Wiedergabe-Sitzung eine Weile keine Verbindung an (Errno 111/104).
_OPEN_RETRY_DELAYS = (1.0, 1.5, 2.0, 2.5, 3.0)
_HEARTBEAT_INTERVAL = 1.0
_PTP_ASYNC_WAIT = 2.0


class MirrorError(Exception):
    """Aufbau oder Übertragung gescheitert – Text ist für die Oberfläche gedacht."""


class PairingRequired(MirrorError):
    """Die gespeicherte Kopplung gilt nicht (mehr); neu koppeln mit PIN."""


class PtpUnavailable(MirrorError):
    """Der Empfänger bot PTP an, lieferte aber keine Uhr – mit NTP neu versuchen."""


# -- Zeit -----------------------------------------------------------------------

def boot_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def compact(ns: int) -> int:
    """Nanosekunden -> 32.32-Festkomma (Sekunden.Bruchteil), wie im Bildkopf."""
    ns = max(ns, 0)
    sec, rest = divmod(ns, 1_000_000_000)
    return ((sec & 0xFFFFFFFF) << 32) | ((rest << 32) // 1_000_000_000)


def ntp_now() -> int:
    """Systemstart-Uhr mit NTP-Epoche, für die Antworten auf Zeitanfragen."""
    ns = boot_ns()
    sec, rest = divmod(ns, 1_000_000_000)
    return (((sec + NTP_EPOCH_OFFSET) & 0xFFFFFFFF) << 32) | ((rest << 32) // 1_000_000_000)


def ntp_reply(request: bytes) -> bytes | None:
    """Antwort (0xd3) auf eine Zeitanfrage (0xd2) des Empfängers, sonst None."""
    if len(request) < 32 or request[0] != 0x80 or request[1] != 0xD2:
        return None
    now = ntp_now()
    reply = bytearray(request[:32])
    reply[0], reply[1] = 0x80, 0xD3
    reply[8:16] = request[24:32]
    reply[16:24] = struct.pack(">Q", now)
    reply[24:32] = struct.pack(">Q", now)
    return bytes(reply)


class PtpClock:
    """Bildet unsere Systemstart-Uhr auf die PTP-Zeitachse des Empfängers ab.

    Der Empfänger nennt in ``X-Apple-RequestReceivedTimestamp`` (+
    ``X-Apple-ProcessingTime``) seine Uhrzeit in Millisekunden; das reicht als
    Anker, ein eigener PTP-Dienst ist nicht nötig. Der Anker läuft nie
    rückwärts, sonst sprängen die Zeitstempel.
    """

    def __init__(self) -> None:
        self.timeline = 0
        self._anchor_ts = 0
        self._anchor_local = 0
        self._lock = threading.Lock()
        self.ready = threading.Event()

    @staticmethod
    def _header_ts(headers: dict[str, str]) -> int | None:
        try:
            received = int(headers["x-apple-requestreceivedtimestamp"])
            processing = int(headers.get("x-apple-processingtime", "0"))
        except (KeyError, ValueError):
            return None
        if received < 0 or processing < 0:
            return None
        return compact((received + processing) * 1_000_000)

    def configure(self, response: dict, headers: dict[str, str], received_local: int) -> bool:
        peer = response.get("timingPeerInfo")
        clock_id = peer.get("ClockID") if isinstance(peer, dict) else None
        ts = self._header_ts(headers)
        if not clock_id or ts is None:
            return False
        with self._lock:
            self.timeline = int(clock_id) & 0xFFFFFFFFFFFFFFFF
            self._anchor_ts, self._anchor_local = ts, received_local
        self.ready.set()
        return True

    def reanchor(self, headers: dict[str, str], received_local: int) -> None:
        ts = self._header_ts(headers)
        if ts is None:
            return
        with self._lock:
            if self._anchor_local and received_local >= self._anchor_local:
                projected = self._anchor_ts + compact(received_local - self._anchor_local)
                ts = max(ts, projected)
            self._anchor_ts, self._anchor_local = ts, received_local

    def update_peer(self, peer: dict) -> None:
        """Neue Zeitachse (Ereignis ``updateTimingPeerInfo``) ohne Zeitsprung."""
        clock_id = peer.get("ClockID")
        if not clock_id:
            return
        now = boot_ns()
        with self._lock:
            if not self._anchor_local:
                self._anchor_ts, self._anchor_local = compact(now), now
            self.timeline = int(clock_id) & 0xFFFFFFFFFFFFFFFF
        self.ready.set()

    def at(self, local_ns: int) -> tuple[int, int] | None:
        with self._lock:
            if not self._anchor_local or not self.timeline:
                return None
            return self._anchor_ts + compact(local_ns - self._anchor_local), self.timeline


# -- Hilfen für /info -------------------------------------------------------------

def _source_version(info: dict, server: str) -> tuple[int, int, int] | None:
    text = str(info.get("sourceVersion") or "")
    if not text and server.startswith("AirTunes/"):
        text = server.split("/", 1)[1].split()[0]
    parts = text.split(".")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        values = [int(p) for p in parts] + [0] * (3 - len(parts))
    except ValueError:
        return None
    return values[0], values[1], values[2]


def _features(info: dict) -> int:
    value = info.get("features", 0)
    if isinstance(value, str):      # mDNS-Schreibweise "0x…,0x…"
        low, _, high = value.partition(",")
        try:
            return int(low, 16) | (int(high, 16) << 32 if high else 0)
        except ValueError:
            return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def prefers_legacy_pairing(info: dict) -> bool:
    """Fernseher anderer Hersteller koppeln mit HKP 3, Apple-Geräte mit Typ 5."""
    features = _features(info)
    return not features & _CORE_UTILS_PAIRING_MASK or bool(features & _THIRD_PARTY_MASK)


def display_size(info: dict) -> tuple[int, int]:
    displays = info.get("displays")
    if isinstance(displays, list) and displays and isinstance(displays[0], dict):
        d = displays[0]
        for wk, hk in (("widthPixels", "heightPixels"), ("width", "height")):
            try:
                w, h = int(d.get(wk, 0)), int(d.get(hk, 0))
            except (TypeError, ValueError):
                continue
            if w > 0 and h > 0:
                return w, h
    return 0, 0


def audio_ports(response: dict) -> tuple[int, int]:
    """Daten- und Steuerport des Ton-Streams aus einer SETUP-Antwort."""
    for stream in response.get("streams") or []:
        if not isinstance(stream, dict) or int(stream.get("type", 0)) != 96:
            continue
        data, control = int(stream.get("dataPort") or 0), int(stream.get("controlPort") or 0)
        connections = stream.get("streamConnections")
        if isinstance(connections, dict):
            rtp = connections.get("streamConnectionTypeRTP") or {}
            rtcp = connections.get("streamConnectionTypeRTCP") or {}
            data = int(rtp.get("streamConnectionKeyPort") or data)
            control = int(rtcp.get("streamConnectionKeyPort") or control)
        return data, control
    return 0, 0


def mac_from_uuid(value: str) -> str:
    digits = value.replace("-", "").lower()[:12].ljust(12, "0")
    parts = ["02"] + [digits[i:i + 2] for i in range(2, 12, 2)]
    return ":".join(parts).upper()


def _scid() -> int:
    return time.time_ns() & 0x7FFFFFFFFFFFFFFF


def bind_consecutive_udp(count: int, port_range: tuple[int, int] | None):
    """``count`` aufeinanderfolgende UDP-Ports (Zeit, Ton-Steuerung, Ton-Daten)."""
    candidates = range(port_range[0], port_range[1] - count + 2) if port_range else ()
    for base in candidates:
        socks = []
        try:
            for offset in range(count):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socks.append(s)
                s.bind(("", base + offset))
            return socks
        except OSError:
            for s in socks:
                s.close()
    # Bereich belegt (oder keiner gewünscht): freie Ports vom System
    for _ in range(32):
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        first.bind(("", 0))
        base = first.getsockname()[1]
        socks = [first]
        try:
            for offset in range(1, count):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socks.append(s)
                s.bind(("", base + offset))
            return socks
        except OSError:
            for s in socks:
                s.close()
    raise MirrorError("Keine freien UDP-Ports für die Zeitsynchronisation")


# -- Sitzung --------------------------------------------------------------------

class MirrorSession:
    """Eine Spiegel-Sitzung zu einem AirPlay-Empfänger.

    Benutzung: :meth:`connect` (Kopplung), :meth:`setup` (Streams aushandeln),
    dann je kodiertem Bild :meth:`send_access_unit`, am Ende :meth:`close`.
    """

    def __init__(self, host: str, *, port: int = DEFAULT_PORT,
                 credentials: hap.Credentials | None = None,
                 store_credentials: Callable[[hap.Credentials, bool], None] | None = None,
                 pin_callback: Callable[[], str | None] | None = None,
                 name: str = "Schwupp", port_range: tuple[int, int] | None = (60000, 60010),
                 latency_ms: int = 150, force_ntp: bool = False,
                 on_failure: Callable[[str], None] | None = None,
                 log: Callable[[str], None] = print) -> None:
        self.host = host
        self.port = port
        self.credentials = credentials
        self.store_credentials = store_credentials
        self.pin_callback = pin_callback
        self.name = name
        self.port_range = port_range
        self.latency_ms = max(20, min(int(latency_ms), 2000))
        self.force_ntp = force_ntp
        self.on_failure = on_failure
        self.log = log

        self.info: dict = {}
        self.server = ""
        self.rtsp: RtspClient | None = None
        self.shared_secret = b""
        self._write_key = b""
        self._read_key = b""

        self.timing = "NTP"
        self.clock: PtpClock | None = None
        self.width = 0
        self.height = 0
        self.session_uri = ""
        self._session_uuid = str(uuid.uuid4()).upper()
        self._peer = host
        self._device_id = ""
        self._audio_scid = 0
        self._audio_uri = ""
        self._session_first = True
        self._record_pending = True
        self._probes_started = False
        self._udp: list[socket.socket] = []
        self._event_sock: socket.socket | None = None
        self._data: socket.socket | None = None
        self._data_lock = threading.Lock()
        self._aead: ChaCha20Poly1305 | None = None
        self._nonce = 0
        self._last_ts = 0
        self._packetizer = Packetizer()
        self._first_frame = threading.Event()
        self._closing = threading.Event()
        self._failed = False
        self.frames_sent = 0
        self.ntp_requests = 0
        self.audio: AudioSender | None = None
        self._audio_key = b""
        self._audio_ports = (0, 0)

    # -- Verbindung + Kopplung ------------------------------------------------
    def _open(self) -> None:
        """Neue Steuerverbindung und ``GET /info``.

        Setzt der Fernseher die Verbindung zurück oder lehnt sie ab – etwa weil
        er eine andere Sitzung gerade erst schließt –, folgen weitere Versuche
        mit wachsenden Pausen (zusammen etwa 10 s).
        """
        attempts = len(_OPEN_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            if self.rtsp is not None:
                self.rtsp.close()
            try:
                self.rtsp = RtspClient(self.host, self.port)
                response = self.rtsp.request("GET", "/info", timeout=10)
                break
            except (OSError, RtspError) as exc:
                self.log(f"[airplay] GET /info, Versuch {attempt + 1}: {exc}")
                if attempt + 1 == attempts:
                    raise MirrorError("Der Fernseher nimmt keine Verbindung an – läuft "
                                      f"gerade eine andere AirPlay-Sitzung? ({exc})") from exc
                time.sleep(_OPEN_RETRY_DELAYS[attempt])
        self.server = response.headers.get("server", "")
        self.info = response.plist()
        self.log(f"[airplay] {self.info.get('model', '?')} ({self.server or '?'}): "
                 f"features {_features(self.info):#x}, status "
                 f"{int(self.info.get('statusFlags', 0) or 0):#x}, "
                 f"Version {self.info.get('sourceVersion', '?')}")

    @property
    def legacy_pairing(self) -> bool:
        return prefers_legacy_pairing(self.info)

    def _pair_headers(self, pairing_id: bytes, pairing_type: int) -> dict[str, str]:
        if pairing_type == hap.PAIRING_LEGACY:
            return {"X-Apple-HKP": str(hap.PAIRING_LEGACY)}
        return {"X-Apple-Client-Name": self.name, "X-Apple-HKP": str(pairing_type),
                "X-Apple-Client-ID": pairing_id.decode()}

    def connect(self) -> None:
        """Verbindet, prüft die Kopplung und schaltet die Verschlüsselung ein."""
        self._open()
        if _features(self.info) and self.info.get("statusFlags", 0) & _STATUS_PASSWORD \
                and self.legacy_pairing:
            raise MirrorError("Der Fernseher verlangt ein Passwort für AirPlay – "
                              "bitte am Fernseher auf „Code“ umstellen")
        if _features(self.info) >> FEATURE_FAIRPLAY & 1:
            raise MirrorError("Dieses Gerät verlangt FairPlay (Apple TV, Mac) – dafür "
                              "doubletake installieren oder HLS wählen")
        if self.credentials is not None:
            try:
                self._verify(self.credentials)
                self.log("[airplay] mit gespeicherter Kopplung angemeldet")
                return
            except PairingRequired as exc:
                self.log(f"[airplay] gespeicherte Kopplung gilt nicht: {exc} – neu koppeln")
                self._open()
        self._pair_with_pin()

    def _verify(self, creds: hap.Credentials) -> None:
        pairing_type = hap.PAIRING_LEGACY if self.legacy_pairing else hap.PAIRING_SCREEN
        headers = self._pair_headers(creds.client_id, pairing_type)
        if pairing_type != hap.PAIRING_LEGACY:
            headers["X-Apple-PD"] = "1"
        verify = hap.PairVerify(creds)
        try:
            m2 = self.rtsp.request("POST", "/pair-verify", verify.m1(),
                                   "application/octet-stream", headers).body
            m4 = self.rtsp.request("POST", "/pair-verify", verify.m3(m2),
                                   "application/octet-stream", headers).body
            shared = verify.finish(m4)
        except hap.PairingError as exc:
            raise PairingRequired(str(exc)) from exc
        except StatusError as exc:
            if exc.status in (401, 403, 470):
                raise PairingRequired(str(exc)) from exc
            raise MirrorError(f"Anmeldung am Fernseher fehlgeschlagen ({exc})") from exc
        self.shared_secret = shared
        cipher, self._write_key, self._read_key = hap.control_cipher(shared)
        self.rtsp.cipher = cipher

    def _pair_with_pin(self) -> None:
        if self.pin_callback is None:
            raise MirrorError("Der Fernseher verlangt einen Code, es steht aber kein "
                              "Eingabefeld zur Verfügung")
        types = [hap.PAIRING_LEGACY] if self.legacy_pairing \
            else [hap.PAIRING_SCREEN, hap.PAIRING_LEGACY]
        for index, pairing_type in enumerate(types):
            setup = hap.PairSetup("", pairing_type)
            headers = self._pair_headers(setup.pairing_id, pairing_type)
            start_headers = dict(headers)
            if pairing_type != hap.PAIRING_LEGACY:
                start_headers["X-Apple-SupportedPINLengths"] = "4"
            start = self.rtsp.request("POST", "/pair-pin-start", headers=start_headers,
                                      allow_error=True)
            if start.status not in (200, 453):
                raise MirrorError(f"Der Fernseher zeigt keinen Code an ({start.status})")
            self.log(f"[airplay] Kopplung mit Code (Typ {pairing_type})")
            pin = self.pin_callback()
            if not pin:
                raise MirrorError("Kopplung abgebrochen")
            setup.pin = pin.strip()
            try:
                creds = self._pair_setup(setup, headers)
            except StatusError as exc:
                if exc.status in _PAIR_SETUP_UNSUPPORTED and index + 1 < len(types):
                    self.log(f"[airplay] Kopplungstyp {pairing_type} abgelehnt ({exc.status})")
                    self._open()
                    continue
                raise MirrorError(f"Kopplung fehlgeschlagen ({exc})") from exc
            except hap.PairingError as exc:
                if exc.code == 2:
                    raise MirrorError("Kopplung fehlgeschlagen – der Code stimmte nicht oder "
                                      "war abgelaufen. Der Fernseher zeigt bei jedem "
                                      "Versuch einen neuen.") from exc
                raise MirrorError(f"Kopplung fehlgeschlagen: {exc}") from exc
            self.credentials = creds
            if self.store_credentials is not None:
                self.store_credentials(creds, pairing_type == hap.PAIRING_LEGACY)
            try:
                self._verify(creds)
            except PairingRequired as exc:
                raise MirrorError(f"Anmeldung nach der Kopplung fehlgeschlagen: {exc}") from exc
            return

    def _pair_setup(self, setup: hap.PairSetup, headers: dict[str, str]) -> hap.Credentials:
        def post(body: bytes) -> bytes:
            return self.rtsp.request("POST", "/pair-setup", body,
                                     "application/octet-stream", headers, timeout=30).body

        m2 = b""
        for _ in range(4):
            m2 = post(setup.m1())
            tlv = hap.tlv_decode(m2)
            error = tlv.get(hap.TLV_ERROR)
            if not error or error[0] != 3:        # 3 = später erneut versuchen
                break
            delay = int.from_bytes(tlv.get(hap.TLV_RETRY_DELAY, b"\x01"), "little")
            if delay > 30:
                break
            time.sleep(delay)
        m4 = post(setup.m3(m2))
        m6 = post(setup.m5(m4))
        return setup.finish(m6)

    # -- SETUP ----------------------------------------------------------------
    def _use_ptp(self) -> bool:
        if self.force_ntp or not self.shared_secret:
            return False
        version = _source_version(self.info, self.server)
        return bool(_features(self.info) >> FEATURE_PTP & 1) and version is not None \
            and version >= _MIN_PTP_VERSION

    def _session_plist(self, device_id: str) -> dict:
        request = {
            "deviceID": device_id, "macAddress": device_id,
            "sessionUUID": self._session_uuid, "sourceVersion": SOURCE_VERSION,
            "isScreenMirroringSession": True, "timingProtocol": self.timing,
            "osBuildVersion": "13F69", "model": "Linux", "name": self.name,
        }
        if self.timing == "NTP":
            request["timingPort"] = self._udp[0].getsockname()[1]
        else:
            peer = {"ID": str(uuid.uuid4()).upper(), "SupportsClockPortMatchingOverride": True,
                    "DeviceType": 0, "Addresses": [self.rtsp.local_host]}
            request["timingPeerInfo"] = peer
            request["timingPeerList"] = [peer]
        return request

    def _setup(self, uri: str, payload: dict) -> tuple[dict, dict[str, str], int]:
        streams = payload.get("streams") or [{}]
        what = {96: "Ton", 110: "Video"}.get(streams[0].get("type"), "Sitzung")
        try:
            response = self.rtsp.request_plist("SETUP", uri, payload, timeout=15)
        except (StatusError, RtspError) as exc:
            self.log(f"[airplay] SETUP {what}: {exc}")
            raise
        if not self.session_uri:
            self.session_uri = uri
        body = response.plist()
        self.log(f"[airplay] SETUP {what}: {response.status}, "
                 f"Felder {sorted(body)}")
        return body, response.headers, boot_ns()

    def setup(self, width: int, height: int, audio: bool = False) -> None:
        """Handelt Sitzung, Ton-Stream und Videostrom aus und öffnet den Datenkanal.

        Mit *audio* geht danach der Ton über :meth:`send_audio` mit, sofern der
        Empfänger ALAC nimmt und Ports für den Ton nennt; sonst bleibt der
        Ton-Stream ein Platzhalter.
        """
        if self.rtsp is None or self.rtsp.cipher is None:
            raise MirrorError("Nicht angemeldet")
        self.width, self.height = width, height
        self.timing = "PTP" if self._use_ptp() else "NTP"
        if self.timing == "PTP":
            self.clock = PtpClock()
        self._udp = bind_consecutive_udp(3, self.port_range)
        if self.timing == "NTP":
            threading.Thread(target=self._ntp_loop, daemon=True, name="airplay-ntp").start()
        self._peer = self.rtsp.peer_host
        self._device_id = mac_from_uuid(str(uuid.uuid4()))
        self._audio_scid = _scid()
        self._audio_uri = f"rtsp://{self._peer}:{self.port}/{self._audio_scid}"

        self._setup_control()
        self._setup_audio()
        data_port = self._setup_video()
        self._open_data(data_port)
        if self._record_pending:
            try:
                self.rtsp.request("RECORD", self._audio_uri, headers={
                    "Session": self._session_uuid, "Range": "npt=0-",
                    "RTP-Info": "seq=0;rtptime=0"}, timeout=10)
            except StatusError as exc:
                raise self._explain(exc) from exc
        if audio:
            self._start_audio()
        if self.audio is None:
            # Ohne Ton werden die Ton-Ports nicht gebraucht – nur die Zeit bleibt
            for sock in self._udp[1:]:
                sock.close()
            del self._udp[1:]
        for target, name in ((self._data_monitor, "airplay-data"),
                             (self._feedback_loop, "airplay-feedback"),
                             (self._heartbeat_loop, "airplay-heartbeat")):
            threading.Thread(target=target, daemon=True, name=name).start()
        self.log(f"[airplay] Sitzung steht: {width}×{height}, Uhr {self.timing}, "
                 f"{'Sitzung zuerst' if self._session_first else 'Ton zuerst'}")

    def _after_response(self, response: dict) -> None:
        """Zeitabfragen starten (NTP) und den Ereigniskanal öffnen, sobald genannt."""
        port = int(response.get("timingPort") or 0)
        if self.timing == "NTP" and port and not self._probes_started:
            self._probes_started = True
            threading.Thread(target=self._ntp_probes, args=(self._peer, port), daemon=True,
                             name="airplay-ntp-probe").start()
        event_port = int(response.get("eventPort") or 0)
        if event_port and self._event_sock is None:
            self._connect_events(self._peer, event_port)

    def _configure_clock(self, response: dict, headers: dict[str, str], at: int) -> None:
        if self.clock is None or self.clock.configure(response, headers, at):
            return
        if response.get("asyncPTPClockConfig") and self._event_sock is not None \
                and self.clock.ready.wait(_PTP_ASYNC_WAIT):
            return
        raise PtpUnavailable("Der Fernseher hat keine PTP-Uhr genannt")

    def _setup_control(self) -> None:
        """Sitzung zuerst (aktuelle Empfänger); ältere lehnen das ab."""
        control = self._session_plist(self._device_id)
        control["updateSessionRequest"] = False
        control["combinedGetInfoWithControlSetup"] = True
        try:
            response, headers, at = self._setup(self._audio_uri, control)
        except StatusError as exc:
            if exc.status not in _ORDER_REJECTED:
                raise self._explain(exc) from exc
            self._session_first = False
            self.log(f"[airplay] Sitzung zuerst abgelehnt ({exc.status}) – ältere Reihenfolge")
            return
        self._after_response(response)
        self._configure_clock(response, headers, at)
        self._record_pending = not response.get("skipRecord", False)
        self._apply_info(response.get("info"))

    def _audio_request(self, modern: bool, key: bytes) -> dict:
        stream = {
            "type": 96, "streamConnectionID": self._audio_scid, "ct": _AUDIO_CT_ALAC,
            "spf": _AUDIO_SPF, "sr": 44100, "audioFormat": _AUDIO_FORMAT_ALAC,
            "audioMode": "default", "usingScreen": True, "latencyMin": 0,
            "latencyMax": self.latency_ms * 44100 // 1000, "redundantAudio": 2,
            # Über einen verschlüsselten Kanal verlangen Empfänger einen eigenen
            # 32-Byte-Schlüssel für den Ton – auch wenn keine Pakete folgen.
            "shk": key,
        }
        control_port = self._udp[1].getsockname()[1]
        if modern:
            stream["isMedia"] = False
            stream["supportsDynamicStreamID"] = True
            stream["streamConnections"] = {
                "streamConnectionTypeRTP": {"streamConnectionKeyUseStreamEncryptionKey": True},
                "streamConnectionTypeRTCP": {"streamConnectionKeyPort": control_port},
            }
        else:
            stream["controlPort"] = control_port
        if self._session_first:
            return {"streams": [stream]}
        request = self._session_plist(self._device_id)
        request["streams"] = [stream]
        return request

    def _setup_audio(self) -> None:
        """Ton-Stream als Platzhalter; bei abgelehnter Form einmal die andere."""
        modern = bool(_features(self.info) >> FEATURE_STREAM_CONNECTIONS & 1)
        key = self._audio_key = os.urandom(32)
        try:
            response, headers, at = self._setup(self._audio_uri, self._audio_request(modern, key))
        except StatusError as exc:
            if exc.status not in _SHAPE_REJECTED:
                raise self._explain(exc) from exc
            try:
                response, headers, at = self._setup(self._audio_uri,
                                                    self._audio_request(not modern, key))
            except StatusError as exc2:
                if self.timing == "PTP" and not self._session_first \
                        and exc2.status in _SHAPE_REJECTED:
                    raise PtpUnavailable(str(exc2)) from exc2
                raise self._explain(exc2) from exc2
        self._after_response(response)
        self._audio_ports = audio_ports(response)
        if not self._session_first:
            self._record_pending = not response.get("skipRecord", False)
            self._configure_clock(response, headers, at)
            self._refresh_info()

    def _start_audio(self) -> None:
        data_port, control_port = self._audio_ports
        features = _features(self.info)
        if not data_port or not control_port:
            self.log("[airplay] Der Fernseher nennt keine Ton-Ports – Spiegelung ohne Ton")
            return
        if not receiver_takes_alac(self.info, features):
            self.log("[airplay] Der Fernseher nimmt kein ALAC – Spiegelung ohne Ton")
            return
        self.audio = AudioSender(
            self._udp[2], self._udp[1], (self._peer, data_port), (self._peer, control_port),
            self._audio_key, latency_samples=self.latency_ms * 44100 // 1000,
            rfc2198=bool(features >> FEATURE_RFC2198 & 1), timing=self.timing,
            clock=self._audio_clock, now=boot_ns, log=self.log)
        self.audio.start()

    def _audio_clock(self, local_ns: int) -> tuple[int, int]:
        """Netzzeit für Sync-Pakete: PTP-Achse, sonst Systemstart-Uhr mit NTP-Epoche."""
        mapped = self.clock.at(local_ns) if self.clock is not None else None
        if mapped:
            return mapped
        return compact(local_ns) + (NTP_EPOCH_OFFSET << 32), 0

    def send_audio(self, pcm: bytes, local_ns: int | None = None) -> None:
        """S16LE, 44,1 kHz, Stereo. Vor dem ersten Bild wird verworfen, damit der
        Ton nicht vor dem Bild anläuft."""
        if self.audio is not None and self._first_frame.is_set() and not self._closing.is_set():
            self.audio.push(pcm, local_ns)

    def _setup_video(self) -> int:
        """Videostrom anmelden; liefert den TCP-Port für die Bilder."""
        video_scid = _scid()
        video = {
            "type": 110, "streamConnectionID": video_scid, "latencyMs": self.latency_ms,
            "timestampInfo": [{"name": n} for n in ("SubSu", "BePxT", "AfPxT", "BefEn", "EmEnc")],
            "shk": self._write_key[:16], "shiv": self._read_key[:16],
        }
        try:
            response, _headers, _at = self._setup(
                f"rtsp://{self._peer}:{self.port}/{video_scid}", {"streams": [video]})
        except StatusError as exc:
            raise self._explain(exc) from exc
        self._after_response(response)
        self._aead = ChaCha20Poly1305(hap.video_key(self.shared_secret, video_scid))
        for stream in response.get("streams") or []:
            if isinstance(stream, dict) and int(stream.get("type", 0)) == 110 \
                    and int(stream.get("dataPort") or 0):
                return int(stream["dataPort"])
        raise MirrorError("Der Fernseher hat keinen Videokanal geöffnet")

    def _open_data(self, port: int) -> None:
        try:
            self._data = socket.create_connection((self._peer, port), timeout=5)
        except OSError as exc:
            raise MirrorError(f"Videokanal nicht erreichbar ({exc})") from exc
        self._data.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._data.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)

    def _apply_info(self, update) -> None:  # noqa: ANN001
        if isinstance(update, dict):
            self.info = {**self.info, **update}

    def _refresh_info(self) -> None:
        with contextlib.suppress(StatusError):
            response = self.rtsp.request("GET", "/info", timeout=3)
            self._apply_info(response.plist())

    @staticmethod
    def _explain(exc: StatusError) -> MirrorError:
        if exc.status == 401:
            return MirrorError("Der Fernseher verlangt ein Passwort für AirPlay – "
                               "bitte am Fernseher auf „Code“ umstellen")
        if exc.status in (403, 470):
            return PairingRequired(f"Der Fernseher hat die Spiegelung abgelehnt ({exc.status})")
        return MirrorError(f"Der Fernseher hat die Spiegelung abgelehnt ({exc})")

    def size_from_receiver(self) -> tuple[int, int]:
        return display_size(self.info)

    # -- Zeit (NTP) -----------------------------------------------------------
    def _ntp_loop(self) -> None:
        sock = self._udp[0]
        sock.settimeout(0.5)
        while not self._closing.is_set():
            try:
                data, addr = sock.recvfrom(128)
            except (TimeoutError, OSError):
                if self._closing.is_set() or sock.fileno() < 0:
                    return
                continue
            reply = ntp_reply(data)
            if reply is not None:
                self.ntp_requests += 1
                if self.ntp_requests <= 3:
                    self.log(f"[airplay] Zeitanfrage {self.ntp_requests} von {addr[0]}")
                with contextlib.suppress(OSError):
                    sock.sendto(reply, addr)

    def _ntp_probes(self, host: str, port: int) -> None:
        for seq in (1, 2, 3):
            request = bytearray(32)
            request[0], request[1] = 0x80, 0xD2
            request[2:4] = struct.pack(">H", seq)
            request[24:32] = struct.pack(">Q", ntp_now())
            try:
                self._udp[0].sendto(bytes(request), (host, port))
            except (OSError, IndexError):
                return
            if self._closing.wait(0.1):
                return

    # -- Ereigniskanal --------------------------------------------------------
    def _connect_events(self, host: str, port: int) -> None:
        try:
            sock = socket.create_connection((host, port), timeout=3)
        except OSError as exc:
            raise MirrorError(f"Ereigniskanal nicht erreichbar ({exc})") from exc
        self._event_sock = sock
        cipher = hap.event_cipher(self.shared_secret)
        threading.Thread(target=self._event_loop, args=(sock, cipher), daemon=True,
                         name="airplay-events").start()

    def _event_loop(self, sock: socket.socket, cipher: hap.HapCipher) -> None:
        from .rtsp import content_length

        buffer = b""
        sock.settimeout(None)
        while not self._closing.is_set():
            try:
                chunk = sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            try:
                buffer += cipher.decrypt(chunk)
            except hap.PairingError:
                return
            while b"\r\n\r\n" in buffer:
                head, _, rest = buffer.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                headers = {}
                for line in lines[1:]:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
                try:
                    length = content_length(headers)
                except RtspError:
                    return
                if len(rest) < length:
                    break
                body, buffer = rest[:length], rest[length:]
                self._handle_event(lines[0], body)
                reply = (f"RTSP/1.0 200 OK\r\nCSeq: {headers.get('cseq', '0')}\r\n"
                         "Content-Length: 0\r\n\r\n").encode()
                try:
                    sock.sendall(cipher.encrypt(reply))
                except OSError:
                    return

    def _handle_event(self, request_line: str, body: bytes) -> None:
        if self.clock is None or not body or not request_line.startswith("POST /command"):
            return
        try:
            command = plistlib.loads(body)
        except Exception:  # noqa: BLE001
            return
        if isinstance(command, dict) and command.get("type") == "updateTimingPeerInfo" \
                and isinstance(command.get("value"), dict):
            self.clock.update_peer(command["value"])

    # -- Am Leben halten ------------------------------------------------------
    def _feedback_loop(self) -> None:
        while not self._closing.is_set():
            try:
                response = self.rtsp.request("POST", "/feedback", timeout=5, allow_error=True)
            except RtspError as exc:
                self._fail(f"Verbindung zum Fernseher verloren ({exc})")
                return
            if self.clock is not None:
                self.clock.reanchor(response.headers, boot_ns())
            if self._closing.wait(_FEEDBACK_INTERVAL):
                return

    def _heartbeat_loop(self) -> None:
        if not self._wait_first_frame():
            return
        header = bytearray(128)
        header[4], header[6] = 0x02, 0x1E
        while not self._closing.wait(_HEARTBEAT_INTERVAL):
            if not self._write(bytes(header)):
                return

    def _wait_first_frame(self) -> bool:
        while not self._first_frame.wait(0.5):
            if self._closing.is_set():
                return False
        return not self._closing.is_set()

    def _data_monitor(self) -> None:
        sock = self._data
        if sock is None:
            return
        sock.settimeout(None)
        while not self._closing.is_set():
            try:
                chunk = sock.recv(4096)
            except OSError:
                chunk = b""
            if not chunk:
                self._fail("Der Fernseher hat die Spiegelung beendet")
                return

    def _fail(self, message: str) -> None:
        if self._closing.is_set() or self._failed:
            return
        self._failed = True
        if self.on_failure is not None:
            self.on_failure(message)

    # -- Bilder ---------------------------------------------------------------
    def _timestamp(self, local_ns: int) -> tuple[int, int]:
        target = local_ns + self.latency_ms * 1_000_000
        mapped = self.clock.at(target) if self.clock is not None else None
        ts, timeline = mapped if mapped else (compact(target), 0)
        if ts <= self._last_ts:
            ts = self._last_ts + 1
        self._last_ts = ts
        return ts, timeline

    def _write(self, data: bytes) -> bool:
        sock = self._data
        if sock is None:
            return False
        with self._data_lock:
            try:
                sock.sendall(data)
            except OSError as exc:
                self._fail(f"Videokanal unterbrochen ({exc})")
                return False
        return True

    def restart_video(self) -> None:
        """Neuer Encoder: wieder auf ein Schlüsselbild warten, SPS/PPS neu senden."""
        self._packetizer = Packetizer()

    def send_access_unit(self, annexb: bytes, local_ns: int | None = None) -> bool:
        """Schickt eine kodierte Zugriffseinheit. False = (noch) nicht gesendet."""
        if self._aead is None or self._closing.is_set():
            return False
        frame = self._packetizer.packetize(annexb)
        if not self._packetizer.ready(frame):
            return False
        ts, timeline = self._timestamp(boot_ns() if local_ns is None else local_ns)
        if frame.keyframe and self._packetizer.needs_config():
            config = self._packetizer.config()
            header = bytearray(128)
            struct.pack_into("<I", header, 0, len(config))
            header[4], header[5], header[6], header[7] = 0x01, 0x00, 0x16, 0x01
            struct.pack_into("<Q", header, 8, ts)
            for offset in (16, 40, 56):
                struct.pack_into("<ff", header, offset, float(self.width), float(self.height))
            if not self._write(bytes(header) + config):
                return False
            self._packetizer.configured()
        header = bytearray(128)
        struct.pack_into("<I", header, 0, len(frame.payload) + 16)
        header[5] = 0x10 if frame.keyframe else 0x00
        struct.pack_into("<Q", header, 8, ts)
        struct.pack_into("<Q", header, 40, timeline)
        sealed = self._aead.encrypt(hap.nonce_for(self._nonce), frame.payload, bytes(header))
        self._nonce += 1
        if not self._write(bytes(header) + sealed):
            return False
        self.frames_sent += 1
        self._first_frame.set()
        return True

    # -- Ende -----------------------------------------------------------------
    def close(self) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        if self.audio is not None:
            self.audio.close()
        if self.rtsp is not None and self.session_uri:
            with contextlib.suppress(Exception):
                self.rtsp.request("TEARDOWN", self.session_uri, timeout=3, allow_error=True)
        for sock in [self._data, self._event_sock, *self._udp]:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    sock.close()
        self._udp = []
        if self.rtsp is not None:
            self.rtsp.close()
