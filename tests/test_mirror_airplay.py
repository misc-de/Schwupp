"""Eigene AirPlay-Engine: Kopplungsdaten, Ablauf, Encoder und Selbstheilung.

Das Protokoll selbst prüft ``test_airplay_stream``; hier geht es um das, was
die Engine darum herum entscheidet: welche Kopplung sie nimmt und wo sie neue
ablegt (die mit pyatv geteilte, damit der Fernseher nicht zweimal nach einem
Code fragt), dass ein Empfänger ohne PTP-Uhr einen zweiten Versuch mit NTP
bekommt, und dass die Bilder ohne Vorschaubild-Warten in die Sitzung gehen.
"""
from __future__ import annotations

import pytest

from app.mirror import airplay
from app.mirror.airplay import AirplayMirrorEngine
from app.mirror.airplay_stream import hap
from app.mirror.airplay_stream.session import MirrorError, PtpUnavailable

CREDS = hap.Credentials(b"\x01" * 32, b"\x02" * 32, b"TV", b"ME")
OTHER = hap.Credentials(b"\x03" * 32, b"\x04" * 32, b"TV", b"MIRROR")


class _Config(dict):
    saved = 0

    def __getitem__(self, key):
        return self.get(key)

    def device_value_for(self, info, key):  # noqa: ARG002
        return self.get(key)

    def save(self):
        self.saved += 1


class _Receiver:
    kind = "airplay"
    host = "192.0.2.40"
    info = None

    def __init__(self):
        self.pin_callback = lambda: "1234"


def _engine(**config):
    base = {"mirror_fps": 30, "mirror_bitrate_kbps": 6000, "mirror_height": 1080,
            "mirror_target_delay_ms": 150, "mirror_encoder": "auto",
            "airplay_creds": {}, "airplay_mirror_creds": {}}
    base.update(config)
    errors = []
    engine = AirplayMirrorEngine(_Receiver(), None, _Config(base), on_error=errors.append)
    engine._running = True
    return engine, errors


def test_the_playback_pairing_is_reused_for_mirroring():
    engine, _ = _engine(airplay_creds={"192.0.2.40": str(CREDS)})
    assert engine._credentials() == CREDS
    engine, _ = _engine(airplay_creds={"192.0.2.40": str(CREDS)},
                        airplay_mirror_creds={"192.0.2.40": str(OTHER)})
    assert engine._credentials() == OTHER          # eigene Spiegel-Kopplung zuerst
    engine, _ = _engine(airplay_creds={"192.0.2.99": str(CREDS)})
    assert engine._credentials() is None


def test_new_pairings_go_where_the_receiver_expects_them():
    engine, _ = _engine(airplay_creds={"192.0.2.7": "x"})
    engine._store_credentials(CREDS, True)
    assert engine.config["airplay_creds"] == {"192.0.2.7": "x", "192.0.2.40": str(CREDS)}
    engine._store_credentials(OTHER, False)
    assert engine.config["airplay_mirror_creds"] == {"192.0.2.40": str(OTHER)}
    assert engine.config.saved == 2


def test_only_airplay_devices_are_mirrored_this_way():
    engine, errors = _engine()
    engine._running = False
    engine.receiver.kind = "chromecast"
    engine.start()
    assert errors and "nur für AirPlay" in errors[0]


def test_availability_depends_on_encoder_and_capture(monkeypatch):
    from app.mirror import capture

    monkeypatch.setattr(capture, "available_encoders", lambda: [])
    ok, detail = AirplayMirrorEngine.check_available()
    assert not ok and "Encoder" in detail
    monkeypatch.setattr(capture, "available_encoders", lambda: ["x264"])
    monkeypatch.setattr(capture, "capture_available", lambda: (False, "kein Portal"))
    assert AirplayMirrorEngine.check_available() == (False, "kein Portal")
    monkeypatch.setattr(capture, "capture_available", lambda: (True, ""))
    assert AirplayMirrorEngine.check_available() == (True, "Bereit")


class _FakeSession:
    instances: list = []
    fail_setup: list = []

    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs
        self.closed = False
        self.sent: list[bytes] = []
        self.restarted = 0
        self.audio = None
        self.want_audio = None
        self.pcm: list[bytes] = []
        _FakeSession.instances.append(self)

    def connect(self):
        pass

    def setup(self, width, height, audio=False):
        self.size = (width, height)
        self.want_audio = audio
        if _FakeSession.fail_setup:
            raise _FakeSession.fail_setup.pop(0)

    def send_access_unit(self, data):
        self.sent.append(data)
        return True

    def send_audio(self, pcm, local_ns=None):
        self.pcm.append((pcm, local_ns))

    def restart_video(self):
        self.restarted += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_session(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.fail_setup = []
    monkeypatch.setattr(airplay, "MirrorSession", _FakeSession)
    return _FakeSession


def test_the_playback_session_is_released_before_mirroring(fake_session, monkeypatch):
    engine, _ = _engine()
    order = []
    engine.receiver.release = lambda: order.append("release")
    monkeypatch.setattr(airplay, "RELEASE_PAUSE_S", 0)
    monkeypatch.setattr(fake_session, "connect", lambda self: order.append("connect"))
    monkeypatch.setattr(engine, "_launch_encoder", lambda *a: None)
    engine.run("src")
    assert order == ["release", "connect"]


def test_run_connects_negotiates_and_starts_the_encoder(fake_session, monkeypatch):
    engine, _ = _engine(airplay_creds={"192.0.2.40": str(CREDS)})
    launched = []
    monkeypatch.setattr(engine, "_launch_encoder", lambda fps, kbps: launched.append((fps, kbps)))
    engine.run("pipewiresrc fd=3 path=4")
    (s,) = fake_session.instances
    assert s.host == "192.0.2.40" and s.size == (1920, 1080)
    assert s.kwargs["credentials"] == CREDS and s.kwargs["port_range"] == (60000, 60010)
    assert s.kwargs["latency_ms"] == 150 and not s.kwargs["force_ntp"]
    assert launched == [(30, 6000)]


def test_a_receiver_without_ptp_clock_gets_a_second_try_with_ntp(fake_session, monkeypatch):
    engine, _ = _engine()
    monkeypatch.setattr(engine, "_launch_encoder", lambda fps, kbps: None)
    fake_session.fail_setup = [PtpUnavailable("keine Uhr")]
    engine.run("src")
    first, second = fake_session.instances
    assert first.closed and not second.closed
    assert second.kwargs["force_ntp"] is True
    assert engine._session is second


def test_failures_close_the_session(fake_session):
    engine, _ = _engine()
    fake_session.fail_setup = [MirrorError("abgelehnt")]
    with pytest.raises(MirrorError):
        engine.run("src")
    assert fake_session.instances[0].closed


def test_a_stop_during_pairing_closes_quietly(fake_session, monkeypatch):
    engine, _ = _engine()
    monkeypatch.setattr(fake_session, "connect", lambda self: setattr(engine, "_running", False))
    engine.run("src")
    assert fake_session.instances[0].closed


class _Element:
    def connect(self, *args):
        self.callback = args[-1]

    def add_signal_watch(self):
        pass


class _Pipeline(_Element):
    def __init__(self, desc):
        self.desc = desc
        self.sink = _Element()
        self.state = None

    def get_by_name(self, name):
        return None if name == "portalsrc" else self.sink

    def get_bus(self):
        return _Element()

    def set_state(self, state):
        self.state = state


class _Gst:
    class State:
        PLAYING = "playing"
        NULL = "null"

    class FlowReturn:
        OK = "ok"

    class MapFlags:
        READ = 1

    def __init__(self):
        self.pipelines = []

    def parse_launch(self, desc):
        self.pipelines.append(_Pipeline(desc))
        return self.pipelines[-1]


def _with_gst(engine, monkeypatch):
    gst = _Gst()
    monkeypatch.setattr(airplay, "gst_init", lambda: gst)
    monkeypatch.setattr(airplay, "h264_encoder_desc", lambda *a: "fakeh264enc")
    return gst


def test_encoder_output_is_whole_frames_without_a_preroll(monkeypatch):
    engine, _ = _engine()
    gst = _with_gst(engine, monkeypatch)
    engine._source_desc = "pipewiresrc fd=3 path=4"
    engine._size = (1920, 1080)
    engine._launch_encoder(30, 6000)
    desc = gst.pipelines[0].desc
    assert desc.startswith("pipewiresrc fd=3 path=4 ")
    assert "width=1920,height=1080" in desc and "fakeh264enc" in desc
    assert "stream-format=byte-stream,alignment=au" in desc
    assert "appsink name=sink" in desc and "async=false" in desc
    assert gst.pipelines[0].state == "playing"


class _Buffer:
    def __init__(self, data):
        self.data = data
        self.unmapped = False

    def map(self, flags):  # noqa: ARG002
        return True, type("Info", (), {"data": self.data})()

    def unmap(self, info):  # noqa: ARG002
        self.unmapped = True


class _Sample:
    def __init__(self, data):
        self.buffer = _Buffer(data)

    def get_buffer(self):
        return self.buffer


class _Sink:
    def __init__(self, sample):
        self.sample = sample

    def emit(self, name):
        assert name == "pull-sample"
        return self.sample


def test_encoded_frames_go_to_the_session(fake_session, monkeypatch):
    engine, _ = _engine()
    engine._Gst = _Gst()
    session = fake_session("192.0.2.40")
    engine._session = session
    sample = _Sample(b"\x00\x00\x01\x65")
    assert engine._on_sample(_Sink(sample)) == "ok"
    assert session.sent == [b"\x00\x00\x01\x65"] and sample.buffer.unmapped
    assert engine._last_frame > 0
    assert engine._on_sample(_Sink(None)) == "ok"
    engine._running = False
    engine._on_sample(_Sink(_Sample(b"x")))
    assert len(session.sent) == 1


def test_a_frozen_capture_restarts_encoder_but_keeps_the_session(fake_session, monkeypatch):
    engine, errors = _engine()
    gst = _with_gst(engine, monkeypatch)
    engine._Gst = gst
    engine._size = (1920, 1080)
    session = fake_session("192.0.2.40")
    engine._session = session
    engine._pipeline = old = _Pipeline("alt")
    engine._capture = type("Cap", (), {"restart_sync": lambda self: "neue-quelle",
                                       "stop": lambda self: None})()
    engine._recover()
    assert old.state == "null" and session.restarted == 1
    assert gst.pipelines[-1].desc.startswith("neue-quelle ")
    assert not errors and not engine._recovering

    engine._capture = type("Cap", (), {"restart_sync": lambda self: None,
                                       "stop": lambda self: None})()
    engine._recover()
    assert "Freigabe" in errors[-1]


def test_repeated_freezes_end_the_mirroring(monkeypatch):
    engine, errors = _engine()
    engine._recover_times = [airplay.time.time()] * airplay.MAX_RECOVERIES
    engine._recover()
    assert "wiederholt" in errors[0]


def test_session_failures_reach_the_interface_once_running():
    engine, errors = _engine()
    engine._on_session_failure("Fernseher aus")
    assert errors == ["Fernseher aus"]
    engine._running = False
    engine._on_session_failure("später")
    assert errors == ["Fernseher aus"]


def test_stop_ends_pipeline_and_session(fake_session):
    engine, _ = _engine()
    engine._Gst = _Gst()
    engine._pipeline = pipeline = _Pipeline("x")
    engine._session = session = fake_session("192.0.2.40")
    engine.stop()
    assert pipeline.state == "null" and session.closed
    assert engine._session is None and not engine.running
    engine.stop()


def test_start_begins_capture_and_watches_for_frozen_pictures(monkeypatch):
    from app.mirror import engine as engine_module

    started = []
    monkeypatch.setattr(engine_module.MirrorEngine, "start", lambda self: started.append(1))
    engine, _ = _engine()
    engine._running = False
    recovered = []
    monkeypatch.setattr(engine, "_recover", lambda: recovered.append(1))
    ticks = iter([None, None, None])

    def sleep(_s):
        try:
            next(ticks)
        except StopIteration:
            engine._running = False

    monkeypatch.setattr(airplay.time, "sleep", sleep)
    engine._last_frame = airplay.time.monotonic() - 10      # seit 10 s kein Bild
    monkeypatch.setattr(airplay.threading, "Thread",
                        lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    engine._running = True
    engine.start()
    assert started == [1] and recovered


def test_a_stop_right_after_setup_closes_the_session(fake_session, monkeypatch):
    engine, _ = _engine()
    original = fake_session.setup

    def setup(self, width, height, audio=False):
        original(self, width, height, audio)
        engine._running = False

    monkeypatch.setattr(fake_session, "setup", setup)
    launched = []
    monkeypatch.setattr(engine, "_launch_encoder", lambda *a: launched.append(a))
    engine.run("src")
    assert fake_session.instances[0].closed and not launched


def test_gstreamer_errors_end_the_mirroring():
    engine, errors = _engine()
    message = type("M", (), {"parse_error": lambda self: ("kaputt", "debug")})()
    engine._on_bus_error(None, message)
    assert errors == ["GStreamer-Fehler beim Spiegeln: kaputt"]


def test_unmappable_buffers_are_skipped(fake_session):
    engine, _ = _engine()
    engine._Gst = _Gst()
    engine._session = session = fake_session("192.0.2.40")
    sample = _Sample(b"x")
    sample.buffer.map = lambda flags: (False, None)
    assert engine._on_sample(_Sink(sample)) == "ok"
    assert session.sent == []


def test_a_failing_restart_ends_the_mirroring(fake_session, monkeypatch):
    engine, errors = _engine()
    engine._Gst = _Gst()

    def broken(self):
        raise RuntimeError("Portal weg")

    engine._capture = type("Cap", (), {"restart_sync": broken, "stop": lambda self: None})()
    engine._recover()
    assert "Portal weg" in errors[0] and not engine._recovering


def test_sound_is_asked_for_when_wanted_and_a_source_exists(fake_session, monkeypatch):
    engine, _ = _engine(mirror_audio=True)
    monkeypatch.setattr(airplay, "audio_source_desc", lambda: "pulsesrc x")
    monkeypatch.setattr(engine, "_launch_encoder", lambda *a: None)
    started = []
    monkeypatch.setattr(engine, "_launch_audio", lambda: started.append(1))
    original = fake_session.setup

    def setup(self, width, height, audio=False):
        original(self, width, height, audio)
        self.audio = object()                       # Empfänger nimmt Ton

    monkeypatch.setattr(fake_session, "setup", setup)
    engine.run("src")
    assert fake_session.instances[0].want_audio is True and started == [1]
    engine, _ = _engine(mirror_audio=False)
    assert not engine._want_audio()
    monkeypatch.setattr(airplay, "audio_source_desc", lambda: None)
    engine, _ = _engine(mirror_audio=True)
    assert not engine._want_audio()


def test_the_sound_pipeline_reads_the_monitor_as_16_bit_pcm(monkeypatch):
    engine, _ = _engine()
    gst = _with_gst(engine, monkeypatch)
    engine._Gst = gst
    monkeypatch.setattr(airplay, "audio_source_desc", lambda: "pulsesrc do-timestamp=true")
    monkeypatch.setattr(airplay, "default_monitor_device", lambda: "plinius_tv.monitor")
    engine._launch_audio()
    desc = gst.pipelines[0].desc
    assert desc.startswith('pulsesrc device="plinius_tv.monitor" do-timestamp=true')
    assert "format=S16LE,rate=44100,channels=2" in desc and "appsink name=asink" in desc
    assert engine._audio_pipeline.state == "playing"

    def broken(desc):
        raise RuntimeError("kein pulsesrc")

    gst.parse_launch = broken
    engine._audio_pipeline = None
    engine._launch_audio()                                  # bleibt stumm, kein Abbruch
    assert engine._audio_pipeline is None


def test_sound_samples_go_to_the_session_with_their_start_time(fake_session, monkeypatch):
    engine, errors = _engine()
    engine._Gst = _Gst()
    engine._session = session = fake_session("192.0.2.40")
    monkeypatch.setattr(airplay, "boot_ns", lambda: 2_000_000_000)
    pcm = b"\x01\x00\x02\x00" * 441                            # 10 ms
    assert engine._on_audio_sample(_Sink(_Sample(pcm))) == "ok"
    assert session.pcm == [(pcm, 2_000_000_000 - 10_000_000)]
    sample = _Sample(b"x")
    sample.buffer.map = lambda flags: (False, None)
    engine._on_audio_sample(_Sink(sample))
    engine._on_audio_sample(_Sink(None))
    assert len(session.pcm) == 1
    engine._on_audio_error(None, type("M", (), {"parse_error": lambda self: ("weg", "")})())
    assert errors == []                                      # Ton-Fehler beendet nichts


def test_stop_also_ends_the_sound_pipeline(fake_session):
    engine, _ = _engine()
    engine._Gst = _Gst()
    engine._audio_pipeline = sound = _Pipeline("ton")
    engine.stop()
    assert sound.state == "null" and engine._audio_pipeline is None
