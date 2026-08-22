"""Konfiguration: Defaults, Persistenz und gerätespezifische Overrides."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.config import Config, device_keys


@dataclass
class FakeInfo:
    uuid: str
    host: str


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return Config()


def test_defaults_are_available_without_a_file(config):
    assert config["mirror_engine"] == "native"
    assert config["mirror_fps"] == 30
    assert config["mirror_audio"] is True


def test_unknown_key_is_none(config):
    assert config["nope"] is None


def test_save_and_reload(tmp_path, monkeypatch, config):
    config["mirror_fps"] = 60
    config.save()
    written = json.loads((tmp_path / "schwupp" / "config.json").read_text())
    assert written["mirror_fps"] == 60
    assert Config()["mirror_fps"] == 60


def test_broken_config_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "schwupp" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json")
    assert Config()["mirror_engine"] == "native"


# -- Geräte-Overrides -------------------------------------------------------

def test_device_keys_lists_uuid_and_host_variant():
    info = FakeInfo(uuid="cast:1234-abcd", host="192.168.0.33")
    assert device_keys(info) == ["cast:1234-abcd", "cast:192.168.0.33"]


def test_device_keys_without_host_variant():
    assert device_keys(FakeInfo(uuid="dlna:192.168.0.5", host="192.168.0.5")) == \
        ["dlna:192.168.0.5"]


def test_device_keys_of_an_object_without_uuid():
    assert device_keys(object()) == []


def test_device_value_falls_back_to_the_global_default(config):
    info = FakeInfo(uuid="cast:abc", host="10.0.0.2")
    assert config.device_value_for(info, "mirror_fps") == 30


def test_device_override_wins_over_global(config):
    info = FakeInfo(uuid="cast:abc", host="10.0.0.2")
    config.set_device_value_for(info, "mirror_fps", 15)
    assert config.device_value_for(info, "mirror_fps") == 15
    assert config["mirror_fps"] == 30          # global unberührt


def test_override_survives_a_uuid_change(config):
    """Ein Cast-Gerät taucht mal mit echter uuid, mal per Host auf – die
    Einstellung muss beide Wege überleben (siehe app/config.py:device_keys)."""
    with_uuid = FakeInfo(uuid="cast:1234-abcd", host="192.168.0.33")
    config.set_device_value_for(with_uuid, "mirror_engine", "hls")

    by_host = FakeInfo(uuid="cast:192.168.0.33", host="192.168.0.33")
    assert config.device_value_for(by_host, "mirror_engine") == "hls"


def test_overrides_are_per_device(config):
    a = FakeInfo(uuid="cast:a", host="10.0.0.1")
    b = FakeInfo(uuid="cast:b", host="10.0.0.2")
    config.set_device_value_for(a, "mirror_bitrate_kbps", 2000)
    assert config.device_value_for(b, "mirror_bitrate_kbps") == 6000
