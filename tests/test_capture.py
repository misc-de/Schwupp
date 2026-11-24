"""Encoder-Auswahl und Capture-Fallback-Kette (ohne echtes GStreamer)."""
from __future__ import annotations

import pytest

from app.mirror import capture


@pytest.fixture
def fake_elements(monkeypatch):
    """Lässt gst_element_exists nur die genannten Elemente melden."""
    present: set[str] = set()

    def configure(*names: str) -> None:
        present.clear()
        present.update(names)

    monkeypatch.setattr(capture, "gst_element_exists", lambda name: name in present)
    return configure


def test_prefers_x264_when_everything_is_available(fake_elements):
    fake_elements("x264enc", "openh264enc", "vaapih264enc", "v4l2h264enc")
    assert capture.available_encoders() == ["x264", "vaapi", "v4l2", "openh264"]
    assert capture.h264_encoder_desc(4000, 30).startswith("x264enc")


def test_falls_back_to_the_next_available_encoder(fake_elements):
    fake_elements("openh264enc")
    desc = capture.h264_encoder_desc(4000, 30)
    assert desc.startswith("openh264enc")
    assert "bitrate=4000000" in desc          # openh264 rechnet in bit/s


def test_x264_gets_bitrate_in_kbit(fake_elements):
    fake_elements("x264enc")
    desc = capture.h264_encoder_desc(2500, 25)
    assert "bitrate=2500" in desc
    assert "key-int-max=25" in desc
    assert "tune=zerolatency" in desc


def test_explicit_choice_is_honoured(fake_elements):
    fake_elements("x264enc", "vaapih264enc")
    assert capture.h264_encoder_desc(3000, 30, "vaapi").startswith("vaapih264enc")


def test_unavailable_explicit_choice_falls_back(fake_elements):
    fake_elements("x264enc")
    assert capture.h264_encoder_desc(3000, 30, "vaapi").startswith("x264enc")


def test_no_encoder_raises_a_readable_error(fake_elements):
    fake_elements()
    with pytest.raises(RuntimeError, match="Kein H.264-Encoder"):
        capture.h264_encoder_desc(3000, 30)


# -- Quellbeschreibungen ----------------------------------------------------

def test_x11_source_includes_framerate_and_pointer():
    desc = capture.x11_source_desc(fps=25)
    assert desc.startswith("ximagesrc")
    assert "show-pointer=true" in desc
    assert "framerate=25/1" in desc


def test_pipewire_source_wires_fd_and_node():
    desc = capture.pipewire_source_desc(fd=11, node_id=42, fps=60)
    assert "pipewiresrc fd=11 path=42" in desc
    assert "framerate=60/1" in desc
    # Ruhender Bildschirm: Portal-Puffer frei, letztes Bild wiederholen
    assert "always-copy=true" in desc
    assert f"keepalive-time={capture.KEEPALIVE_MS}" in desc
    assert f" name={capture.PORTAL_SOURCE} " in desc
    # Das Portal meldet ein unsinniges Pixelseitenverhältnis
    assert "capssetter caps=video/x-raw,pixel-aspect-ratio=1/1" in desc
    # Gedrehtes Telefon -> Fernseher quer
    assert "videoflip video-direction=auto" in desc


def test_auto_prefers_a_hardware_encoder_over_openh264(monkeypatch):
    """openh264enc kann kein Main-Profil – auto nimmt ihn nur, wenn sonst nichts da ist."""
    monkeypatch.setattr(capture, "gst_element_exists",
                        lambda e: e in ("openh264enc", "v4l2h264enc"))
    assert "v4l2h264enc " in capture.h264_encoder_desc(6000, 30)


def test_v4l2_encoder_sends_sps_pps_with_the_key_frame(monkeypatch):
    """sequence_header_mode=1 ließ MediaTeks Encoder SPS/PPS weglassen: schwarzer Fernseher."""
    monkeypatch.setattr(capture, "gst_element_exists", lambda e: e == "v4l2h264enc")
    desc = capture.h264_encoder_desc(6000, 30, "v4l2")
    assert "sequence_header_mode=0" in desc
    assert "h264_i_frame_period=30" in desc
    # Schwarz kam als Rot an, wenn der Encoder RGB selbst wandelte: immer NV12 davor
    assert desc.startswith("videoconvert ! video/x-raw,format=NV12 ! v4l2h264enc ")


# -- Auswahlkette -----------------------------------------------------------

def _selector(monkeypatch, *, x11=False, portal=False, wf=False):
    monkeypatch.setattr(capture, "x11_capture_available", lambda: x11)
    monkeypatch.setattr(capture, "screencast_portal_available", lambda: portal)
    monkeypatch.setattr(capture, "wf_recorder_available", lambda: wf)
    return capture.CaptureSelector(fps=30)


def _run(selector):
    result = {}
    selector.start(on_ready=lambda d: result.setdefault("ready", d),
                   on_error=lambda e: result.setdefault("error", e))
    return result


def test_x11_is_used_first(monkeypatch):
    result = _run(_selector(monkeypatch, x11=True, portal=True, wf=True))
    assert result["ready"].startswith("ximagesrc")


def test_portal_is_used_when_there_is_no_x11(monkeypatch):
    selector = _selector(monkeypatch, portal=True)
    started = {}

    class FakePortal:
        def __init__(self, fps):
            started["fps"] = fps

        def start(self, callback):
            callback(7, 21)                     # fd, node_id

    monkeypatch.setattr(capture, "PortalScreenCast", FakePortal)
    result = _run(selector)
    assert started["fps"] == 30
    assert "pipewiresrc fd=7 path=21" in result["ready"]


def test_portal_failure_falls_back_to_wf_recorder(monkeypatch):
    selector = _selector(monkeypatch, portal=True, wf=True)

    class FailingPortal:
        def __init__(self, fps):
            pass

        def start(self, callback):
            callback(None, "kein Backend")

    class FakeWf:
        def __init__(self, fps):
            pass

        def source_desc(self):
            return "fdsrc fd=3 ! tsdemux"

        def stop(self):
            pass

    monkeypatch.setattr(capture, "PortalScreenCast", FailingPortal)
    monkeypatch.setattr(capture, "WfRecorderCapture", FakeWf)
    result = _run(selector)
    assert result["ready"].startswith("fdsrc")


def test_no_capture_path_reports_a_helpful_error(monkeypatch):
    result = _run(_selector(monkeypatch))
    assert "error" in result
    assert "Bildschirmaufnahme" in result["error"]


def test_cancelled_selector_swallows_a_late_portal_callback(monkeypatch):
    """Der Nutzer stoppt, während der Portal-Dialog noch offen ist."""
    selector = _selector(monkeypatch, portal=True)
    pending = {}

    class SlowPortal:
        def __init__(self, fps):
            pass

        def start(self, callback):
            pending["cb"] = callback

        def close(self):
            pending["closed"] = True

    monkeypatch.setattr(capture, "PortalScreenCast", SlowPortal)
    result = _run(selector)
    selector.stop()
    pending["cb"](7, 21)
    assert result == {}
    assert pending["closed"]                 # die Freigabe endet beim Portal
    selector.stop()                          # zweites Stoppen schadet nicht


def test_restart_sync_rebuilds_x11_but_not_portal(monkeypatch):
    x11 = _selector(monkeypatch, x11=True)
    _run(x11)
    assert x11.restart_sync().startswith("ximagesrc")

    portal = _selector(monkeypatch, portal=True)

    class FakePortal:
        def __init__(self, fps):
            pass

        def start(self, callback):
            callback(3, 4)

    monkeypatch.setattr(capture, "PortalScreenCast", FakePortal)
    _run(portal)
    assert portal.restart_sync() is None     # Freigabe muss neu bestätigt werden


# --- Portal-Session: eindeutige Tokens, Close beim Stoppen -------------------

class _FakeBus:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def call(self, bus, path, iface, method, *args):
        if self.fail:
            raise RuntimeError("bus weg")
        self.calls.append((bus, path, iface, method))


class _FakeGio:
    class DBusCallFlags:
        NONE = 0


def _portal(handle="/org/freedesktop/portal/desktop/session/1_2/schwupp_session_2", bus=None):
    portal = capture.PortalScreenCast(fps=30)
    portal._bus = bus or _FakeBus()
    portal._Gio = _FakeGio
    portal._session_handle = handle
    return portal


def test_tokens_stay_unique_across_screencasts():
    """Ein Dienst spiegelt mehrmals: das Portal lehnt einen schon benutzten Pfad ab."""
    first, second = capture.PortalScreenCast(), capture.PortalScreenCast()
    tokens = [first._new_token("session"), second._new_token("session"),
              first._new_token("session")]
    assert len(set(tokens)) == 3
    assert all(t.startswith("schwupp_session_") for t in tokens)


def test_close_ends_the_portal_session_once():
    portal = _portal()
    portal.close()
    portal.close()
    assert portal._bus.calls == [(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop/session/1_2/schwupp_session_2",
        "org.freedesktop.portal.Session", "Close")]


def test_close_without_a_session_does_nothing():
    portal = _portal(handle=None)
    portal.close()
    assert portal._bus.calls == []
    capture.PortalScreenCast().close()       # noch nie gestartet: kein Bus


def test_close_survives_a_vanished_portal(capsys):
    portal = _portal(bus=_FakeBus(fail=True))
    portal.close()
    assert portal._session_handle is None
    assert "bus weg" in capsys.readouterr().out


def test_a_session_created_after_stop_is_closed_at_once():
    portal = _portal(handle=None)
    steps = []
    portal._select_sources = lambda: steps.append("select")
    portal.close()
    portal._on_session_created(0, {"session_handle": "/s/late"})
    assert steps == []
    assert [c[1] for c in portal._bus.calls] == ["/s/late"]


def test_answers_after_stop_are_ignored():
    portal = _portal()
    steps = []
    portal._start_cast = lambda: steps.append("start")
    portal._open_remote = lambda node: steps.append("remote")
    portal.close()
    portal._on_sources_selected(0, {})
    portal._on_started(0, {"streams": [(5, {})]})
    assert steps == []


def test_wf_recorder_needs_the_h264_decoder(monkeypatch):
    """Ohne avdec_h264 scheitert wf-recorder erst in der Pipeline und verdeckt den Portal-Fehler."""
    monkeypatch.setattr(capture.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(capture, "gst_element_exists", lambda name: False)
    assert not capture.wf_recorder_available()
    monkeypatch.setattr(capture, "gst_element_exists", lambda name: name == "avdec_h264")
    assert capture.wf_recorder_available()
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)
    assert not capture.wf_recorder_available()


# --- Zeitstempel der Portal-Quelle -------------------------------------------

class _ProbeGst:
    CLOCK_TIME_NONE = 2**64 - 1

    class PadProbeReturn:
        OK = "ok"
        DROP = "drop"

    class PadProbeType:
        BUFFER = "buffer"

    class BufferFlags:
        CORRUPTED = "corrupted"


class _Buf:
    def __init__(self, corrupted=False):
        self.pts = 17_000_000_000_000      # Systemlaufzeit in ns, wie vom Portal
        self.dts = 0
        self.corrupted = corrupted

    def has_flags(self, flag):
        return self.corrupted and flag == _ProbeGst.BufferFlags.CORRUPTED


class _Info:
    def __init__(self, buf):
        self.buf = buf

    def get_buffer(self):
        return self.buf


class _Pad:
    def add_probe(self, kind, probe):
        self.kind, self.probe = kind, probe


class _Src:
    def __init__(self):
        self.pad = _Pad()

    def get_static_pad(self, name):
        assert name == "src"
        return self.pad


class _Pipe:
    def __init__(self, src):
        self.src = src
        self.clock = "pipewire"

    def get_by_name(self, name):
        return self.src if name == capture.PORTAL_SOURCE else None

    def use_clock(self, clock):
        self.clock = clock


def test_retime_does_nothing_without_a_portal_source():
    pipe = _Pipe(None)
    assert capture.retime_portal_source(pipe, _ProbeGst) is False
    assert pipe.clock == "pipewire"


def test_retime_stamps_frames_on_arrival(monkeypatch):
    """Systemlaufzeit als PTS ließ pipewiresrc Stunden warten – jetzt Zeit ab erstem Bild."""
    src = _Src()
    pipe = _Pipe(src)
    assert capture.retime_portal_source(pipe, _ProbeGst) is True
    assert pipe.clock is None                    # nichts wartet auf eine Uhr
    probe = src.pad.probe
    assert src.pad.kind == _ProbeGst.PadProbeType.BUFFER

    now = iter([5_000_000_000, 5_033_000_000])
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: next(now))
    first, second = _Buf(), _Buf()
    assert probe(None, _Info(first)) == _ProbeGst.PadProbeReturn.OK
    assert probe(None, _Info(second)) == _ProbeGst.PadProbeReturn.OK
    assert (first.pts, second.pts) == (0, 33_000_000)
    assert second.dts == _ProbeGst.CLOCK_TIME_NONE


def test_retime_drops_the_empty_pause_frame():
    src = _Src()
    capture.retime_portal_source(_Pipe(src), _ProbeGst)
    assert src.pad.probe(None, _Info(_Buf(corrupted=True))) == _ProbeGst.PadProbeReturn.DROP
    assert src.pad.probe(None, _Info(None)) == _ProbeGst.PadProbeReturn.OK
