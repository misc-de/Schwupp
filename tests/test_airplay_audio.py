"""Ton beim AirPlay-Spiegeln: ALAC, RTP, Redundanz, Sync, Nachforderungen.

Die Vektoren am Anfang sind byte-genau gegen doubletake (Go) geprüft – dem
Sender, mit dem der Hisense samt Ton spiegelt. Die Sitzungstests lassen den
Python-Empfänger (``_airplay_receiver``) jedes Ton-Paket entschlüsseln und
das ALAC wieder in PCM zurückverwandeln.
"""
from __future__ import annotations

import hashlib
import struct

import pytest
from _airplay_receiver import FakeReceiver, wait_for

from app.mirror.airplay_stream import audio, session
from app.mirror.airplay_stream.audio import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    AudioPacketizer,
    alac_verbatim,
    red_header_length,
    red_payload,
    retransmit_answers,
    sync_packet,
)
from app.mirror.airplay_stream.session import MirrorSession

KEY = bytes(i * 3 for i in range(32))
PCM = bytes(((i * 7919 + 13) % 256) for i in range(FRAME_BYTES))


def alac_decode(frame: bytes) -> bytes:
    """Umkehrung von alac_verbatim (prüft den Kopf und das Ende-Element)."""
    value = int.from_bytes(frame, "big")
    bits = len(frame) * 8
    head = value >> (bits - 55)
    assert head >> 52 == 0b001 and head >> 32 & 0b1111 == 0b1001   # CPE, Größe, verbatim
    count = head & 0xFFFFFFFF
    body_bits = count * 2 * 16
    body = (value >> (bits - 55 - body_bits)) & ((1 << body_bits) - 1)
    end = (value >> (bits - 55 - body_bits - 3)) & 0b111
    assert end == 0b111
    samples = struct.unpack(f">{count * 2}h", body.to_bytes(body_bits // 8, "big"))
    return struct.pack(f"<{count * 2}h", *samples)


# -- Vektoren (gegen doubletake geprüft) ------------------------------------------

def test_alac_frames_match_the_reference_sender():
    frame = alac_verbatim(PCM)
    assert len(frame) == 1416
    assert hashlib.sha256(frame).hexdigest() == \
        "a914ead260ea35da42da7042643dfe911b312a4ee7b690d33b2da09afea6ac04"
    assert frame[:12].hex() == "200012000002c1f81bb5d771" and frame[-4:].hex() == "a33d5fc0"
    assert alac_decode(frame) == PCM


def test_audio_packets_match_the_reference_sender():
    packets = AudioPacketizer(KEY)
    assert packets.packet(b"hallo ton", 352, 1).hex() == (
        "80600001000001600000000074468a0d97de5436a4e5c6cd740ec16f1c9bd9d34e"
        "6c8a1e720000000000000000")
    red = red_payload(b"drei", 704, [(b"eins", 0), (b"zwei", 352)],
                      AudioPacketizer.max_plain_payload())
    assert red.hex() == "e00b0004e00580046065696e737a77656964726569"
    assert packets.packet(red, 704, 3, audio.PT_RED, red_header_length(red)).hex() == (
        "80610003000002c000000000e00b0004e00580046055c7c9cc05e5f79f60ab4f4d11cfcb123b2774"
        "61f2589f637ac52e340100000000000000")
    assert AudioPacketizer.max_plain_payload() == 1436


def test_sync_packets_match_the_reference_sender():
    assert sync_packet("NTP", 0xE123456789ABCDEF, 0, 10000, 6615, True).hex() == \
        "90d4000400000d39e123456789abcdef00002710"
    assert sync_packet("PTP", 0x0000123480000000, 0x4454424C54414B45, 10000, 6615,
                       False).hex() == \
        "80d7000400000d390000043d1b726d0000000d394454424c54414b45"


# -- Bausteine --------------------------------------------------------------------

def test_redundancy_keeps_within_limits_and_rejects_broken_headers():
    big = b"x" * 0x400                                  # zu groß für ein Längenfeld
    assert red_payload(b"neu", 1000, [(big, 648)], 1436) == bytes([audio.PT_DATA]) + b"neu"
    far = [(b"alt", 0)]
    assert red_payload(b"neu", 0x4000, far, 1436) == bytes([audio.PT_DATA]) + b"neu"
    three = red_payload(b"d", 1056, [(b"a", 0), (b"b", 352), (b"c", 704)], 1436)
    assert red_header_length(three) == 9 and three.endswith(b"bcd")   # nur zwei Vorgänger
    for broken in (b"", b"\xe0\x00\x00", b"\x22"):
        with pytest.raises(ValueError):
            red_header_length(broken)


def test_retransmit_requests_are_answered_until_history_ends():
    history = {5: b"funf", 6: b"sechs"}
    request = struct.pack(">BBHHH", 0x80, 0xD5, 9, 5, 4)
    answers = retransmit_answers(request, history)
    assert answers[0] == b"\x80\xd6\x00\x09funf" and answers[1] == b"\x80\xd6\x00\x09sechs"
    assert answers[2] == struct.pack(">BBHHH", 0x80, 0xD6, 9, 7, 0)    # abgelaufen
    assert len(answers) == 3
    assert retransmit_answers(b"\x80\xd5", history) == []


def test_packet_history_is_bounded():
    packets = AudioPacketizer(KEY)
    for seq in range(audio.HISTORY + 10):
        packets.remember(seq, b"p")
    assert len(packets.history) == audio.HISTORY and 0 not in packets.history


def test_receivers_that_take_alac():
    assert audio.receiver_takes_alac({}, 0)
    assert audio.receiver_takes_alac({}, 1 << 19)
    assert not audio.receiver_takes_alac({}, 1 << 21)                  # nur AAC-ELD
    assert audio.receiver_takes_alac({"supportedFormats": {"screenStream": 0x40000}}, 0)
    assert not audio.receiver_takes_alac({"supportedFormats": {"screenStream": 0x1000000}}, 0)
    assert audio.receiver_takes_alac({"supportedFormats": {"screenStream": "x"}}, 0)


def test_audio_ports_are_read_from_both_answer_shapes():
    assert session.audio_ports({"streams": [{"type": 96, "dataPort": 5, "controlPort": 6}]}) \
        == (5, 6)
    modern = {"streams": [{"type": 110}, {"type": 96, "streamConnections": {
        "streamConnectionTypeRTP": {"streamConnectionKeyPort": 7},
        "streamConnectionTypeRTCP": {"streamConnectionKeyPort": 8}}}]}
    assert session.audio_ports(modern) == (7, 8)
    assert session.audio_ports({}) == (0, 0)


# -- Ganze Sitzungen --------------------------------------------------------------

IDR = (b"\x00\x00\x00\x01\x67\x42\xc0\x1e\x95\xa0\x00\x00\x01\x68\xce\x3c\x80"
       b"\x00\x00\x01\x65\x88\x84\x21")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(session, "_HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(session, "_FEEDBACK_INTERVAL", 0.05)
    monkeypatch.setattr(audio, "SYNC_INTERVAL", 0.05)


def _mirror(rx: FakeReceiver, **kwargs) -> MirrorSession:
    s = MirrorSession("127.0.0.1", port=rx.port, pin_callback=lambda: rx.pin,
                      port_range=None, log=lambda _m: None, **kwargs)
    s.connect()
    s.setup(1920, 1080, audio=True)
    return s


def test_sound_follows_the_picture_and_arrives_intact():
    rx = FakeReceiver()
    s = _mirror(rx, latency_ms=150)
    try:
        assert s.audio is not None and not s.audio.rfc2198
        s.send_audio(PCM)                                  # vor dem ersten Bild: verworfen
        assert s.send_access_unit(IDR)
        second = bytes(reversed(PCM))
        s.send_audio(PCM + second[:100], local_ns=10_000_000_000)
        s.send_audio(second[100:])                        # Rest ergänzt den zweiten Rahmen
        assert wait_for(lambda: len(rx.audio_frames) == 2)
        assert wait_for(lambda: len(rx.audio_syncs) >= 2)
    finally:
        s.close()
        rx.close()
    assert rx.frame_errors == []
    (pt1, seq1, rtp1, f1), (_pt2, seq2, rtp2, f2) = rx.audio_frames
    assert (pt1, seq1, rtp1, seq2, rtp2) == (audio.PT_DATA, 1, FRAME_SAMPLES, 2, 2 * FRAME_SAMPLES)
    assert alac_decode(f1) == PCM and alac_decode(f2) == second
    first = rx.audio_syncs[0]
    latency = 150 * 44100 // 1000
    assert first[:2] == b"\x90\xd4"
    playhead, network, rtp_now = struct.unpack(">IQI", first[4:20])
    assert rtp_now == FRAME_SAMPLES and playhead == (FRAME_SAMPLES - latency) & 0xFFFFFFFF
    # Netzzeit = Zeit des ersten Werts mit NTP-Epoche
    assert network == session.compact(10_000_000_000) + (session.NTP_EPOCH_OFFSET << 32)
    assert rx.audio_syncs[1][:2] == b"\x80\xd4"                       # laufende Sync-Pakete


def test_lost_sound_is_sent_again_on_request():
    rx = FakeReceiver()
    s = _mirror(rx)
    try:
        assert s.send_access_unit(IDR)
        s.send_audio(PCM * 2)
        assert wait_for(lambda: len(rx.audio_frames) == 2)
        rx.request_audio(2, 2)
        assert wait_for(lambda: len(rx.audio_answers) == 2)
    finally:
        s.close()
        rx.close()
    again, expired = rx.audio_answers
    assert again[:4] == b"\x80\xd6\x00\x09"
    assert rx.open_audio_packet(again[4:])[1] == 2                   # Originalpaket Nr. 2
    assert expired == struct.pack(">BBHHH", 0x80, 0xD6, 9, 3, 0)


def test_rfc2198_receivers_get_red_packets_with_whole_frames():
    """Unkomprimierte ALAC-Rahmen (1416 Byte) passen nicht ins 10-Bit-Längenfeld
    von RFC 2198 und nicht zweimal in ein Datagramm: Das 0x61-Paket trägt dann
    nur den aktuellen Rahmen, verlorene kommen über die Nachforderung."""
    rx = FakeReceiver(features=FakeReceiver.LEGACY_FEATURES | (1 << 61))
    s = _mirror(rx)
    try:
        assert s.audio.rfc2198
        assert s.send_access_unit(IDR)
        s.send_audio(PCM * 3)
        assert wait_for(lambda: len(rx.audio_frames) == 3)
    finally:
        s.close()
        rx.close()
    pt, seq, rtp, payload = rx.audio_frames[2]
    assert pt == audio.PT_RED and seq == 3
    assert red_header_length(payload) == 1
    assert payload[1:] == alac_verbatim(PCM)
    assert len(rx.audio_frames[2][3]) + 12 + 16 + 8 <= audio.MAX_DATAGRAM


def test_ptp_sessions_announce_sound_on_the_receivers_timeline():
    rx = FakeReceiver(features=FakeReceiver.PTP_FEATURES, ptp=True, session_first=True)
    s = _mirror(rx)
    try:
        assert s.send_access_unit(IDR)
        s.send_audio(PCM)
        assert wait_for(lambda: rx.audio_syncs)
    finally:
        s.close()
        rx.close()
    sync = rx.audio_syncs[0]
    assert sync[1] == audio.PT_SYNC_PTP
    assert struct.unpack(">Q", sync[20:28])[0] == 0x4454424C54414B45


@pytest.mark.parametrize("knobs, features", [
    ({"no_audio_ports": True}, FakeReceiver.LEGACY_FEATURES),
    ({}, (FakeReceiver.LEGACY_FEATURES & ~(1 << 19)) | (1 << 21)),     # nur AAC-ELD
])
def test_without_ports_or_alac_mirroring_stays_silent(knobs, features):
    rx = FakeReceiver(features=features, **knobs)
    s = _mirror(rx)
    try:
        assert s.audio is None
        assert s.send_access_unit(IDR)
        s.send_audio(PCM)                                  # wird ignoriert
        assert wait_for(lambda: rx.frames)
    finally:
        s.close()
        rx.close()
    assert rx.audio_frames == []


def test_modern_receivers_get_sound_through_stream_connections():
    rx = FakeReceiver(features=FakeReceiver.LEGACY_FEATURES | (1 << 59))
    s = _mirror(rx)
    try:
        assert s.audio is not None
        assert s.send_access_unit(IDR)
        s.send_audio(PCM)
        assert wait_for(lambda: rx.audio_frames)
    finally:
        s.close()
        rx.close()
    descriptor = next(r for r in rx.setups if r.get("streams")
                      and r["streams"][0]["type"] == 96)["streams"][0]
    assert descriptor["streamConnections"]["streamConnectionTypeRTP"][
        "streamConnectionKeyUseStreamEncryptionKey"] is True


def test_a_closed_sender_sends_nothing_more():
    rx = FakeReceiver()
    s = _mirror(rx)
    sender = s.audio
    try:
        assert s.send_access_unit(IDR)
    finally:
        s.close()
        rx.close()
    sender.push(PCM)
    assert sender.frames_sent == 0
    sender._send(alac_verbatim(PCM), 0)                # Socket zu: still verworfen
    assert sender.frames_sent == 0
