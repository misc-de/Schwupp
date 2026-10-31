"""Ton an Lautsprecher: Einordnung der Geräte, Sonos-Besonderheiten, Live-MP3."""
from __future__ import annotations

import urllib.request
from dataclasses import dataclass

import pytest

from app.discovery import ReceiverInfo, SonosInfo, airplay_has_video, parse_zone_groups
from app.receivers.base import Context
from app.receivers.sonos import SonosReceiver
from app.server.httpserver import MediaServer
from app.speaker import is_speaker

ZONES = (
    '<ZoneGroups>'
    '<ZoneGroup Coordinator="RINCON_A" ID="A:1">'
    '<ZoneGroupMember UUID="RINCON_A" ZoneName="Küche"/>'
    '<ZoneGroupMember UUID="RINCON_B" ZoneName="Bad"/>'
    '<ZoneGroupMember UUID="RINCON_SUB" ZoneName="Küche" Invisible="1"/>'
    '</ZoneGroup>'
    '<ZoneGroup Coordinator="RINCON_C" ID="C:1">'
    '<ZoneGroupMember UUID="RINCON_C" ZoneName="Wohnzimmer"></ZoneGroupMember>'
    '</ZoneGroup>'
    '</ZoneGroups>'
)


def test_zone_groups_count_rooms_without_invisible_members():
    assert parse_zone_groups(ZONES) == {"RINCON_A": 2, "RINCON_C": 1}


@pytest.mark.parametrize("features,video", [
    ("0x5A7FFFF7,0x1E", True),     # Apple TV: Video und Bildschirm
    ("0x445F8A00,0x1C340", False),  # HomePod: nur Ton
    ("0x80", True),                 # nur Bildschirm
    ("", True),                     # ohne Angabe wie bisher ein Fernseher
    ("kaputt", True),
])
def test_airplay_video_bits(features, video):
    assert airplay_has_video(features) is video


@dataclass
class FakeCastInfo:
    cast_type: str


def _info(kind, raw=None):
    return ReceiverInfo(kind=kind, uuid=f"{kind}:h", name="Gerät", host="192.0.2.5",
                        port=0, model="", raw=raw)


@pytest.mark.parametrize("info,speaker", [
    (_info("raop"), True),
    (_info("sonos"), True),
    (_info("dlna"), True),
    (_info("chromecast", FakeCastInfo("audio")), True),
    (_info("chromecast", FakeCastInfo("group")), True),
    (_info("chromecast", FakeCastInfo("cast")), False),
    (_info("chromecast"), False),   # versteckter Cast-Receiver (LG-TV)
    (_info("airplay"), False),
])
def test_speakers_and_tvs(info, speaker):
    assert is_speaker(info) is speaker


def test_sonos_plays_a_live_mp3_under_its_radio_scheme(monkeypatch):
    sent = {}
    sonos = SonosReceiver(_info("sonos", SonosInfo(
        avtransport="http://192.0.2.5:1400/MediaRenderer/AVTransport/Control",
        rendering=None, group_rendering=None, topology=None, udn="RINCON_A")), Context())

    def play_url(url, mime, title, *, live=False, uri=None):
        sent.update(url=url, uri=uri, live=live)

    monkeypatch.setattr(sonos._dlna, "play_url", play_url)
    sonos.play_live_audio("http://192.0.2.1:8000/live/abc.mp3", "Ton")
    assert sent["uri"] == "x-rincon-mp3radio://192.0.2.1:8000/live/abc.mp3"
    assert sent["url"].startswith("http://")      # die Metadaten nennen die echte URL
    assert sent["live"] is True


def test_live_mp3_is_served_with_its_type_and_dlna_headers():
    server = MediaServer()
    server.start()
    try:
        url, live = server.add_live("audio/mpeg", client_host="127.0.0.1")
        assert url.endswith(".mp3")
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.headers["Content-Type"] == "audio/mpeg"
            assert resp.headers["transferMode.dlna.org"] == "Streaming"
        assert live.mime == "audio/mpeg"
    finally:
        server.stop()


def test_renderer_without_service_list_gets_the_usual_paths(monkeypatch):
    """Frontier-Silicon-Radios lassen die Dienste aus der Beschreibung weg."""
    from app import discovery

    xml = ("<root><device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
           "<friendlyName>Classic Stereo</friendlyName><modelName>ClassicStereo</modelName>"
           "</device></root>")

    class Reply:
        def read(self):
            return xml.encode()

    monkeypatch.setattr(discovery.urllib.request, "urlopen", lambda *a, **k: Reply())
    monkeypatch.setattr(discovery, "guess_controls",
                        lambda base: (f"{base}/AVTransport/control",
                                      f"{base}/RenderingControl/control"))
    name, model, raw = discovery._fetch_renderer("http://192.0.2.67:8080/dd.xml")
    assert name == "Classic Stereo"
    assert raw.avtransport == "http://192.0.2.67:8080/AVTransport/control"
    assert raw.rendering == "http://192.0.2.67:8080/RenderingControl/control"
