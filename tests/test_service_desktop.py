"""Desktop auf dem Fernseher (ConnectDesktop), ohne Bus, systemd und Fernseher.

Der Desktop-Dienst, der Empfänger und die Engine sind Attrappen; Worker-Threads
und GLib.idle_add laufen sofort im Test-Thread.
"""
from __future__ import annotations

import os
import stat
import subprocess

import pytest
from gi.repository import GLib

from app import service as svc_mod
from app.config import Config
from app.discovery import ReceiverInfo
from app.service import CastService


class FakeApp:
    def __init__(self) -> None:
        self.config = Config()
        self.server = object()
        self.holds = 0

    def hold(self) -> None:
        self.holds += 1

    def release(self) -> None:
        self.holds -= 1


class FakeReceiver:
    def __init__(self, info, context) -> None:  # noqa: ANN001
        self.info = info
        self.kind = info.kind
        self.name = info.name

    def connect(self, prompt_cb=None, pin_cb=None) -> None:  # noqa: ANN001
        pass

    def disconnect(self) -> None:
        pass


class FakeEngine:
    display_name = "Fake"
    started: list[str] = []

    def __init__(self, receiver, server, config, on_error, on_started) -> None:  # noqa: ANN001
        self.receiver = receiver

    @classmethod
    def check_available(cls):  # noqa: ANN206
        return True, ""

    def start(self) -> None:
        # Was die Bildschirmauswahl in diesem Moment vorfände
        stamp = svc_mod._runtime_dir() / "plinius-screencast-consent"
        FakeEngine.started.append(stamp.read_text() if stamp.exists() else None)

    def stop(self) -> None:
        pass


class Now:
    """threading.Thread, das sofort läuft – oder erst, wenn der Test es will."""

    deferred: list = []
    later = False

    def __init__(self, target, args=(), daemon=None, name=None) -> None:  # noqa: ANN001
        self.target, self.args = target, args

    def start(self) -> None:
        if Now.later and self.target.__name__ == "work":
            Now.deferred.append(self)
        else:
            self.target(*self.args)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(GLib, "idle_add", lambda func, *args: func(*args))
    Now.deferred, Now.later = [], False
    monkeypatch.setattr(svc_mod.threading, "Thread", Now)
    monkeypatch.setattr(svc_mod, "create_receiver", FakeReceiver)
    monkeypatch.setattr(svc_mod, "get_engine_class", lambda name: FakeEngine)
    FakeEngine.started = []
    calls = []
    monkeypatch.setattr(svc_mod, "desktop_available", lambda: True)
    monkeypatch.setattr(svc_mod, "start_desktop",
                        lambda: calls.append("start") or "HEADLESS-1")
    monkeypatch.setattr(svc_mod, "stop_desktop", lambda: calls.append("stop"))
    service = CastService(FakeApp())
    service._engine_name = lambda receiver: "fake"
    for info in (ReceiverInfo(kind="chromecast", uuid="cast:tv", name="Wohnzimmer",
                              host="10.0.0.2", port=8009, model="Chromecast"),
                 ReceiverInfo(kind="raop", uuid="raop:box", name="Küche",
                              host="10.0.0.3", port=7000, model="")):
        service._devices[info.uuid] = info
    service.calls = calls
    service.runtime = runtime
    return service


def prop(service, name):
    return service._property(name).unpack()


# ----- der ganze Weg -----

def test_connect_desktop_shares_the_desktops_screen(env):
    env.connect("cast:tv", desktop=True)
    assert env.calls == ["start"]
    # Die Bildschirmauswahl fand beim Start den Namen des Desktops vor
    assert FakeEngine.started == ["HEADLESS-1"]
    stamp = env.runtime / "plinius-screencast-consent"
    assert stat.S_IMODE(stamp.stat().st_mode) == 0o600
    assert env.state == "starting"
    assert prop(env, "Source") == "desktop"
    assert prop(env, "ActiveDevice") == "cast:tv"

    env.stop_streaming()
    # Die Fenster kehren aufs Telefon zurück
    assert env.calls == ["start", "stop"]
    assert prop(env, "Source") == ""
    # Ein zweites Stoppen beendet nichts mehr
    env.stop_streaming()
    assert env.calls == ["start", "stop"]


def test_connect_mirrors_the_phone_as_before(env):
    env.connect("cast:tv")
    assert env.calls == []
    assert FakeEngine.started == [None]
    assert prop(env, "Source") == "screen"
    env.stop_streaming()
    assert env.calls == []


def test_desktop_then_phone_leaves_no_desktop(env):
    env.connect("cast:tv", desktop=True)
    env.connect("cast:tv")
    assert env.calls == ["start", "stop"]
    assert prop(env, "Source") == "screen"


@pytest.mark.parametrize("uuid, available", [("raop:box", True), ("cast:tv", False)])
def test_connect_desktop_only_to_a_tv_and_only_with_plinius_desktop(env, monkeypatch, uuid,
                                                                   available):
    monkeypatch.setattr(svc_mod, "desktop_available", lambda: available)
    with pytest.raises(ValueError):
        env.connect(uuid, desktop=True)
    assert env.calls == [] and env.state == "idle"


def test_connect_desktop_to_an_unknown_device(env):
    with pytest.raises(LookupError):
        env.connect("cast:gone", desktop=True)


def test_a_desktop_that_does_not_start_is_an_error(env, monkeypatch):
    def broken():
        env.calls.append("start")
        raise RuntimeError("no virtual screen")
    monkeypatch.setattr(svc_mod, "start_desktop", broken)
    env.connect("cast:tv", desktop=True)
    assert FakeEngine.started == []
    assert "no virtual screen" in prop(env, "Error")
    assert env.state == "idle"
    assert env.calls == ["start", "stop"]


def test_cancelled_while_the_desktop_starts(env):
    Now.later = True
    env.connect("cast:tv", desktop=True)
    env.stop_streaming()
    assert env.calls == ["stop"]
    # Der Start kommt spät zurück: Niemand zeigt den Desktop, er geht wieder
    Now.later = False
    Now.deferred.pop().start()
    assert env.calls == ["stop", "start", "stop"]
    assert FakeEngine.started == []


def test_late_start_leaves_a_newer_desktop_alone(env):
    Now.later = True
    env.connect("cast:tv", desktop=True)
    first = Now.deferred.pop()
    env.connect("cast:tv", desktop=True)
    second = Now.deferred.pop()
    Now.later = False
    first.start()
    second.start()
    assert env.calls == ["stop", "start", "start"]
    assert FakeEngine.started == ["HEADLESS-1"]


def test_no_consent_without_a_runtime_directory(env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing"))
    env.connect("cast:tv", desktop=True)
    assert FakeEngine.started == []
    assert prop(env, "Error")
    assert env.calls == ["start", "stop"]


# ----- D-Bus -----

class Invocation:
    def __init__(self) -> None:
        self.value = self.error = None

    def return_value(self, value) -> None:  # noqa: ANN001
        self.value = value

    def return_dbus_error(self, name, message) -> None:  # noqa: ANN001
        self.error = (name, message)


def test_connect_desktop_over_dbus(env):
    inv = Invocation()
    env._on_method_call(None, ":1.5", svc_mod.OBJECT_PATH, svc_mod.INTERFACE, "ConnectDesktop",
                        GLib.Variant("(s)", ("cast:tv",)), inv)
    assert inv.error is None
    assert prop(env, "Source") == "desktop"
    inv = Invocation()
    env._on_method_call(None, ":1.5", svc_mod.OBJECT_PATH, svc_mod.INTERFACE, "ConnectDesktop",
                        GLib.Variant("(s)", ("raop:box",)), inv)
    assert inv.error[0] == "org.freedesktop.DBus.Error.InvalidArgs"


def test_the_interface_describes_the_desktop():
    node = __import__("gi.repository.Gio", fromlist=["Gio"]).DBusNodeInfo.new_for_xml(svc_mod._XML)
    iface = node.interfaces[0]
    assert "ConnectDesktop" in [m.name for m in iface.methods]
    assert {"DesktopAvailable", "Source"} <= {p.name for p in iface.properties}


@pytest.mark.parametrize("available", [True, False])
def test_desktop_available_property(env, monkeypatch, available):
    monkeypatch.setattr(svc_mod, "desktop_available", lambda: available)
    assert prop(env, "DesktopAvailable") is available


# ----- die Helfer selbst -----

def test_desktop_available_looks_for_the_unit(tmp_path, monkeypatch):
    user, etc = tmp_path / "lib", tmp_path / "etc"
    monkeypatch.setattr(svc_mod, "DESKTOP_UNIT_DIRS", (str(user), str(etc)))
    assert not svc_mod.desktop_available()
    etc.mkdir()
    (etc / svc_mod.DESKTOP_UNIT).write_text("[Service]\n")
    assert svc_mod.desktop_available()


class Run:
    def __init__(self, returncode=0, stderr="", exc=None) -> None:  # noqa: ANN001
        self.returncode, self.stderr, self.exc = returncode, stderr, exc
        self.cmds = []

    def __call__(self, cmd, **kwargs):  # noqa: ANN001, ANN204
        self.cmds.append((cmd, kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)


def _screen(tmp_path, monkeypatch, name):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    if name is not None:
        (tmp_path / "plinius-desktop-screen").mkdir()
        (tmp_path / "plinius-desktop-screen" / "output").write_text(name)


def test_start_desktop_returns_the_screen(tmp_path, monkeypatch):
    _screen(tmp_path, monkeypatch, "HEADLESS-1\n")
    run = Run()
    monkeypatch.setattr(svc_mod.subprocess, "run", run)
    assert svc_mod.start_desktop() == "HEADLESS-1"
    (cmd, kwargs), = run.cmds
    assert cmd == ["systemctl", "--user", "start", "plinius-desktop-cast.service"]
    assert kwargs["timeout"] == svc_mod.DESKTOP_START_TIMEOUT_S


@pytest.mark.parametrize("run, name, message", [
    (Run(1, "Job failed. See journalctl"), "HEADLESS-1", "Job failed"),
    (Run(5, ""), "HEADLESS-1", "systemctl: 5"),
    (Run(exc=subprocess.TimeoutExpired("systemctl", 30)), "HEADLESS-1", "timed out"),
    (Run(exc=FileNotFoundError("systemctl")), "HEADLESS-1", "systemctl"),
    (Run(), None, "service.no_desktop_screen"),
    (Run(), "", "service.no_desktop_screen"),
])
def test_start_desktop_failures(tmp_path, monkeypatch, run, name, message):
    _screen(tmp_path, monkeypatch, name)
    monkeypatch.setattr(svc_mod.subprocess, "run", run)
    monkeypatch.setattr(svc_mod, "t", lambda key, **kw: key)
    with pytest.raises(RuntimeError, match=message):
        svc_mod.start_desktop()


def test_stop_desktop_does_not_wait(monkeypatch):
    run = Run()
    monkeypatch.setattr(svc_mod.subprocess, "run", run)
    svc_mod.stop_desktop()
    assert run.cmds[0][0] == ["systemctl", "--user", "stop", "--no-block",
                              "plinius-desktop-cast.service"]


def test_stop_desktop_survives_a_missing_systemctl(monkeypatch, capsys):
    monkeypatch.setattr(svc_mod.subprocess, "run", Run(exc=FileNotFoundError("systemctl")))
    svc_mod.stop_desktop()
    assert "Desktop" in capsys.readouterr().out


def test_write_consent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    stamp = tmp_path / "plinius-screencast-consent"
    stamp.write_text("old and longer")
    svc_mod.write_consent("HEADLESS-1")
    assert stamp.read_text() == "HEADLESS-1"


def test_runtime_dir_without_the_variable(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert str(svc_mod._runtime_dir()) == f"/run/user/{os.getuid()}"
