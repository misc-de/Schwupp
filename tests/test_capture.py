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
    assert capture.available_encoders() == ["x264", "openh264", "vaapi", "v4l2"]
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

    monkeypatch.setattr(capture, "PortalScreenCast", SlowPortal)
    result = _run(selector)
    selector.stop()
    pending["cb"](7, 21)
    assert result == {}


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
