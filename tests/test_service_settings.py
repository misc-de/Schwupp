"""Einstellungen über die D-Bus-Schnittstelle (GetSettings/SetSetting/…), ohne Bus."""
from __future__ import annotations

import pytest

from app.config import Config
from app.discovery import ReceiverInfo
from app.service import CastService


class FakeApp:
    def __init__(self) -> None:
        self.config = Config()


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    svc = CastService(FakeApp())
    tv = ReceiverInfo(kind="chromecast", uuid="cast:tv", name="Wohnzimmer",
                      host="10.0.0.2", port=8009, model="Chromecast")
    svc._devices[tv.uuid] = tv
    return svc


def test_defaults_have_no_engine(service):
    out = service.get_settings("")
    assert out["mirror_height"].unpack() == 1080
    assert "mirror_engine" not in out
    assert out["resolutions"].unpack() == [720, 1080, 2160]


def test_device_follows_defaults_until_set(service):
    service.set_setting("", "mirror_fps", 25)
    out = service.get_settings("cast:tv")
    assert out["mirror_fps"].unpack() == 25
    assert out["custom"].unpack() is False
    service.set_setting("cast:tv", "mirror_fps", 50)
    assert service.get_settings("cast:tv")["mirror_fps"].unpack() == 50
    assert service.get_settings("")["mirror_fps"].unpack() == 25
    assert service.get_settings("cast:tv")["custom"].unpack() is True


def test_device_is_remembered_and_found_offline(service):
    service.set_setting("cast:tv", "mirror_height", 720)
    service._devices.clear()
    assert [row[0] for row in service._property("KnownDevices").unpack()] == ["cast:tv"]
    assert service.get_settings("cast:tv")["mirror_height"].unpack() == 720


def test_reset_and_forget(service):
    service.set_setting("cast:tv", "mirror_audio", False)
    service.reset_device("cast:tv")
    assert service.get_settings("cast:tv")["mirror_audio"].unpack() is True
    service.forget_device("cast:tv")
    service._devices.clear()
    with pytest.raises(LookupError):
        service.get_settings("cast:tv")


@pytest.mark.parametrize(("dev", "key", "value"), [
    ("", "mirror_height", 1000),
    ("", "mirror_fps", 120),
    ("", "mirror_engine", "native"),
    ("", "unknown", 1),
    ("cast:tv", "mirror_audio", 1),
    ("cast:tv", "mirror_engine", "nope"),
    ("cast:tv", "mirror_encoder", "nope"),
])
def test_invalid_values_are_refused(service, dev, key, value):
    with pytest.raises(ValueError):
        service.set_setting(dev, key, value)


def test_unknown_device(service):
    with pytest.raises(LookupError):
        service.get_settings("cast:none")
