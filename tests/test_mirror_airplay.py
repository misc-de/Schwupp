"""AirPlay-Spiegel-Engine: Kommandozeile, Auffinden des Programms, Fehlertexte.

Die Engine startet ein Hilfsprogramm; getestet wird deshalb, dass die Aufrufe
stimmen – insbesondere die beiden Einstellungen, die am echten Gerät den
Unterschied zwischen Standbild und laufendem Bild ausgemacht haben: H.264 statt
HEVC und ein fester UDP-Portbereich für die Firewall.
"""
from __future__ import annotations

import pytest

from app.mirror import airplay
from app.mirror.airplay import PORT_RANGE, AirplayMirrorEngine


class _Receiver:
    host = "192.168.0.50"
    kind = "airplay"
    info = None

    def __init__(self):
        self.pin_callback = lambda: "1234"


class _Config(dict):
    def device_value_for(self, info, key):  # noqa: ANN001, ARG002
        return self.get(key)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = _Config({"mirror_fps": 30, "mirror_bitrate_kbps": 0,
                      "mirror_audio": True, "mirror_target_delay_ms": 0,
                      "mirror_airplay_codec": "h264"})
    return AirplayMirrorEngine(_Receiver(), None, config)


def _command(engine, monkeypatch, tmp_path):
    """Startet run() mit einem abgefangenen Popen und liefert die Kommandozeile."""
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.stdout = iter([])
            self.stdin = None

        def poll(self):
            return 0

    monkeypatch.setattr(airplay, "_find_binary", lambda: "/usr/bin/doubletake")
    monkeypatch.setattr(airplay.subprocess, "Popen", FakePopen)
    with pytest.raises(RuntimeError):      # ohne Ausgabe kommt kein "ready"
        engine.run("")
    return captured["cmd"]


def test_command_uses_h264_and_a_fixed_port_range(engine, monkeypatch, tmp_path):
    """Beides war am echten Gerät entscheidend: HEVC ergab nur ein Standbild,
    und ohne festen Portbereich lässt sich keine Firewall-Regel angeben."""
    cmd = _command(engine, monkeypatch, tmp_path)
    assert "-video-codec" in cmd and cmd[cmd.index("-video-codec") + 1] == "h264"
    assert "-port-range" in cmd and cmd[cmd.index("-port-range") + 1] == PORT_RANGE
    assert cmd[cmd.index("-target") + 1] == "192.168.0.50"


def test_first_run_asks_for_pairing(engine, monkeypatch, tmp_path):
    cmd = _command(engine, monkeypatch, tmp_path)
    assert "-pair" in cmd           # noch keine Zugangsdaten vorhanden


def test_audio_is_included_when_enabled(engine, monkeypatch, tmp_path):
    assert "-no-audio" not in _command(engine, monkeypatch, tmp_path)


def test_audio_can_be_switched_off(engine, monkeypatch, tmp_path):
    engine.config["mirror_audio"] = False
    assert "-no-audio" in _command(engine, monkeypatch, tmp_path)


def test_latency_override_only_when_set(engine, monkeypatch, tmp_path):
    assert "-target-latency-ms" not in _command(engine, monkeypatch, tmp_path)
    engine.config["mirror_target_delay_ms"] = 80
    cmd = _command(engine, monkeypatch, tmp_path)
    assert cmd[cmd.index("-target-latency-ms") + 1] == "80"


# -- Auffinden des Programms ------------------------------------------------

def test_env_override_wins(monkeypatch, tmp_path):
    binary = tmp_path / "doubletake"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("SCHWUPP_DOUBLETAKE", str(binary))
    assert airplay._find_binary() == str(binary)


def test_missing_binary_is_reported_clearly(monkeypatch):
    monkeypatch.delenv("SCHWUPP_DOUBLETAKE", raising=False)
    monkeypatch.setattr(airplay.shutil, "which", lambda _n: None)
    monkeypatch.setattr(airplay.os, "access", lambda *a, **k: False)
    ok, detail = AirplayMirrorEngine.check_available()
    assert not ok
    assert "doubletake" in detail


# -- Fehlertexte ------------------------------------------------------------

@pytest.mark.parametrize("output,expected", [
    ("pairing failed: pair-setup: pair-setup M4 error: 2", "Code"),
    ("dial tcp: connection refused", "nicht erreichbar"),
    ("pairing credential cannot be empty", "kein Code"),
])
def test_output_becomes_a_helpful_message(output, expected):
    assert expected in AirplayMirrorEngine._explain(output)


def test_unknown_output_is_kept_verbatim():
    assert "seltsamer Fehler" in AirplayMirrorEngine._explain("seltsamer Fehler")


# -- Auswahl der Spiegel-Wege ------------------------------------------------

class _Recv:
    kind = "airplay"

    def __init__(self, video: bool):
        self._video = video

    def supports(self, feature):  # noqa: ANN001
        from app.receivers.base import Feature
        return self._video if feature == Feature.VIDEO else True


def test_hls_is_dropped_when_a_device_takes_no_video(monkeypatch):
    """HLS-Spiegelung schickt dem Gerät eine Video-URL – nimmt es die nicht an,
    gehört der Weg nicht in die Auswahl."""
    from app.mirror import engines_for_kind
    names = [e.name for e in engines_for_kind("airplay", _Recv(video=False))]
    assert names == ["airplay"]


def test_hls_stays_for_devices_that_play_video():
    from app.mirror import engines_for_kind
    names = [e.name for e in engines_for_kind("airplay", _Recv(video=True))]
    assert "hls" in names and "airplay" in names


def test_without_a_receiver_nothing_is_filtered():
    from app.mirror import engines_for_kind
    assert [e.name for e in engines_for_kind("airplay")] == ["airplay", "hls"]
