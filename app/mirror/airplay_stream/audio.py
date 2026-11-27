"""Ton für die AirPlay-Spiegelung: ALAC-Rahmen als RTP über UDP.

Der Ton läuft getrennt vom Bild über zwei UDP-Ports, die der Empfänger beim
SETUP des Ton-Streams nennt:

* **Daten:** je 352 Abtastwerte (44,1 kHz, 16 Bit, Stereo) ein ALAC-Rahmen im
  unkomprimierten Modus („verbatim“) – das braucht keinen Encoder und kostet
  rund 1,4 Mbit/s. RTP-Kopf (Typ 0x60, SSRC 0), Nutzdaten mit
  ChaCha20-Poly1305 verschlüsselt: Schlüssel ist der eigene ``shk`` aus dem
  SETUP, die Nonce ein Zähler (8 Byte, hinten angehängt), AAD sind
  Zeitstempel und SSRC aus dem Kopf. Empfänger mit Feature-Bit 61 bekommen
  RFC 2198 (Typ 0x61): der aktuelle Rahmen plus die zwei vorigen, deren
  Kopfteil unverschlüsselt bleibt.
* **Steuerung:** Sync-Pakete (NTP 0xd4, PTP 0xd7) verbinden RTP-Zeit und Uhr
  – „Rahmen X spielt zur Zeit T“, mit dem Wiedergabepuffer als Abstand –,
  und der Empfänger fordert verlorene Pakete an (0xd5), die als 0xd6 erneut
  kommen.
"""
from __future__ import annotations

import contextlib
import socket
import struct
import threading
from collections import OrderedDict
from collections.abc import Callable

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

SAMPLE_RATE = 44100
CHANNELS = 2
FRAME_SAMPLES = 352
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2          # S16LE

PT_DATA = 0x60
PT_RED = 0x61
PT_SYNC_NTP = 0xD4
PT_SYNC_PTP = 0xD7
PT_RETRANSMIT_REQUEST = 0xD5
PT_RETRANSMIT_RESPONSE = 0xD6
REDUNDANCY = 2
MAX_DATAGRAM = 1472
HISTORY = 512
SYNC_INTERVAL = 1.0

# Formatmasken (supportedFormats.screenStream) und Feature-Bits dazu
FORMAT_ALAC = 0x00040000
FEATURE_ALAC = 19
FEATURE_RFC2198 = 61


def receiver_takes_alac(info: dict, features: int) -> bool:
    """Nimmt der Empfänger ALAC für Bildschirmton? Ohne Angabe: ja (Vorgabe)."""
    formats = info.get("supportedFormats")
    if isinstance(formats, dict) and "screenStream" in formats:
        try:
            mask = int(formats["screenStream"]) & 0xFFFFFFFFFFFFFFFF
        except (TypeError, ValueError):
            mask = 0
        return mask == 0 or bool(mask & FORMAT_ALAC)
    format_bits = features & ((1 << 18) | (1 << 19) | (1 << 20) | (1 << 21))
    return not format_bits or bool(features >> FEATURE_ALAC & 1)


# -- ALAC -----------------------------------------------------------------------

def alac_verbatim(pcm: bytes) -> bytes:
    """Ein Stereo-ALAC-Rahmen ohne Kompression aus S16LE-PCM.

    Kopf: Elementtyp CPE (3 Bit), Instanz 0 (4), 12 ungenutzte Bits,
    „hat Größe“ (1), Zusatzbytes 0 (2), verbatim (1), Anzahl Abtastwerte (32);
    dann jeder Wert als 16 Bit big-endian, zum Schluss das Ende-Element (3 Bit 7).
    Gefüllt wird auf ganze Bytes.
    """
    frames = len(pcm) // (CHANNELS * 2)
    acc = 0b001                       # TYPE_CPE
    acc = (acc << 4) | 0              # elementInstanceTag
    acc = (acc << 12) | 0             # unused
    acc = (acc << 1) | 1              # hasSize
    acc = (acc << 2) | 0              # extraBytes
    acc = (acc << 1) | 1              # verbatim
    acc = (acc << 32) | frames
    head_bits = 3 + 4 + 12 + 1 + 2 + 1 + 32           # 55 Bit
    samples = struct.unpack(f"<{frames * CHANNELS}h", pcm[:frames * CHANNELS * 2])
    body = struct.pack(f">{len(samples)}h", *samples)
    # 55 Bit Kopf + Werte + 3 Bit Ende = Bitstrom, der um 1 Bit gegen Bytes versetzt ist
    value = (acc << (len(body) * 8)) | int.from_bytes(body, "big")
    value = (value << 3) | 0b111
    bits = head_bits + len(body) * 8 + 3
    pad = (-bits) % 8
    return (value << pad).to_bytes((bits + pad) // 8, "big")


# -- RFC 2198 -------------------------------------------------------------------

def red_payload(primary: bytes, primary_rtp: int,
                previous: list[tuple[bytes, int]], max_bytes: int) -> bytes:
    """RFC-2198-Nutzlast: die neuesten Vorgänger (höchstens zwei), dann der Rahmen."""
    selected: list[tuple[bytes, int]] = []
    used = 1 + len(primary)
    for payload, rtp in reversed(previous):
        if len(selected) >= REDUNDANCY:
            break
        offset = (primary_rtp - rtp) & 0xFFFFFFFF
        if offset > 0x3FFF or len(payload) > 0x3FF or used + 4 + len(payload) > max_bytes:
            break
        selected.append((payload, rtp))
        used += 4 + len(payload)
    selected.reverse()
    head = bytearray()
    for payload, rtp in selected:
        offset = (primary_rtp - rtp) & 0xFFFFFFFF
        head += bytes((0x80 | PT_DATA, (offset >> 6) & 0xFF,
                       ((offset & 0x3F) << 2) | (len(payload) >> 8), len(payload) & 0xFF))
    head.append(PT_DATA)
    return bytes(head) + b"".join(p for p, _ in selected) + primary


def red_header_length(payload: bytes) -> int:
    """Länge des unverschlüsselten RFC-2198-Kopfteils."""
    offset = 0
    while True:
        if offset >= len(payload):
            raise ValueError("RFC-2198-Nutzlast ohne Hauptkopf")
        if payload[offset] & 0x7F != PT_DATA:
            raise ValueError("RFC-2198-Block mit falschem Typ")
        if not payload[offset] & 0x80:
            return offset + 1
        offset += 4


# -- Pakete ---------------------------------------------------------------------

class AudioPacketizer:
    """Baut verschlüsselte RTP-Pakete und merkt sie sich für Nachforderungen."""

    def __init__(self, key: bytes) -> None:
        self._aead = ChaCha20Poly1305(key)
        self._nonce = 0
        self.history: OrderedDict[int, bytes] = OrderedDict()

    def packet(self, payload: bytes, rtp: int, seq: int, payload_type: int = PT_DATA,
               clear_prefix: int = 0) -> bytes:
        header = struct.pack(">BBHII", 0x80, payload_type, seq & 0xFFFF, rtp & 0xFFFFFFFF, 0)
        nonce = struct.pack("<Q", self._nonce)
        self._nonce += 1
        sealed = self._aead.encrypt(b"\x00" * 4 + nonce, payload[clear_prefix:], header[4:12])
        return header + payload[:clear_prefix] + sealed + nonce

    def remember(self, seq: int, packet: bytes) -> None:
        self.history[seq & 0xFFFF] = packet
        self.history.move_to_end(seq & 0xFFFF)
        while len(self.history) > HISTORY:
            self.history.popitem(last=False)

    @staticmethod
    def max_plain_payload() -> int:
        return MAX_DATAGRAM - 12 - 16 - 8


def sync_packet(timing: str, network_time: int, timeline: int, rtp_now: int,
                latency_samples: int, first: bool) -> bytes:
    """„Rahmen rtp_now − Puffer spielt zur Zeit network_time“ (NTP oder PTP)."""
    playhead = (rtp_now - latency_samples) & 0xFFFFFFFF
    flags = 0x90 if first else 0x80
    if timing == "PTP":
        seconds, fraction = network_time >> 32, network_time & 0xFFFFFFFF
        nanos = seconds * 1_000_000_000 + ((fraction * 1_000_000_000) >> 32)
        return struct.pack(">BBHIQIQ", flags, PT_SYNC_PTP, 4, playhead, nanos, playhead,
                           timeline & 0xFFFFFFFFFFFFFFFF)
    return struct.pack(">BBHIQI", flags, PT_SYNC_NTP, 4, playhead,
                       network_time & 0xFFFFFFFFFFFFFFFF, rtp_now & 0xFFFFFFFF)


def retransmit_answers(request: bytes, history: dict[int, bytes]) -> list[bytes]:
    """Antworten auf eine Nachforderung (0xd5); unbekannte Folge beendet sie."""
    if len(request) != 8 or request[0] != 0x80 or request[1] != PT_RETRANSMIT_REQUEST:
        return []
    request_seq, first, count = struct.unpack(">HHH", request[2:8])
    answers = []
    for offset in range(min(count, len(history) + 1)):
        seq = (first + offset) & 0xFFFF
        original = history.get(seq)
        head = struct.pack(">BBH", 0x80, PT_RETRANSMIT_RESPONSE, request_seq)
        if original is None:
            answers.append(head + struct.pack(">HH", seq, 0))
            break
        answers.append(head + original)
    return answers


# -- Sender ---------------------------------------------------------------------

class AudioSender:
    """Nimmt PCM entgegen, teilt es in Rahmen und schickt sie im Takt der Uhr.

    *clock(local_ns)* liefert ``(Netzzeit, Zeitachse)`` für Sync-Pakete – dieselbe
    Uhr wie fürs Bild, ohne Wiedergabepuffer (den trägt der Abstand im
    Sync-Paket). Die RTP-Zeit läuft gleichmäßig mit den Abtastwerten; jede
    Sekunde wird sie neu an die Uhr gebunden, so folgt sie der Soundkarte.
    """

    def __init__(self, data_sock: socket.socket, ctrl_sock: socket.socket,
                 data_addr: tuple[str, int], ctrl_addr: tuple[str, int], key: bytes, *,
                 latency_samples: int, rfc2198: bool, timing: str,
                 clock: Callable[[int], tuple[int, int]],
                 now: Callable[[], int], log: Callable[[str], None] = print) -> None:
        self.data_sock = data_sock
        self.ctrl_sock = ctrl_sock
        self.data_addr = data_addr
        self.ctrl_addr = ctrl_addr
        self.latency_samples = latency_samples
        self.rfc2198 = rfc2198
        self.timing = timing
        self.clock = clock
        self.now = now
        self.log = log
        self.packets = AudioPacketizer(key)
        self._pending = b""
        self._seq = 1
        self._rtp = FRAME_SAMPLES                  # RECORD: rtptime=0, erster Rahmen danach
        self._last_rtp = 0
        self._last_local = 0
        self._previous: list[tuple[bytes, int]] = []
        self._started = False
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self.frames_sent = 0

    def start(self) -> None:
        threading.Thread(target=self._control_loop, daemon=True, name="airplay-audio-ctrl").start()
        threading.Thread(target=self._sync_loop, daemon=True, name="airplay-audio-sync").start()

    def push(self, pcm: bytes, local_ns: int | None = None) -> None:
        """S16LE-Stereo-PCM mit 44,1 kHz; *local_ns* = Zeit des ersten Werts."""
        if self._closing.is_set():
            return
        local = self.now() if local_ns is None else local_ns
        # Was schon wartet, ist älter: der erste neue Rahmen beginnt um so viel früher
        pending_ns = len(self._pending) // (CHANNELS * 2) * 1_000_000_000 // SAMPLE_RATE
        self._pending += pcm
        start = local - pending_ns
        index = 0
        while len(self._pending) >= FRAME_BYTES:
            frame, self._pending = self._pending[:FRAME_BYTES], self._pending[FRAME_BYTES:]
            frame_ns = start + index * FRAME_SAMPLES * 1_000_000_000 // SAMPLE_RATE
            self._send(alac_verbatim(frame), frame_ns)
            index += 1

    def _send(self, payload: bytes, local_ns: int) -> None:
        with self._lock:
            rtp, seq = self._rtp, self._seq
            if not self._started:
                self._started = True
                self._last_rtp, self._last_local = rtp, local_ns
                self._sync(local_ns, rtp, first=True)
            if self.rfc2198:
                red = red_payload(payload, rtp, self._previous,
                                  AudioPacketizer.max_plain_payload())
                packet = self.packets.packet(red, rtp, seq, PT_RED, red_header_length(red))
                self.packets.remember(seq, self.packets.packet(payload, rtp, seq))
            else:
                packet = self.packets.packet(payload, rtp, seq)
                self.packets.remember(seq, packet)
            self._previous = (self._previous + [(payload, rtp)])[-REDUNDANCY:]
            self._last_rtp, self._last_local = rtp, local_ns
            self._rtp = (rtp + FRAME_SAMPLES) & 0xFFFFFFFF
            self._seq = (seq + 1) & 0xFFFF
        try:
            self.data_sock.sendto(packet, self.data_addr)
        except OSError:
            return
        self.frames_sent += 1

    def _sync(self, local_ns: int, rtp: int, first: bool = False) -> None:
        network, timeline = self.clock(local_ns)
        packet = sync_packet(self.timing, network, timeline, rtp, self.latency_samples, first)
        with contextlib.suppress(OSError):
            self.ctrl_sock.sendto(packet, self.ctrl_addr)

    def _sync_loop(self) -> None:
        while not self._closing.wait(SYNC_INTERVAL):
            with self._lock:
                if not self._started:
                    continue
                now = self.now()
                elapsed = max(now - self._last_local, 0)
                rtp = (self._last_rtp + elapsed * SAMPLE_RATE // 1_000_000_000) & 0xFFFFFFFF
                self._sync(now, rtp)

    def _control_loop(self) -> None:
        self.ctrl_sock.settimeout(0.5)
        while not self._closing.is_set():
            try:
                data, addr = self.ctrl_sock.recvfrom(2048)
            except (TimeoutError, OSError):
                if self._closing.is_set() or self.ctrl_sock.fileno() < 0:
                    return
                continue
            if addr[0] != self.ctrl_addr[0]:
                continue
            with self._lock:
                answers = retransmit_answers(data, dict(self.packets.history))
            for answer in answers:
                with contextlib.suppress(OSError):
                    self.ctrl_sock.sendto(answer, addr)

    def close(self) -> None:
        self._closing.set()
        for sock in (self.data_sock, self.ctrl_sock):
            with contextlib.suppress(OSError):
                sock.close()
