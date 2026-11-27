"""H.264 für den AirPlay-Videostrom: Annex B → AVCC und die avcC-Konfiguration.

Der Encoder liefert je Bild eine Zugriffseinheit im Annex-B-Format
(Startcodes). AirPlay will dagegen NAL-Einheiten mit 4-Byte-Längenpräfix (AVCC)
und SPS/PPS getrennt davon als eigenes, unverschlüsseltes Konfigurationspaket.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

NAL_SLICE_IDR = 5
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9


def split_annexb(data: bytes) -> list[bytes]:
    """NAL-Einheiten ohne Startcodes (3 oder 4 Byte) in Reihenfolge."""
    units: list[bytes] = []
    i, n = 0, len(data)
    start = -1
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if start >= 0:
                units.append(data[start:i].rstrip(b"\x00") if i > start else b"")
            i += 3
            start = i
            continue
        i += 1
    if start >= 0 and start < n:
        units.append(data[start:])
    return [u for u in units if u]


def avcc_config(sps: bytes, pps: bytes) -> bytes:
    """avcC-Eintrag (ein SPS, ein PPS, 4-Byte-Längen) plus 4-Byte-Anhang.

    Den Anhang (0x02 0 0 0) schicken echte Sender mit; Empfänger, die ihn
    erwarten, werten das Paket sonst als unvollständig.
    """
    if len(sps) < 4 or not pps:
        raise ValueError("SPS/PPS unvollständig")
    return (bytes((1, sps[1], sps[2], sps[3], 0xFF, 0xE1)) + struct.pack(">H", len(sps)) + sps
            + b"\x01" + struct.pack(">H", len(pps)) + pps + b"\x02\x00\x00\x00")


@dataclass
class Frame:
    payload: bytes = b""     # AVCC, ohne Parametersätze
    keyframe: bool = False
    has_picture: bool = False


@dataclass
class Packetizer:
    """Zerlegt Zugriffseinheiten und merkt sich, wann SPS/PPS neu zu senden sind."""

    sps: bytes = b""
    pps: bytes = b""
    _sent: tuple[bytes, bytes] = field(default=(b"", b""))
    primed: bool = False

    def packetize(self, access_unit: bytes) -> Frame:
        frame = Frame()
        payload = bytearray()
        for nal in split_annexb(access_unit):
            nal_type = nal[0] & 0x1F
            if nal_type == NAL_SPS:
                self.sps = nal
                continue
            if nal_type == NAL_PPS:
                self.pps = nal
                continue
            if nal_type == NAL_AUD:
                continue
            if 1 <= nal_type <= 5:
                frame.has_picture = True
            if nal_type == NAL_SLICE_IDR:
                frame.keyframe = True
            payload += struct.pack(">I", len(nal)) + nal
        frame.payload = bytes(payload)
        return frame

    def ready(self, frame: Frame) -> bool:
        """Gesendet wird erst ab einem Schlüsselbild mit bekannten SPS/PPS."""
        return frame.has_picture and (
            self.primed or (frame.keyframe and bool(self.sps) and bool(self.pps)))

    def needs_config(self) -> bool:
        return not self.primed or (self.sps, self.pps) != self._sent

    def config(self) -> bytes:
        return avcc_config(self.sps, self.pps)

    def configured(self) -> None:
        self._sent = (self.sps, self.pps)
        self.primed = True
