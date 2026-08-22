"""Cast-RTP-Paketisierung – Bit-für-Bit gegen das dokumentierte Wire-Format.

Der Receiver verwirft Frames stumm, wenn ein Flag oder Offset nicht stimmt;
diese Tests sind die einzige Absicherung, die ohne echten Fernseher greift.
"""
from __future__ import annotations

import struct

from app.mirror.cast_streaming import rtp


def _parse(packet: bytes) -> dict:
    """Zerlegt ein Cast-RTP-Paket in seine Felder (Spiegelbild von packetize)."""
    version, b1, seq, ts, ssrc = struct.unpack("!BBHII", packet[:12])
    flags, frame_id, packet_id, max_packet_id = struct.unpack("!BBHH", packet[12:18])
    has_ref = bool(flags & 0x40)
    offset = 19 if has_ref else 18
    return {
        "version": version,
        "marker": bool(b1 & 0x80),
        "payload_type": b1 & 0x7F,
        "seq": seq,
        "timestamp": ts,
        "ssrc": ssrc,
        "key_frame": bool(flags & 0x80),
        "has_reference": has_ref,
        "frame_id": frame_id,
        "packet_id": packet_id,
        "max_packet_id": max_packet_id,
        "reference_frame_id": packet[18] if has_ref else None,
        "payload": packet[offset:],
    }


def _packetize(payload: bytes, **kwargs):
    defaults = dict(frame_id=0, is_key=True, reference_frame_id=-1, ssrc=100001,
                    payload_type=96, rtp_timestamp=0, seq=0)
    defaults.update(kwargs)
    return rtp.packetize(payload=payload, **defaults)


def test_single_packet_keyframe_header():
    packets, next_seq = _packetize(b"abc", rtp_timestamp=12345, seq=7)
    assert len(packets) == 1
    assert next_seq == 8
    f = _parse(packets[0])
    assert f["version"] == 0x80
    assert f["payload_type"] == 96
    assert f["marker"] is True          # einziges = letztes Paket
    assert f["seq"] == 7
    assert f["timestamp"] == 12345
    assert f["ssrc"] == 100001
    assert f["key_frame"] is True
    assert f["has_reference"] is False  # Keyframes referenzieren nichts
    assert f["frame_id"] == 0
    assert f["packet_id"] == 0
    assert f["max_packet_id"] == 0
    assert f["payload"] == b"abc"


def test_delta_frame_carries_reference():
    packets, _ = _packetize(b"x", frame_id=5, is_key=False, reference_frame_id=4)
    f = _parse(packets[0])
    assert f["key_frame"] is False
    assert f["has_reference"] is True
    assert f["reference_frame_id"] == 4


def test_payload_is_split_and_reassembles():
    payload = bytes(range(256)) * 20          # ~5 KB -> mehrere Pakete
    packets, next_seq = _packetize(payload, seq=100)
    assert len(packets) > 1
    assert next_seq == 100 + len(packets)

    fields = [_parse(p) for p in packets]
    assert b"".join(f["payload"] for f in fields) == payload
    assert [f["packet_id"] for f in fields] == list(range(len(packets)))
    assert all(f["max_packet_id"] == len(packets) - 1 for f in fields)
    assert [f["seq"] for f in fields] == list(range(100, 100 + len(packets)))


def test_marker_bit_only_on_last_packet():
    packets, _ = _packetize(bytes(5000))
    markers = [_parse(p)["marker"] for p in packets]
    assert markers[-1] is True
    assert not any(markers[:-1])


def test_packets_fit_into_one_mtu():
    packets, _ = _packetize(bytes(100_000))
    assert max(len(p) for p in packets) <= 1500 - 28   # abzüglich IP+UDP


def test_ids_wrap_into_a_single_byte():
    """frame_id wird als 8 Bit übertragen – 300 muss als 44 ankommen."""
    packets, _ = _packetize(b"y", frame_id=300, is_key=False, reference_frame_id=299)
    f = _parse(packets[0])
    assert f["frame_id"] == 300 & 0xFF
    assert f["reference_frame_id"] == 299 & 0xFF


def test_timestamp_and_seq_wrap():
    packets, next_seq = _packetize(b"z", rtp_timestamp=0x1_0000_0001, seq=0xFFFF)
    f = _parse(packets[0])
    assert f["timestamp"] == 1          # 32 Bit
    assert f["seq"] == 0xFFFF
    assert next_seq == 0x10000          # der Aufrufer maskiert beim nächsten Frame


def test_empty_payload_still_produces_one_packet():
    packets, _ = _packetize(b"")
    assert len(packets) == 1
    assert _parse(packets[0])["payload"] == b""
