"""RTCP-Parser der nativen Engine (Compound-Walk, Cast-NACK, XR-Referenzzeit)."""
from __future__ import annotations

import struct

from app.mirror.cast_streaming.rtcp import (
    NTP_EPOCH,
    find_xr_reftime,
    ntp_to_unix,
    parse_nacks,
    sender_report,
    unix_to_ntp,
    walk,
)


def _rtcp(packet_type: int, body: bytes) -> bytes:
    """Baut ein RTCP-Sub-Paket mit korrektem Längenfeld (32-Bit-Wörter minus 1)."""
    assert len(body) % 4 == 0
    words = (len(body) + 4) // 4 - 1
    return struct.pack("!BBH", 0x80, packet_type, words) + body


def test_walk_iterates_all_subpackets_of_a_compound():
    compound = _rtcp(201, bytes(8)) + _rtcp(207, bytes(12)) + _rtcp(206, bytes(16))
    assert [pt for pt, _off, _size in walk(compound)] == [201, 207, 206]


def test_walk_stops_on_truncated_input():
    """Ein abgeschnittenes Paket darf keine Endlosschleife oder Exception geben."""
    truncated = _rtcp(201, bytes(8))[:-3]
    assert list(walk(truncated)) == []


def test_ntp_conversion():
    assert ntp_to_unix((NTP_EPOCH + 100) << 32) == 100.0
    half = ntp_to_unix(((NTP_EPOCH + 5) << 32) | (1 << 31))
    assert abs(half - 5.5) < 1e-6


def _cast_feedback(media_ssrc: int, fields: list[tuple[int, int, int]]) -> bytes:
    # sender_ssrc, media_ssrc, dann FCI: 'CAST' + 4 Bytes Kopf + je 4 Byte Feld
    fci = b"CAST" + bytes(4) + b"".join(
        struct.pack("!BHB", frame, packet_id, mask) for frame, packet_id, mask in fields)
    body = struct.pack("!II", 1, media_ssrc) + fci
    body += bytes((4 - len(body) % 4) % 4)
    return _rtcp(206, body)


def test_parse_nacks_extracts_media_ssrc_and_fields():
    packet = _cast_feedback(100003, [(7, 2, 0b101), (7, 9, 0)])
    ssrc, fields = parse_nacks(packet)
    assert ssrc == 100003          # ordnet die Anforderung dem Audio-Stream zu
    assert fields == [(7, 2, 0b101), (7, 9, 0)]


def test_parse_nacks_finds_the_packet_inside_a_compound():
    compound = _rtcp(201, bytes(8)) + _cast_feedback(100001, [(3, 0, 0)])
    ssrc, fields = parse_nacks(compound)
    assert ssrc == 100001
    assert fields == [(3, 0, 0)]


def test_parse_nacks_ignores_foreign_feedback():
    """PT=206 ohne das 'CAST'-Magic ist generisches RTPFB – nicht für uns."""
    body = struct.pack("!II", 1, 2) + b"XXXX" + bytes(8)
    ssrc, fields = parse_nacks(_rtcp(206, body))
    assert ssrc is None and fields is None


def test_parse_nacks_on_unrelated_compound():
    assert parse_nacks(_rtcp(201, bytes(8))) == (None, None)


def test_find_xr_reftime_reads_block_type_4():
    ntp = 0xE9A3_1234_5678_9ABC
    block = struct.pack("!BBH", 4, 0, 2) + struct.pack("!Q", ntp)
    packet = _rtcp(207, struct.pack("!I", 1) + block)
    assert find_xr_reftime(packet) == ntp


def test_find_xr_reftime_skips_other_block_types():
    other = struct.pack("!BBH", 5, 0, 1) + bytes(4)      # BT=5, nicht unsere Referenzzeit
    packet = _rtcp(207, struct.pack("!I", 1) + other)
    assert find_xr_reftime(packet) is None


def test_find_xr_reftime_returns_none_without_xr():
    assert find_xr_reftime(_rtcp(200, bytes(20))) is None


def test_sender_report_layout():
    """PT=200, 6 Wörter Länge, NTP-Zeit und RTP-Zeit an den erwarteten Offsets."""
    packet = sender_report(ssrc=100001, wall_clock=1_000_000.5, rtp_timestamp=90000,
                           packets=12, octets=3400)
    assert len(packet) == 28
    version, pt, words, ssrc = struct.unpack("!BBHI", packet[:8])
    assert (version, pt, words, ssrc) == (0x80, 200, 6, 100001)
    sec, frac, rtp_ts, pkts, octs = struct.unpack("!IIIII", packet[8:28])
    assert (sec, frac) == unix_to_ntp(1_000_000.5)
    assert ntp_to_unix((sec << 32) | frac) == 1_000_000.5
    assert (rtp_ts, pkts, octs) == (90000, 12, 3400)


def test_sender_report_masks_counters_to_32_bit():
    packet = sender_report(1, 0.0, 0x1_0000_0005, 0x1_0000_0007, 0x1_0000_0009)
    rtp_ts, pkts, octs = struct.unpack("!III", packet[16:28])
    assert (rtp_ts, pkts, octs) == (5, 7, 9)
