"""Eigene AirPlay-Spiegelung: Protokollbausteine und ganze Sitzungen.

Die Testvektoren am Anfang wurden byte-genau gegen doubletake (Go) geprüft,
das am Hisense-Fernseher spiegelt: Schlüsselableitung des Videostroms,
verschlüsseltes Bild mit Kopf als AAD, avcC, Steuer- und Ereignisschlüssel,
Zeitstempel. Weicht hier ein Byte ab, bleibt der Fernseher schwarz.

Die Sitzungstests laufen gegen einen kleinen Empfänger in Python
(``_airplay_receiver``), der die Gegenseite spricht und jedes Bild
entschlüsselt.
"""
from __future__ import annotations

import struct

import pytest
from _airplay_receiver import CLOCK_ID, FakeReceiver, wait_for
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from app.mirror.airplay_stream import h264, hap, session
from app.mirror.airplay_stream.rtsp import RtspError, StatusError, content_length, parse_head
from app.mirror.airplay_stream.session import (
    MirrorError,
    MirrorSession,
    PairingRequired,
    PtpClock,
    PtpUnavailable,
)

SHARED = bytes(range(32))
SCID = 1795816749965735257
SPS = bytes.fromhex("6742c01e95a0")
PPS = bytes.fromhex("68ce3c80")


# -- Testvektoren (gegen doubletake geprüft) ------------------------------------

def test_video_stream_key_and_frame_match_the_reference_sender():
    key = hap.video_key(SHARED, SCID)
    assert key.hex() == "0e51b4a5775330a7f9195fa71350690fe549216b775937e09b53ed2bc3b06db0"
    plain = bytes([0, 0, 0, 3, 0x65, 0x88, 0x84])
    header = bytearray(128)
    struct.pack_into("<I", header, 0, len(plain) + 16)
    header[5] = 0x10
    struct.pack_into("<Q", header, 8, session.compact(123456789012345))
    struct.pack_into("<Q", header, 40, 0x4454424C54414B45)
    assert bytes(header[:48]).hex() == (
        "170000000010000089b6fcc940e2010000000000000000000000000000000000"
        "0000000000000000454b41544c425444")
    sealed = ChaCha20Poly1305(key).encrypt(hap.nonce_for(7), plain, bytes(header))
    assert sealed.hex() == "db91d3127a52b4cd76ae6506fcf5bb31c7c7e3d86f251b"


def test_avcc_config_matches_the_reference_sender():
    assert h264.avcc_config(SPS, PPS).hex() == \
        "0142c01effe100066742c01e95a001000468ce3c8002000000"


def test_channel_keys_and_timestamps_match_the_reference_sender():
    _cipher, write_key, _read = hap.control_cipher(SHARED)
    assert write_key.hex() == "c3ca130c7033dbe5e7ff7f91d117ead869bac476994c7a48ca170c111136ed96"
    assert hap.hkdf_sha512(SHARED, b"Events-Salt", b"Events-Read-Encryption-Key").hex() == \
        "77c905af437e45acea18c03d6d5b7791fa9b9da65f3d4d63de8ce7ab3ea3fa5a"
    assert session.compact(1500 * 1_000_000) == 0x180000000
    assert session.compact(-5) == 0


# -- HAP-Bausteine --------------------------------------------------------------

def test_tlv_splits_long_values_and_joins_them_again():
    value = bytes(range(256)) * 2
    data = hap.tlv_encode([(hap.TLV_PUBLIC_KEY, value), (hap.TLV_STATE, b"\x03"),
                           (hap.TLV_SALT, b"")])
    assert data.count(bytes((hap.TLV_PUBLIC_KEY, 255))) == 2
    decoded = hap.tlv_decode(data + b"\x05\x09abc")     # abgeschnittenes Ende fällt weg
    assert decoded[hap.TLV_PUBLIC_KEY] == value
    assert decoded[hap.TLV_STATE] == b"\x03"
    assert decoded[hap.TLV_SALT] == b""
    assert hap.TLV_ENCRYPTED_DATA not in decoded


def test_tlv_error_is_reported_with_its_meaning():
    with pytest.raises(hap.PairingError, match="Code falsch") as info:
        hap.check_tlv_error("M4", {hap.TLV_ERROR: b"\x02"})
    assert info.value.code == 2
    hap.check_tlv_error("M4", {hap.TLV_STATE: b"\x04"})


def test_credentials_use_the_pyatv_format():
    creds = hap.Credentials(b"\x01" * 32, b"\x02" * 32, b"AA:BB", b"CLIENT")
    text = str(creds)
    assert text.count(":") == 3
    assert hap.Credentials.parse(text) == creds
    assert hap.Credentials.parse(None) is None
    assert hap.Credentials.parse("00:11") is None                 # Legacy-Format
    assert hap.Credentials.parse("zz:11:22:33") is None
    assert hap.Credentials.parse("01:02:03:04") is None           # falsche Längen


def test_hap_frames_split_at_1024_bytes_and_survive_partial_reads():
    key_a, key_b = b"\x11" * 32, b"\x22" * 32
    sender, receiver = hap.HapCipher(key_a, key_b), hap.HapCipher(key_b, key_a)
    message = bytes(range(256)) * 9            # 2304 Byte -> 3 Rahmen
    wire = sender.encrypt(message)
    assert len(wire) == len(message) + 3 * 18
    out = b"".join(receiver.decrypt(wire[i:i + 100]) for i in range(0, len(wire), 100))
    assert out == message
    with pytest.raises(hap.PairingError):
        receiver.decrypt(b"\x00\x00")           # Länge 0 ist ungültig
    tampered = hap.HapCipher(key_b, key_a)
    bad = bytearray(sender.encrypt(b"hallo"))
    bad[-1] ^= 1
    with pytest.raises(hap.PairingError):
        tampered.decrypt(bytes(bad))


# -- H.264 ----------------------------------------------------------------------

def test_annexb_is_split_into_nal_units_with_both_start_codes():
    data = b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x01" + PPS + b"\x00\x00\x00\x01\x65\x88"
    assert h264.split_annexb(data) == [SPS, PPS, b"\x65\x88"]
    assert h264.split_annexb(b"") == []


def test_packetizer_waits_for_a_keyframe_and_resends_changed_parameters():
    p = h264.Packetizer()
    p_frame = p.packetize(b"\x00\x00\x01\x41\x9a\x01")
    assert p_frame.has_picture and not p_frame.keyframe
    assert not p.ready(p_frame)                       # vor dem ersten Schlüsselbild
    idr = p.packetize(b"\x00\x00\x00\x01\x09\xf0" + b"\x00\x00\x01" + SPS + b"\x00\x00\x01" + PPS
                      + b"\x00\x00\x01\x06\x05\x01" + b"\x00\x00\x01\x65\x88\x84")
    assert idr.keyframe and p.ready(idr)
    # AUD und Parametersätze fehlen, SEI und Bild sind als AVCC drin
    assert idr.payload == b"\x00\x00\x00\x03\x06\x05\x01\x00\x00\x00\x03\x65\x88\x84"
    assert p.needs_config()
    assert p.config() == h264.avcc_config(SPS, PPS)
    p.configured()
    assert not p.needs_config() and p.ready(p_frame)
    p.packetize(b"\x00\x00\x01" + SPS[:-1] + b"\xff")    # neues SPS
    assert p.needs_config()
    with pytest.raises(ValueError):
        h264.avcc_config(b"\x67", PPS)


# -- Zeit -----------------------------------------------------------------------

def test_ntp_reply_echoes_the_request_and_ignores_anything_else():
    request = bytearray(32)
    request[0], request[1] = 0x80, 0xD2
    request[24:32] = b"ABCDEFGH"
    reply = session.ntp_reply(bytes(request))
    assert reply[:2] == b"\x80\xd3" and reply[8:16] == b"ABCDEFGH"
    assert struct.unpack(">I", reply[16:20])[0] > session.NTP_EPOCH_OFFSET
    assert session.ntp_reply(reply) is None          # Antwort nie beantworten
    assert session.ntp_reply(b"\x80\xd2") is None


def test_ptp_clock_follows_the_receiver_and_never_runs_backwards():
    clock = PtpClock()
    assert clock.at(10) is None
    assert not clock.configure({}, {}, 0)
    assert not clock.configure({"timingPeerInfo": {"ClockID": 9}},
                               {"x-apple-requestreceivedtimestamp": "x"}, 0)
    headers = {"x-apple-requestreceivedtimestamp": "1000", "x-apple-processingtime": "5"}
    assert clock.configure({"timingPeerInfo": {"ClockID": 9}}, headers, 1_000_000_000)
    ts, timeline = clock.at(1_500_000_000)
    assert timeline == 9 and ts == session.compact(1_505_000_000)
    clock.reanchor({"x-apple-requestreceivedtimestamp": "1000"}, 2_000_000_000)  # älter
    assert clock.at(2_000_000_000)[0] == session.compact(2_005_000_000)
    clock.reanchor({}, 3_000_000_000)                # ohne Zeitstempel: nichts
    clock.reanchor({"x-apple-requestreceivedtimestamp": "-4"}, 3_000_000_000)
    clock.update_peer({"ClockID": 12})
    assert clock.at(2_000_000_000)[1] == 12
    clock.update_peer({})
    fresh = PtpClock()
    fresh.update_peer({"ClockID": 3})
    assert fresh.ready.is_set() and fresh.at(session.boot_ns())[1] == 3


# -- /info ----------------------------------------------------------------------

def test_receiver_info_helpers():
    assert session._features({"features": "0x5A7FFFF7,0x1E"}) == 0x1E5A7FFFF7
    assert session._features({"features": "junk"}) == 0
    assert session._features({"features": None}) == 0
    assert session._source_version({}, "AirTunes/377.40.00 extra") == (377, 40, 0)
    assert session._source_version({"sourceVersion": "980.71.1"}, "") == (980, 71, 1)
    assert session._source_version({"sourceVersion": "x.y"}, "") is None
    assert session._source_version({"sourceVersion": "1"}, "") is None
    assert session.prefers_legacy_pairing({"features": FakeReceiver.LEGACY_FEATURES})
    assert not session.prefers_legacy_pairing({"features": 1 << 38})
    assert session.prefers_legacy_pairing({"features": 0})
    assert session.display_size({"displays": [{"width": 1280, "height": 720}]}) == (1280, 720)
    assert session.display_size({"displays": [{"widthPixels": "x"}]}) == (0, 0)
    assert session.display_size({}) == (0, 0)
    assert session.mac_from_uuid("12345678-9abc-def0") == "02:34:56:78:9A:BC"


def test_udp_ports_are_consecutive_and_prefer_the_firewall_range():
    socks = session.bind_consecutive_udp(3, (61000, 61010))
    try:
        ports = [s.getsockname()[1] for s in socks]
        assert ports == [ports[0], ports[0] + 1, ports[0] + 2]
        assert 61000 <= ports[0] <= 61008
    finally:
        for s in socks:
            s.close()
    socks = session.bind_consecutive_udp(3, None)
    ports = [s.getsockname()[1] for s in socks]
    assert ports[2] == ports[0] + 2
    for s in socks:
        s.close()


# -- RTSP -----------------------------------------------------------------------

def test_rtsp_heads_are_parsed_strictly():
    status, reason, headers = parse_head(b"RTSP/1.0 455 Method Not Valid\r\nCSeq: 3")
    assert (status, reason, headers) == (455, "Method Not Valid", {"cseq": "3"})
    for bad in (b"FOO 200 OK", b"RTSP/1.0 abc", b"RTSP/1.0 200 OK\r\nkaputt"):
        with pytest.raises(RtspError):
            parse_head(bad)
    assert content_length({}) == 0
    for bad in ("x", "-1", str(10 ** 9)):
        with pytest.raises(RtspError):
            content_length({"content-length": bad})


# -- Ganze Sitzungen --------------------------------------------------------------

AUD = b"\x00\x00\x00\x01\x09\xf0"
IDR = AUD + b"\x00\x00\x01" + SPS + b"\x00\x00\x01" + PPS + b"\x00\x00\x01\x65\x88\x84\x21"
P_FRAME = AUD + b"\x00\x00\x01\x41\x9a\x02\x03"


@pytest.fixture(autouse=True)
def fast_intervals(monkeypatch):
    monkeypatch.setattr(session, "_OPEN_RETRY_DELAY", 0.01)
    monkeypatch.setattr(session, "_HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(session, "_FEEDBACK_INTERVAL", 0.05)


@pytest.fixture
def receiver():
    rx = FakeReceiver()
    yield rx
    rx.close()


def _session(rx: FakeReceiver, **kwargs) -> MirrorSession:
    stored = kwargs.pop("stored", [])
    defaults = {"port": rx.port, "pin_callback": lambda: rx.pin, "port_range": None,
                "store_credentials": lambda creds, legacy: stored.append((creds, legacy)),
                "log": lambda _msg: None}
    defaults.update(kwargs)
    return MirrorSession("127.0.0.1", **defaults)


def test_first_mirroring_pairs_with_the_pin_and_streams_decryptable_frames(receiver):
    stored = []
    s = _session(receiver, stored=stored)
    try:
        s.connect()
        assert receiver.pin_starts == 1
        assert stored and stored[0][1] is True            # HKP 3: mit pyatv geteilt
        s.setup(1920, 1080)
        assert s.timing == "NTP"
        assert not s.send_access_unit(P_FRAME)             # vor dem Schlüsselbild nichts
        assert s.send_access_unit(IDR)
        assert s.send_access_unit(P_FRAME)
        assert wait_for(lambda: len(receiver.frames) == 2)
        assert wait_for(lambda: receiver.heartbeats >= 1)
        assert receiver.ntp_answered.wait(2)
        assert receiver.event_acks.wait(2)
    finally:
        s.close()
    assert receiver.frame_errors == []
    assert receiver.codec_configs == [(h264.avcc_config(SPS, PPS), 1920.0, 1080.0)]
    (key_plain, key_flag, ts1, line1), (p_plain, p_flag, ts2, _l) = receiver.frames
    assert key_plain == b"\x00\x00\x00\x04\x65\x88\x84\x21" and key_flag
    assert p_plain == b"\x00\x00\x00\x04\x41\x9a\x02\x03" and not p_flag
    assert ts2 > ts1 and line1 == 0
    # Ältere Reihenfolge: Ton-Stream mit Sitzungsdaten, controlPort und eigenem Schlüssel
    audio = receiver.setups[1]
    assert audio["streams"][0]["type"] == 96 and "controlPort" in audio["streams"][0]
    assert audio["isScreenMirroringSession"] is True and len(audio["streams"][0]["shk"]) == 32
    video = receiver.setups[2]["streams"][0]
    assert video["type"] == 110 and video["latencyMs"] == 150
    assert ("RECORD", s.session_uri) in receiver.requests
    assert receiver.teardowns == 1
    assert ("POST", "/feedback") in receiver.requests


def test_stored_pairing_needs_no_pin(receiver):
    stored = []
    first = _session(receiver, stored=stored)
    first.connect()
    first.close()

    def no_pin():
        raise AssertionError("PIN abgefragt")

    second = _session(receiver, credentials=stored[0][0], pin_callback=no_pin)
    try:
        second.connect()
        assert second.rtsp.cipher is not None
    finally:
        second.close()
    assert receiver.pin_starts == 1


def test_a_stale_pairing_falls_back_to_a_new_pin(receiver):
    stale = hap.Credentials(receiver.ltpk, b"\x07" * 32, receiver.identifier, b"UNKNOWN")
    stored = []
    s = _session(receiver, credentials=stale, stored=stored)
    try:
        s.connect()
    finally:
        s.close()
    assert receiver.pin_starts == 1 and stored


def test_a_different_device_at_the_address_is_not_trusted(receiver):
    foreign = hap.Credentials(b"\x01" * 32, b"\x07" * 32, b"OTHER", b"ME")
    with pytest.raises(MirrorError, match="Kopplung abgebrochen"):
        _session(receiver, credentials=foreign, pin_callback=lambda: "").connect()


def test_a_wrong_pin_explains_what_happened(receiver):
    with pytest.raises(MirrorError, match="Code stimmte nicht"):
        _session(receiver, pin_callback=lambda: "9999").connect()


def test_without_a_pin_dialog_pairing_is_refused(receiver):
    with pytest.raises(MirrorError, match="kein Eingabefeld"):
        _session(receiver, pin_callback=None).connect()


def test_fairplay_receivers_are_refused_before_pairing():
    rx = FakeReceiver(features=FakeReceiver.LEGACY_FEATURES | (1 << 14))
    try:
        with pytest.raises(MirrorError, match="FairPlay"):
            _session(rx).connect()
        assert rx.pin_starts == 0
    finally:
        rx.close()


def test_password_receivers_get_a_clear_message():
    rx = FakeReceiver(status_flags=1 << 7)
    try:
        with pytest.raises(MirrorError, match="Passwort"):
            _session(rx).connect()
    finally:
        rx.close()


def test_modern_receivers_pair_for_screen_capture():
    rx = FakeReceiver(features=(1 << 38) | (1 << 7) | (1 << 28), session_first=True)
    stored = []
    s = _session(rx, stored=stored)
    try:
        s.connect()
        s.setup(1280, 720)
    finally:
        s.close()
    assert stored[0][1] is False                     # eigener Platz, nicht pyatvs
    assert rx.controllers                            # Kopplung mit Freigabe angelegt
    assert "streams" not in rx.setups[0]             # Sitzung zuerst
    assert rx.setups[0]["combinedGetInfoWithControlSetup"] is True


def test_ptp_receivers_get_frames_on_their_timeline():
    rx = FakeReceiver(features=FakeReceiver.PTP_FEATURES, ptp=True, session_first=True)
    s = _session(rx, latency_ms=100)
    try:
        s.connect()
        s.setup(1920, 1080)
        assert s.timing == "PTP"
        assert s.send_access_unit(IDR)
        assert wait_for(lambda: rx.frames)
    finally:
        s.close()
    _plain, _key, ts, timeline = rx.frames[0]
    assert timeline == CLOCK_ID
    # Anker 5 000 002 ms beim Empfänger + 100 ms Puffer, wenige ms Laufzeit
    assert session.compact(5_000_102_000_000) <= ts < session.compact(5_000_202_000_000)
    assert rx.setups[0]["timingPeerInfo"]["Addresses"] == ["127.0.0.1"]


def test_ptp_without_a_clock_asks_for_ntp():
    rx = FakeReceiver(features=FakeReceiver.PTP_FEATURES, ptp=True, ptp_clock=False,
                      session_first=True)
    s = _session(rx)
    try:
        s.connect()
        with pytest.raises(PtpUnavailable):
            s.setup(1920, 1080)
    finally:
        s.close()
    forced = _session(rx, force_ntp=True, credentials=s.credentials)
    try:
        forced.connect()
        assert not forced._use_ptp()
    finally:
        forced.close()


def test_a_rejected_audio_shape_is_retried_once():
    rx = FakeReceiver(features=FakeReceiver.LEGACY_FEATURES | (1 << 59),
                      reject_stream_connections=True)
    s = _session(rx)
    try:
        s.connect()
        s.setup(1920, 1080)
    finally:
        s.close()
    audio = [r for r in rx.setups if r.get("streams") and r["streams"][0]["type"] == 96]
    assert "streamConnections" in audio[0]["streams"][0]
    assert "controlPort" in audio[1]["streams"][0]


def test_the_receiver_ending_the_stream_is_reported(receiver):
    failures = []
    receiver.close_data_after = 1
    s = _session(receiver, on_failure=failures.append)
    try:
        s.connect()
        s.setup(1920, 1080)
        s.send_access_unit(IDR)
        assert wait_for(lambda: failures)
    finally:
        s.close()
    assert "beendet" in failures[0] or "unterbrochen" in failures[0]
    assert not s.send_access_unit(P_FRAME)


def test_setup_needs_a_verified_connection(receiver):
    with pytest.raises(MirrorError, match="Nicht angemeldet"):
        _session(receiver).setup(1920, 1080)


def test_status_errors_are_translated():
    assert "Passwort" in str(MirrorSession._explain(StatusError("SETUP", "/", 401, "")))
    assert isinstance(MirrorSession._explain(StatusError("SETUP", "/", 403, "")),
                      PairingRequired)
    assert "abgelehnt" in str(MirrorSession._explain(StatusError("SETUP", "/", 500, "x")))


def test_unreachable_receivers_are_named():
    with pytest.raises(MirrorError, match="keine Verbindung an"):
        MirrorSession("127.0.0.1", port=1, log=lambda _m: None).connect()


def test_a_reset_first_connection_gets_a_second_try():
    """Der Hisense setzte GET /info zurück, solange noch eine andere Sitzung lief."""
    rx = FakeReceiver(reset_first=1)
    logs = []
    s = _session(rx, log=logs.append)
    try:
        s.connect()
        assert s.rtsp.cipher is not None
    finally:
        s.close()
        rx.close()
    assert any("Versuch 1" in line for line in logs)
    assert any("features" in line for line in logs)


def test_two_resets_explain_the_likely_cause():
    rx = FakeReceiver(reset_first=2)
    try:
        with pytest.raises(MirrorError, match="andere AirPlay-Sitzung"):
            _session(rx).connect()
    finally:
        rx.close()


# -- Fehlerfälle und seltenere Wege ---------------------------------------------

def _paired(rx: FakeReceiver) -> hap.Credentials:
    stored = []
    s = _session(rx, stored=stored)
    s.connect()
    s.close()
    return stored[0][0]


def test_receivers_that_name_their_timing_port_get_our_probes():
    rx = FakeReceiver(timing_port=True)
    s = _session(rx)
    try:
        s.connect()
        s.setup(1920, 1080)
        assert rx.probes.wait(2)
    finally:
        s.close()
        rx.close()


def test_a_ptp_clock_announced_later_on_the_event_channel_is_used():
    rx = FakeReceiver(features=FakeReceiver.PTP_FEATURES, ptp=True, session_first=True,
                      async_ptp=True)
    s = _session(rx)
    try:
        s.connect()
        s.setup(1920, 1080)
        assert s.send_access_unit(IDR)
        assert wait_for(lambda: rx.frames)
    finally:
        s.close()
        rx.close()
    assert rx.frames[0][3] == CLOCK_ID


def test_event_commands_that_are_no_plist_are_acknowledged_anyway():
    rx = FakeReceiver(features=FakeReceiver.PTP_FEATURES, ptp=True, session_first=True,
                      event_garbage=True)
    s = _session(rx)
    try:
        s.connect()
        s.setup(1920, 1080)
        assert s.clock.timeline == CLOCK_ID
        assert rx.event_acks.wait(2)
    finally:
        s.close()
        rx.close()


def test_screen_pairing_falls_back_to_the_classic_type():
    rx = FakeReceiver(features=(1 << 38), session_first=True, refuse_hkp={"5"})
    stored = []
    s = _session(rx, stored=stored)
    try:
        s.connect()
    finally:
        s.close()
        rx.close()
    assert rx.pin_starts == 2 and stored[0][1] is True


def test_a_receiver_asking_to_wait_is_asked_again():
    rx = FakeReceiver(backoff_once=0)
    try:
        assert _paired(rx)
    finally:
        rx.close()


def test_a_receiver_asking_to_wait_too_long_is_reported():
    rx = FakeReceiver(backoff_once=60)
    try:
        with pytest.raises(MirrorError, match="später erneut"):
            _session(rx).connect()
    finally:
        rx.close()


def test_a_refused_record_is_explained():
    rx = FakeReceiver(record_status=500)
    s = _session(rx)
    try:
        s.connect()
        with pytest.raises(MirrorError, match="abgelehnt"):
            s.setup(1920, 1080)
    finally:
        s.close()
        rx.close()


def test_ptp_receivers_refusing_the_audio_shape_get_an_ntp_retry():
    rx = FakeReceiver(features=FakeReceiver.PTP_FEATURES, ptp=True, fail_setup={"audio": 400})
    s = _session(rx)
    try:
        s.connect()
        with pytest.raises(PtpUnavailable):
            s.setup(1920, 1080)
    finally:
        s.close()
        rx.close()


@pytest.mark.parametrize("status", [404, 500])
def test_a_receiver_that_shows_no_code_is_reported(status):
    rx = FakeReceiver(pin_start_status=status)
    try:
        with pytest.raises(MirrorError, match="keinen Code"):
            _session(rx).connect()
    finally:
        rx.close()


def test_refused_verification_needs_a_new_pairing_or_explains_itself():
    rx = FakeReceiver()
    try:
        creds = _paired(rx)
        rx.knobs["verify_status"] = 470
        with pytest.raises(MirrorError, match="nach der Kopplung"):
            _session(rx, credentials=creds).connect()   # neu gekoppelt, Prüfung scheitert
        rx.knobs["verify_status"] = 500
        with pytest.raises(MirrorError, match="Anmeldung am Fernseher"):
            _session(rx, credentials=creds).connect()
    finally:
        rx.close()


def test_a_refused_pairing_type_without_alternative_is_reported():
    rx = FakeReceiver(refuse_hkp={"3"})                 # klassischer Empfänger: nur Typ 3
    try:
        with pytest.raises(MirrorError, match=r"Kopplung fehlgeschlagen \(.*501"):
            _session(rx).connect()
        assert rx.pin_starts == 1
    finally:
        rx.close()


def test_other_pairing_errors_are_passed_on():
    rx = FakeReceiver(refuse_hkp=set())
    rx._pair_setup = lambda body, state: (hap.tlv_encode(          # noqa: ARG005
        [(hap.TLV_STATE, b"\x02"), (hap.TLV_ERROR, b"\x04")]), None)
    try:
        with pytest.raises(MirrorError, match="zu viele Geräte"):
            _session(rx).connect()
    finally:
        rx.close()


@pytest.mark.parametrize("phase", ["control", "audio", "video"])
def test_refused_setups_are_explained(phase):
    rx = FakeReceiver(session_first=True, fail_setup={phase: 500})
    s = _session(rx)
    try:
        s.connect()
        with pytest.raises(MirrorError, match="abgelehnt"):
            s.setup(1920, 1080)
    finally:
        s.close()
        rx.close()


def test_a_refused_audio_shape_without_alternative_is_explained():
    rx = FakeReceiver(fail_setup={"audio": 400})
    s = _session(rx)
    try:
        s.connect()
        with pytest.raises(MirrorError, match="abgelehnt"):
            s.setup(1920, 1080)
    finally:
        s.close()
        rx.close()


def test_a_missing_or_unreachable_video_port_is_reported():
    for knob, text in (("no_data_port", "keinen Videokanal"),
                       ("closed_data_port", "nicht erreichbar")):
        rx = FakeReceiver(**{knob: True})
        s = _session(rx)
        try:
            s.connect()
            with pytest.raises(MirrorError, match=text):
                s.setup(1920, 1080)
        finally:
            s.close()
            rx.close()


def test_losing_the_control_connection_is_reported():
    rx = FakeReceiver(drop_after_record=True)
    failures = []
    s = _session(rx, on_failure=failures.append)
    try:
        s.connect()
        s.setup(1920, 1080)
        assert wait_for(lambda: failures)
    finally:
        s.close()
        rx.close()
    assert "verloren" in failures[0]


def test_timestamps_always_move_forward_and_restart_waits_for_a_keyframe(receiver):
    s = _session(receiver)
    try:
        s.connect()
        s.setup(1920, 1080)
        assert s.send_access_unit(IDR, local_ns=10_000_000_000)
        assert s.send_access_unit(P_FRAME, local_ns=5_000_000_000)    # Uhr springt zurück
        s.restart_video()
        assert not s.send_access_unit(P_FRAME)
        assert s.send_access_unit(IDR)
        assert wait_for(lambda: len(receiver.frames) == 3)
    finally:
        s.close()
    stamps = [f[2] for f in receiver.frames]
    assert stamps == sorted(stamps) and len(set(stamps)) == 3
    assert len(receiver.codec_configs) == 2
    assert s.size_from_receiver() == (1920, 1080)
    s.close()                                        # zweimal schadet nicht


def test_nothing_is_sent_once_the_data_channel_is_gone(receiver):
    s = _session(receiver)
    try:
        s.connect()
        s.setup(1920, 1080)
        s._data.close()
        assert not s.send_access_unit(IDR)            # schon das SPS/PPS-Paket scheitert
    finally:
        s.close()


# -- Verfälschte Kopplungsnachrichten -------------------------------------------

def _setup_until_m6(rx: FakeReceiver, pin: str = "1234"):
    state: dict = {}
    setup = hap.PairSetup(pin)
    m2, _ = rx._pair_setup(setup.m1(), state)
    m4, _ = rx._pair_setup(setup.m3(m2), state)
    m6, _ = rx._pair_setup(setup.m5(m4), state)
    return setup, m6


def _reseal(setup: hap.PairSetup, items) -> bytes:  # noqa: ANN001
    sealed = ChaCha20Poly1305(setup._session_key).encrypt(
        hap.nonce_for(b"PS-Msg06"), hap.tlv_encode(items), None)
    return hap.tlv_encode([(hap.TLV_STATE, b"\x06"), (hap.TLV_ENCRYPTED_DATA, sealed)])


def test_forged_pair_setup_answers_are_rejected(receiver):
    setup = hap.PairSetup("1234")
    with pytest.raises(hap.PairingError, match="Salt"):
        setup.m3(hap.tlv_encode([(hap.TLV_STATE, b"\x02")]))
    with pytest.raises(hap.PairingError, match="ungültiger"):
        setup.m3(hap.tlv_encode([(hap.TLV_SALT, b"s"), (hap.TLV_PUBLIC_KEY, b"\x00")]))
    state: dict = {}
    m2, _ = receiver._pair_setup(setup.m1(), state)
    setup.m3(m2)
    with pytest.raises(hap.PairingError, match="nicht echt") as info:
        setup.m5(hap.tlv_encode([(hap.TLV_PROOF, b"falsch")]))   # Gerät kennt die PIN nicht
    assert info.value.code == 2

    setup, m6 = _setup_until_m6(receiver)
    assert setup.finish(m6).atv_id == receiver.identifier
    with pytest.raises(hap.PairingError, match="nicht entschlüsselbar"):
        setup.finish(hap.tlv_encode([(hap.TLV_ENCRYPTED_DATA, b"x" * 40)]))
    with pytest.raises(hap.PairingError, match="Kennung"):
        setup.finish(_reseal(setup, [(hap.TLV_IDENTIFIER, b"TV")]))
    with pytest.raises(hap.PairingError, match="Signatur"):
        setup.finish(_reseal(setup, [(hap.TLV_IDENTIFIER, receiver.identifier),
                                     (hap.TLV_PUBLIC_KEY, receiver.ltpk),
                                     (hap.TLV_SIGNATURE, b"\x00" * 64)]))


def test_forged_pair_verify_answers_are_rejected(receiver):
    setup, m6 = _setup_until_m6(receiver)
    creds = setup.finish(m6)
    state: dict = {}
    verify = hap.PairVerify(creds)
    with pytest.raises(hap.PairingError, match="Schlüssel"):
        verify.m3(hap.tlv_encode([(hap.TLV_STATE, b"\x02")]))
    m2, _ = receiver._pair_verify(verify.m1(), state)
    tlv = hap.tlv_decode(m2)
    garbled = hap.tlv_encode([(hap.TLV_PUBLIC_KEY, tlv[hap.TLV_PUBLIC_KEY]),
                              (hap.TLV_ENCRYPTED_DATA, b"x" * 40)])
    with pytest.raises(hap.PairingError, match="nicht entschlüsselbar"):
        verify.m3(garbled)
    # Ein Gerät mit gleicher Kennung, aber anderem Schlüssel
    impostor_ltpk = hap._raw_public(hap.Ed25519PrivateKey.generate().public_key())
    impostor = hap.Credentials(impostor_ltpk, creds.ltsk, creds.atv_id, creds.client_id)
    other = hap.PairVerify(impostor)
    m2, _ = receiver._pair_verify(other.m1(), {})
    with pytest.raises(hap.PairingError, match="Signatur"):
        other.m3(m2)
    with pytest.raises(hap.PairingError, match="nicht abgeschlossen"):
        hap.PairVerify(creds).finish(b"")


# -- Kaputter RTSP-Verkehr ------------------------------------------------------

def _canned_server(reply: bytes):
    import socket
    import threading

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def run():
        conn, _ = sock.accept()
        conn.recv(65536)
        if reply:
            conn.sendall(reply)
        conn.close()
        sock.close()

    threading.Thread(target=run, daemon=True).start()
    return sock.getsockname()[1]


@pytest.mark.parametrize("reply, text", [
    (b"", "geschlossen"),
    (b"RTSP/1.0 200 OK\r\nX: " + b"y" * 70000, "zu groß"),
    (b"RTSP/1.0 200 OK\r\nContent-Length: 10\r\n\r\nabc", "geschlossen"),
])
def test_broken_responses_end_the_request(reply, text):
    from app.mirror.airplay_stream.rtsp import RtspClient

    client = RtspClient("127.0.0.1", _canned_server(reply))
    try:
        with pytest.raises(RtspError, match=text):
            client.request("GET", "/info")
    finally:
        client.close()


def test_response_bodies_must_be_plist_dictionaries():
    from app.mirror.airplay_stream.rtsp import Response

    assert Response(200, "OK", {}, b"").plist() == {}
    with pytest.raises(RtspError, match="keine gültige"):
        Response(200, "OK", {}, b"kein plist").plist()
    import plistlib
    with pytest.raises(RtspError, match="kein plist-Wörterbuch"):
        Response(200, "OK", {}, plistlib.dumps([1], fmt=plistlib.FMT_BINARY)).plist()


def test_a_failed_send_is_a_transport_error():
    from app.mirror.airplay_stream.rtsp import RtspClient

    client = RtspClient("127.0.0.1", _canned_server(b""))
    client.sock.close()
    with pytest.raises(RtspError):
        client.request("GET", "/info")
