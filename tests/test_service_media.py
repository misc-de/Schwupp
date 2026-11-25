"""Videos und Links über die Cast-Schnittstelle (PlayFile, PlayLink, Choose…, Actions).

Ohne Bus, Fernseher und Fenster: Empfänger, Medienserver und Dialoge sind
Attrappen; Worker-Threads und GLib.idle_add laufen sofort im Test-Thread.
"""
from __future__ import annotations

import pytest
from gi.repository import GLib

from app import service as svc_mod
from app.config import Config
from app.discovery import ReceiverInfo
from app.receivers import Feature, static_features
from app.service import CastService, device_actions


class FakeServer:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def add_file(self, path, client_host=None):  # noqa: ANN001
        url = f"http://192.0.2.1:8000/{len(self.files)}"
        self.files[url] = path
        return url

    def remove_file(self, url) -> None:  # noqa: ANN001
        self.files.pop(url, None)


class FakeApp:
    def __init__(self) -> None:
        self.config = Config()
        self.server = FakeServer()
        self.holds = 0
        self.inhibited: list[str] = []

    def hold(self) -> None:
        self.holds += 1

    def release(self) -> None:
        self.holds -= 1

    def inhibit(self, window, flags, reason):  # noqa: ANN001
        self.inhibited.append(reason)
        return len(self.inhibited)

    def uninhibit(self, cookie) -> None:  # noqa: ANN001
        pass


class FakeReceiver:
    youtube = True
    fail = None
    log: list = []

    def __init__(self, info, context) -> None:  # noqa: ANN001
        self.info = info
        self.kind = info.kind
        self.name = info.name
        self.host = info.host

    def connect(self, prompt_cb=None, pin_cb=None) -> None:  # noqa: ANN001
        FakeReceiver.log.append("connect")

    def disconnect(self) -> None:
        FakeReceiver.log.append("disconnect")

    def stop(self) -> None:
        FakeReceiver.log.append("stop")
        if FakeReceiver.fail == "stop":
            raise RuntimeError("weg")

    def supports(self, feature) -> bool:  # noqa: ANN001
        return feature != Feature.YOUTUBE or FakeReceiver.youtube

    def play_media(self, url, mime, *, title=None, live=False) -> None:  # noqa: ANN001
        if FakeReceiver.fail == "play":
            raise RuntimeError("Fernseher sagt nein")
        FakeReceiver.log.append(("media", url, mime, title))

    def play_youtube(self, video_id) -> None:  # noqa: ANN001
        FakeReceiver.log.append(("youtube", video_id))


class Now:
    def __init__(self, target, args=(), daemon=None, name=None) -> None:  # noqa: ANN001
        self.target, self.args = target, args

    def start(self) -> None:
        self.target(*self.args)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(GLib, "idle_add", lambda func, *args: func(*args))
    monkeypatch.setattr(svc_mod.threading, "Thread", Now)
    monkeypatch.setattr(svc_mod, "create_receiver", FakeReceiver)
    monkeypatch.setattr(svc_mod, "desktop_available", lambda: False)
    monkeypatch.setattr(svc_mod.youtube, "yt_dlp_available", lambda: True)
    FakeReceiver.log, FakeReceiver.fail, FakeReceiver.youtube = [], None, True
    service = CastService(FakeApp())
    for info in (ReceiverInfo(kind="chromecast", uuid="cast:tv", name="Wohnzimmer",
                              host="192.0.2.2", port=8009, model="Chromecast"),
                 ReceiverInfo(kind="raop", uuid="raop:box", name="Küche",
                              host="192.0.2.3", port=7000, model="")):
        service._devices[info.uuid] = info
    video = tmp_path / "Urlaub.mp4"
    video.write_bytes(b"\0")
    service.video = str(video)
    return service


def prop(service, name):
    return service._property(name).unpack()


# ----- was je Gerät geht -----

def test_static_features_without_a_connection():
    assert Feature.YOUTUBE in static_features("chromecast")
    assert static_features("raop") == {Feature.SOUND}
    assert static_features("unbekannt") == frozenset()
    # AirPlay-Bewegtbild steht erst nach der Verbindung fest
    assert Feature.VIDEO not in static_features("airplay")


def test_actions_per_device(env, monkeypatch):
    assert prop(env, "Actions") == {"cast:tv": ["mirror", "youtube", "video"], "raop:box": []}
    monkeypatch.setattr(svc_mod, "desktop_available", lambda: True)
    assert device_actions(env._devices["cast:tv"]) == ["mirror", "youtube", "video", "desktop"]


def test_youtube_only_with_the_app_or_yt_dlp(monkeypatch):
    """Ein Fernseher ohne YouTube-App bekommt Links nur, wenn yt-dlp sie auflösen kann."""
    tv = ReceiverInfo(kind="chromecast", uuid="cast:tv", name="Flur", host="192.0.2.4",
                      port=8009, model="")
    monkeypatch.setattr(svc_mod, "desktop_available", lambda: False)
    monkeypatch.setattr(svc_mod, "static_features",
                        lambda kind: frozenset({Feature.MEDIA, Feature.VIDEO}))
    monkeypatch.setattr(svc_mod.youtube, "yt_dlp_available", lambda: True)
    assert device_actions(tv) == ["mirror", "youtube", "video"]
    monkeypatch.setattr(svc_mod.youtube, "yt_dlp_available", lambda: False)
    assert device_actions(tv) == ["mirror", "video"]


# ----- Datei -----

def test_play_file_serves_it_and_says_video(env):
    env._on_method_call(None, ":1.5", None, None, "PlayFile",
                        GLib.Variant("(ss)", ("cast:tv", env.video)), Invocation())
    url = next(iter(env.app.server.files))
    assert FakeReceiver.log == ["connect", ("media", url, "video/mp4", "Urlaub.mp4")]
    assert prop(env, "State") == "streaming"
    assert prop(env, "Source") == "video"
    assert env.app.inhibited == [svc_mod.t("service.inhibit_media", name="Wohnzimmer")]


def test_stopping_a_video_stops_the_tv_and_withdraws_the_file(env):
    env.play("cast:tv", "video", env.video)
    env.stop_streaming()
    assert FakeReceiver.log[-2:] == ["stop", "disconnect"]
    assert env.app.server.files == {}
    assert prop(env, "Source") == ""
    assert prop(env, "State") == "idle"


def test_a_tv_that_does_not_stop_still_gets_disconnected(env, capsys):
    env.play("cast:tv", "video", env.video)
    FakeReceiver.fail = "stop"
    env.stop_streaming()
    assert FakeReceiver.log[-1] == "disconnect"
    assert "weg" in capsys.readouterr().out


def test_a_failed_playback_withdraws_the_file(env):
    FakeReceiver.fail = "play"
    env.play("cast:tv", "video", env.video)
    assert env.app.server.files == {}
    assert "Fernseher sagt nein" in prop(env, "Error")
    assert prop(env, "State") == "idle"


def test_a_late_start_after_stop_withdraws_the_file(env):
    env.app.server.add_file(env.video)
    url = next(iter(env.app.server.files))
    env._media_started(object(), url)
    assert env.app.server.files == {}
    env._media_started(object(), None)


def test_bad_requests(env):
    with pytest.raises(LookupError):
        env.play("cast:weg", "video", env.video)
    with pytest.raises(ValueError):
        env.play("raop:box", "video", env.video)       # Lautsprecher
    with pytest.raises(ValueError):
        env.play("cast:tv", "video", "/nicht/da.mp4")
    with pytest.raises(ValueError):
        env.play("cast:tv", "link", "kein link")
    assert FakeReceiver.log == []


# ----- Links -----

@pytest.mark.parametrize(("youtube_app", "url", "expected"), [
    (True, "https://youtu.be/dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
    (True, "https://example.org/film.webm?x=1",
     ("media", "https://example.org/film.webm?x=1", "video/webm",
      "https://example.org/film.webm?x=1")),
])
def test_play_link(env, youtube_app, url, expected):
    FakeReceiver.youtube = youtube_app
    env._on_method_call(None, ":1.5", None, None, "PlayLink",
                        GLib.Variant("(ss)", ("cast:tv", url)), Invocation())
    assert FakeReceiver.log == ["connect", expected]
    assert prop(env, "Source") == "link"
    env.stop_streaming()
    assert FakeReceiver.log[-2:] == ["stop", "disconnect"]


def test_a_youtube_link_without_the_app_goes_through_yt_dlp(env, monkeypatch):
    FakeReceiver.youtube = False

    class Stream:
        url, mime, title = "https://cdn.example.org/v.mp4", "video/mp4", "Ein Film"

    monkeypatch.setattr(svc_mod.youtube, "resolve_stream", lambda url: Stream)
    env.play("cast:tv", "link", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert FakeReceiver.log[-1] == ("media", Stream.url, "video/mp4", "Ein Film")


# ----- Dialoge -----

class Invocation:
    def return_value(self, value) -> None:  # noqa: ANN001
        self.value = value

    def return_dbus_error(self, name, text) -> None:  # noqa: ANN001
        self.error = (name, text)


class FakeFile:
    def __init__(self, path) -> None:  # noqa: ANN001
        self.path = path

    def get_path(self):  # noqa: ANN201
        return self.path


class FakeFileDialog:
    last = None

    def __init__(self, title=None, filters=None) -> None:  # noqa: ANN001
        self.title, self.filters, self.folder = title, filters, None
        FakeFileDialog.last = self

    def set_initial_folder(self, folder) -> None:  # noqa: ANN001
        self.folder = folder

    def open(self, parent, cancellable, callback) -> None:  # noqa: ANN001
        self.callback = callback

    def open_finish(self, result):  # noqa: ANN001, ANN201
        if result is None:
            raise GLib.Error("abgebrochen")
        return FakeFile(result)


def test_choose_file_asks_then_plays(env, monkeypatch, tmp_path):
    monkeypatch.setattr(svc_mod.Gtk, "FileDialog", FakeFileDialog)
    monkeypatch.setattr(svc_mod.GLib, "get_user_special_dir", lambda kind: str(tmp_path))
    env._on_method_call(None, ":1.5", None, None, "ChooseFile",
                        GLib.Variant("(s)", ("cast:tv",)), Invocation())
    dialog = FakeFileDialog.last
    assert dialog.title == svc_mod.t("service.choose_video", name="Wohnzimmer")
    assert dialog.folder.get_path() == str(tmp_path)
    assert env.app.holds == 1                        # Dienst bleibt, solange gefragt wird
    dialog.callback(dialog, env.video)
    assert env.app.holds == 1                        # jetzt hält die Wiedergabe
    assert prop(env, "Source") == "video"


def test_choose_file_cancelled_or_gone(env, monkeypatch):
    monkeypatch.setattr(svc_mod.Gtk, "FileDialog", FakeFileDialog)
    monkeypatch.setattr(svc_mod.GLib, "get_user_special_dir", lambda kind: None)
    env.choose_file("cast:tv")
    FakeFileDialog.last.callback(FakeFileDialog.last, None)
    assert env.app.holds == 0 and FakeReceiver.log == []
    env.choose_file("cast:tv")
    del env._devices["cast:tv"]                      # Gerät verschwand beim Aussuchen
    FakeFileDialog.last.callback(FakeFileDialog.last, env.video)
    assert prop(env, "Error")
    with pytest.raises(ValueError):
        env.choose_file("raop:box")


class FakeEntry:
    last = None

    def __init__(self, **props) -> None:  # noqa: ANN003
        self.text = ""
        self.handlers = {}
        FakeEntry.last = self

    def connect(self, signal, handler) -> None:  # noqa: ANN001
        self.handlers[signal] = handler

    def get_text(self) -> str:
        return self.text

    def set_text(self, text) -> None:  # noqa: ANN001
        self.text = text


class FakeAlert:
    last = None

    def __init__(self, heading=None, body=None, extra_child=None) -> None:  # noqa: ANN001
        self.heading, self.body, self.child = heading, body, extra_child
        self.responses = []
        FakeAlert.last = self

    def add_response(self, rid, label) -> None:  # noqa: ANN001
        self.responses.append(rid)

    def set_response_appearance(self, rid, appearance) -> None:  # noqa: ANN001
        pass

    def set_default_response(self, rid) -> None:  # noqa: ANN001
        pass

    def set_close_response(self, rid) -> None:  # noqa: ANN001
        pass

    def connect(self, signal, handler) -> None:  # noqa: ANN001
        self.answer = handler

    def present(self, parent) -> None:  # noqa: ANN001
        self.presented = True


class FakeClipboard:
    text: str | None = "https://youtu.be/dQw4w9WgXcQ"

    def read_text_async(self, cancellable, callback) -> None:  # noqa: ANN001
        callback(self, None)

    def read_text_finish(self, result):  # noqa: ANN001, ANN201
        if FakeClipboard.text is None:
            raise GLib.Error("leer")
        return FakeClipboard.text


class FakeDisplay:
    @staticmethod
    def get_default():  # noqa: ANN205
        return FakeDisplay()

    def get_clipboard(self):  # noqa: ANN201
        return FakeClipboard()


@pytest.fixture
def dialogs(monkeypatch):
    monkeypatch.setattr(svc_mod.Gtk, "Entry", FakeEntry)
    monkeypatch.setattr(svc_mod.Adw, "AlertDialog", FakeAlert)
    monkeypatch.setattr(svc_mod.Gdk, "Display", FakeDisplay)
    FakeClipboard.text = "https://youtu.be/dQw4w9WgXcQ"


def test_choose_link_takes_the_link_from_the_clipboard(env, dialogs):
    env._on_method_call(None, ":1.5", None, None, "ChooseLink",
                        GLib.Variant("(s)", ("cast:tv",)), Invocation())
    alert, entry = FakeAlert.last, FakeEntry.last
    assert alert.presented and alert.responses == ["cancel", "play"]
    assert "Wohnzimmer" in alert.body
    entry.handlers["map"](entry)                     # Fenster sichtbar -> Zwischenablage
    assert entry.text == "https://youtu.be/dQw4w9WgXcQ"
    alert.answer(alert, "play")
    assert FakeReceiver.log == ["connect", ("youtube", "dQw4w9WgXcQ")]
    assert env.app.holds == 1                        # nur noch die Wiedergabe


def test_choose_link_keeps_typed_text_and_ignores_other_clipboards(env, dialogs):
    env.choose_link("cast:tv")
    entry = FakeEntry.last
    entry.text = "https://example.org/a.mp4"
    entry.handlers["map"](entry)
    assert entry.text == "https://example.org/a.mp4"   # Getipptes bleibt
    entry.text = ""
    FakeClipboard.text = "Einkaufsliste"
    entry.handlers["map"](entry)
    assert entry.text == ""
    FakeClipboard.text = None
    entry.handlers["map"](entry)
    assert entry.text == ""


def test_choose_link_cancelled_or_wrong(env, dialogs, monkeypatch):
    env.choose_link("cast:tv")
    FakeAlert.last.answer(FakeAlert.last, "cancel")
    assert FakeReceiver.log == [] and env.app.holds == 0
    env.choose_link("cast:tv")
    FakeEntry.last.text = "kein link"
    FakeAlert.last.answer(FakeAlert.last, "play")
    assert prop(env, "Error") == svc_mod.t("service.no_link")
    monkeypatch.setattr(FakeDisplay, "get_default", staticmethod(lambda: None))
    env.choose_link("cast:tv")
    FakeEntry.last.handlers["map"](FakeEntry.last)   # ohne Display: kein Absturz
    with pytest.raises(ValueError):
        env.choose_link("raop:box")


def test_the_active_device_stays_playable_when_the_search_lost_it(env):
    env.play("cast:tv", "video", env.video)
    del env._devices["cast:tv"]
    env.play("cast:tv", "link", "https://youtu.be/dQw4w9WgXcQ")
    assert FakeReceiver.log[-1] == ("youtube", "dQw4w9WgXcQ")


# ----- nur Video- und Audiodateien ins LAN -----

def test_play_file_shares_only_media(env, tmp_path):
    """PlayFile gibt die Datei im LAN frei – ein Schlüssel oder Dokument nie."""
    secret = tmp_path / "id_ed25519"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    notes = tmp_path / "notizen.txt"
    notes.write_text("privat")
    for path in (secret, notes):
        with pytest.raises(ValueError):
            env.play("cast:tv", "video", str(path))
    assert FakeReceiver.log == []
    assert env.app.server.files == {}


def test_media_by_extension_or_content(tmp_path):
    from app.service import is_media_file

    song = tmp_path / "Lied.flac"
    song.write_bytes(b"\0")
    assert is_media_file(str(song))
    # Ohne Endung entscheidet der Inhalt (MP4: „ftyp“ im ersten Kasten)
    clip = tmp_path / "clip"
    clip.write_bytes(b"\0\0\0\x18ftypisom\0\0\0\0isomiso2" + b"\0" * 64)
    assert is_media_file(str(clip))
    assert not is_media_file(str(tmp_path / "fehlt"))
