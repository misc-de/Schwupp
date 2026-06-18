"""Cast-RTP-Packetizer: zerlegt einen (verschlüsselten) Frame in UDP-Pakete.

Paketaufbau (nachgebaut nach chromium media/cast rtp_packetizer / rtp_parser):

  RTP-Header (12 B):
    Byte 0   : 0x80              (V=2, P=0, X=0, CC=0)
    Byte 1   : M(1) | PT(7)      (Marker-Bit nur im letzten Paket des Frames)
    Byte 2-3 : sequence number   (global, +1 pro Paket)
    Byte 4-7 : RTP-Timestamp     (pro Frame; Video 90 kHz)
    Byte 8-11: Sender-SSRC
  Cast-Header (6 B, +1 wenn Referenz):
    Byte 12  : bit7 key_frame | bit6 has_reference | bits0-3 ext-count(0)
    Byte 13  : frame_id (untere 8 Bit)
    Byte 14-15: packet_id
    Byte 16-17: max_packet_id
    Byte 18  : reference_frame_id (nur wenn has_reference)
  danach: Payload-Fragment
"""
from __future__ import annotations

import struct

_IP_UDP_OVERHEAD = 28
_RTP_HEADER_LEN = 12
_MTU = 1500
# konservative Nutzlast je Paket (RTP + max. Cast-Header 7)
MAX_PAYLOAD = _MTU - _IP_UDP_OVERHEAD - _RTP_HEADER_LEN - 7


def packetize(
    *,
    payload: bytes,
    frame_id: int,
    is_key: bool,
    reference_frame_id: int,
    ssrc: int,
    payload_type: int,
    rtp_timestamp: int,
    seq: int,
) -> tuple[list[bytes], int]:
    """Erzeugt die UDP-Pakete eines Frames. Gibt (pakete, nächste_seq) zurück."""
    has_ref = not is_key  # Keyframes referenzieren nichts
    num_packets = max(1, (len(payload) + MAX_PAYLOAD - 1) // MAX_PAYLOAD)
    max_packet_id = num_packets - 1

    packets: list[bytes] = []
    for pid in range(num_packets):
        chunk = payload[pid * MAX_PAYLOAD : (pid + 1) * MAX_PAYLOAD]
        last = pid == max_packet_id
        b1 = (0x80 if last else 0x00) | (payload_type & 0x7F)
        rtp = struct.pack(
            "!BBHII", 0x80, b1, seq & 0xFFFF,
            rtp_timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF,
        )
        flags = (0x80 if is_key else 0) | (0x40 if has_ref else 0)
        cast = struct.pack("!BBHH", flags, frame_id & 0xFF, pid, max_packet_id)
        if has_ref:
            cast += struct.pack("!B", reference_frame_id & 0xFF)
        packets.append(rtp + cast + chunk)
        seq += 1
    return packets, seq
