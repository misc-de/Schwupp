"""Lokaler Medien-Server: Range-Auslieferung, Token-Lebensdauer, Pfadschutz.

Der Server läuft im LAN und liefert Dateien aus dem Benutzerverzeichnis aus –
die Zugriffsregeln sind daher sicherheitsrelevant und werden hier gegen einen
echten laufenden Server geprüft (kein Mock).
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

import pytest

from app.server.httpserver import MediaServer, _token_of


@pytest.fixture
def server():
    srv = MediaServer(port=0)
    srv.start()
    yield srv
    srv.stop()


def _get(server, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}",
                                 headers=headers or {})
    return urllib.request.urlopen(req, timeout=5)


def _path_of(url: str) -> str:
    return "/" + url.split("/", 3)[3]


@pytest.fixture
def media(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(bytes(range(256)) * 4)      # 1024 Byte
    return f


def test_serves_a_registered_file(server, media):
    url = server.add_file(str(media))
    resp = _get(server, _path_of(url))
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "video/mp4"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.read() == media.read_bytes()


def test_range_request_returns_206_and_the_right_slice(server, media):
    url = server.add_file(str(media))
    resp = _get(server, _path_of(url), {"Range": "bytes=10-19"})
    assert resp.status == 206
    assert resp.headers["Content-Range"] == "bytes 10-19/1024"
    assert resp.headers["Content-Length"] == "10"
    assert resp.read() == media.read_bytes()[10:20]


def test_open_ended_range(server, media):
    url = server.add_file(str(media))
    resp = _get(server, _path_of(url), {"Range": "bytes=1000-"})
    assert resp.status == 206
    assert resp.read() == media.read_bytes()[1000:]


def test_unsatisfiable_range_is_416(server, media):
    url = server.add_file(str(media))
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, _path_of(url), {"Range": "bytes=5000-6000"})
    assert exc.value.code == 416


def test_unknown_token_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/file/doesnotexist.mp4")
    assert exc.value.code == 404


def test_removed_file_is_no_longer_reachable(server, media):
    """Ohne remove_file bliebe jede gecastete Datei bis zum App-Ende abrufbar."""
    url = server.add_file(str(media))
    _get(server, _path_of(url)).read()
    server.remove_file(url)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, _path_of(url))
    assert exc.value.code == 404


def test_each_file_gets_its_own_token(server, media):
    a, b = server.add_file(str(media)), server.add_file(str(media))
    assert _token_of(a) != _token_of(b)
    assert len(_token_of(a)) >= 16          # secrets.token_urlsafe(12)


def test_cors_headers_are_present(server, media):
    """Ohne CORS lehnt der Cast-Receiver HLS-Segmente ab (docs/MIRRORING.md)."""
    resp = _get(server, _path_of(server.add_file(str(media))))
    assert resp.headers["Access-Control-Allow-Origin"] == "*"


# -- HLS --------------------------------------------------------------------

def test_hls_directory_is_served_under_its_token(server, tmp_path):
    hls = tmp_path / "hls"
    hls.mkdir()
    (hls / "playlist.m3u8").write_text("#EXTM3U\n")
    base = server.add_hls_dir(str(hls))
    resp = _get(server, _path_of(base) + "playlist.m3u8")
    assert resp.status == 200
    assert resp.read() == b"#EXTM3U\n"


def test_hls_without_token_is_not_served(server, tmp_path):
    hls = tmp_path / "hls"
    hls.mkdir()
    (hls / "playlist.m3u8").write_text("#EXTM3U\n")
    server.add_hls_dir(str(hls))
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/hls/playlist.m3u8")
    assert exc.value.code == 404


@pytest.mark.parametrize("attack", [
    "../../../etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "subdir/secret.txt",
])
def test_hls_path_traversal_is_blocked(server, tmp_path, attack):
    hls = tmp_path / "hls"
    (hls / "subdir").mkdir(parents=True)
    (hls / "subdir" / "secret.txt").write_text("secret")
    base = server.add_hls_dir(str(hls))
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, _path_of(base) + attack)
    assert exc.value.code == 404


def test_removed_hls_dir_is_gone(server, tmp_path):
    hls = tmp_path / "hls"
    hls.mkdir()
    (hls / "playlist.m3u8").write_text("#EXTM3U\n")
    base = server.add_hls_dir(str(hls))
    server.remove_hls_dir(base)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, _path_of(base) + "playlist.m3u8")
    assert exc.value.code == 404


# -- Live-Stream -------------------------------------------------------------

def test_live_stream_delivers_written_chunks(server):
    url, live = server.add_live()
    resp = _get(server, _path_of(url))
    assert resp.headers["Content-Type"] == "video/mp2t"
    # Der Handler registriert sich erst nach dem Senden der Header als Abnehmer.
    deadline = time.monotonic() + 5
    while live.consumer_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert live.consumer_count == 1
    live.write(b"\x47" + bytes(187))
    assert resp.read(188) == b"\x47" + bytes(187)
    live.close()
    resp.close()


def test_live_stream_drops_old_data_instead_of_blocking(server):
    """Ein langsamer Client darf die Encoder-Pipeline nie ausbremsen."""
    _url, live = server.add_live()
    q = live.add_consumer()
    for i in range(600):                      # mehr als die Queue fasst (512)
        live.write(bytes([i % 256]))
    assert q.qsize() <= 512


def test_base_url_uses_the_route_towards_the_device(server, monkeypatch):
    """Die URL muss auf dem Interface liegen, über das das Gerät uns erreicht."""
    monkeypatch.setattr("app.server.httpserver.lan_ip",
                        lambda target="8.8.8.8": {"192.168.5.7": "192.168.5.2"}.get(
                            target, "10.0.0.1"))
    assert server.base_url("192.168.5.7") == f"http://192.168.5.2:{server.port}"
    assert server.base_url() == f"http://10.0.0.1:{server.port}"


def test_stop_closes_live_streams_and_forgets_tokens(tmp_path, media):
    srv = MediaServer(port=0)
    srv.start()
    url = srv.add_file(str(media))
    _url, live = srv.add_live()
    q = live.add_consumer()
    srv.stop()
    assert q.get_nowait() is None             # Sentinel -> Handler beendet sich
    assert srv._lookup_file(_token_of(url)) is None
