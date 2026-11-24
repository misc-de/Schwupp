"""Die Video-Pipelines warten nicht auf ein erstes Bild.

Unter wlroots liefert das Portal bei ruhendem Bildschirm kein neues Bild;
pipewiresrc wiederholt das letzte erst im Zustand PLAYING (keepalive-time).
Eine Senke, die vorher ein Vorschaubild verlangt, hielte die Pipeline in
PAUSED – der Fernseher bliebe schwarz.
"""

import pytest

from app.mirror import dlnats, native


class _Element:
    def connect(self, *args):
        pass

    def add_signal_watch(self):
        pass


class _Pipeline(_Element):
    def __init__(self, desc):
        self.desc = desc

    def get_by_name(self, name):
        return None if name == "portalsrc" else _Element()

    def get_bus(self):
        return _Element()

    def set_state(self, state):
        self.state = state


class _Gst:
    class State:
        PLAYING = "playing"

    def __init__(self):
        self.pipelines = []

    def parse_launch(self, desc):
        pipeline = _Pipeline(desc)
        self.pipelines.append(pipeline)
        return pipeline


def _engine(cls, monkeypatch, module):
    gst = _Gst()
    monkeypatch.setattr(module, "gst_init", lambda: gst)
    monkeypatch.setattr(module, "h264_encoder_desc", lambda *a: "fakeh264enc")
    engine = cls.__new__(cls)
    engine.receiver = None
    engine.config = {"mirror_encoder": "auto", "mirror_audio": False}
    return engine, gst


def test_native_video_sink_does_not_wait_for_a_preroll(monkeypatch):
    engine, gst = _engine(native.NativeMirrorEngine, monkeypatch, native)
    engine._source_desc = "pipewiresrc fd=3 path=4"
    engine._on_video_sample = engine._on_video_error = None
    engine._launch_encoder(1920, 1080, 30, 6_000_000)
    desc = gst.pipelines[0].desc
    assert desc.startswith("pipewiresrc fd=3 path=4 ")
    assert "appsink name=sink" in desc and "async=false" in desc
    assert gst.pipelines[0].state == "playing"


def test_dlna_video_sink_does_not_wait_for_a_preroll(monkeypatch):
    engine, gst = _engine(dlnats.DlnaTsMirrorEngine, monkeypatch, dlnats)
    built = {}
    monkeypatch.setattr(dlnats.DlnaTsMirrorEngine, "video_params", lambda self: (0, 0, 30, 6000))

    class Stop(Exception):
        pass

    class Server:
        def add_live(self, *a, **k):
            return "http://192.0.2.1/live", object()

    engine.server = Server()
    engine.receiver = type("R", (), {"host": "192.0.2.2"})()

    def parse_launch(desc):
        built["desc"] = desc
        raise Stop

    gst.parse_launch = parse_launch
    with pytest.raises(Stop):
        engine.run("pipewiresrc fd=3 path=4")
    assert "appsink name=sink" in built["desc"] and "async=false" in built["desc"]
