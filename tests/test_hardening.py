"""Was aus dem Netz kommt, bleibt Daten: Gerätenamen, SSDP-Adressen, Antworten.

Jeder im LAN kann sich per mDNS oder SSDP als Fernseher oder Lautsprecher
melden. Diese Tests prüfen, dass solche Angaben weder Konfigurationen
umschreiben noch Schwupp an fremde Adressen schicken oder den Speicher füllen.
"""
from __future__ import annotations

import http.server
import socket
import stat
import threading
import time

import pytest

from app import discovery, dlna, net
from app.config import Config
from app.server import httpserver
from app.speaker.pipewire import NullSink, RaopSink, _quote, safe_label

EVIL = 'Box"\n}]\ncontext.exec = [ { path = /bin/sh args = [ -c $x ] } ]\n# \\'


# ----- Gerätenamen in PipeWire -----

def test_safe_label_keeps_a_readable_name():
    assert safe_label("Küche +1") == "Küche +1"
    assert safe_label("Bad & Wohnzimmer") == "Bad & Wohnzimmer"


def test_safe_label_drops_what_ends_a_line_or_string():
    label = safe_label(EVIL)
    for bad in ("\n", "\r", '"', "'", "\\", "$"):
        assert bad not in label
    assert safe_label("a b\tc\0d") == "a b c d"
    assert len(safe_label("x" * 500)) == 80


def test_raop_config_cannot_be_extended_by_a_name():
    benign = RaopSink("192.0.2.5", 7000, "Schwupp → Box")._config()
    evil = RaopSink("192.0.2.5", 7000, f"Schwupp → {EVIL}")._config()
    # Gleich viele Zeilen: Der Name bleibt im Kommentar und im String
    assert evil.count("\n") == benign.count("\n")
    assert not any(line.lstrip().startswith("context.exec") for line in evil.splitlines())
    desc = next(line for line in evil.splitlines() if "node.description" in line)
    assert desc.count('"') == 2 and "\\" not in desc


@pytest.mark.parametrize(("host", "port"), [
    ('192.0.2.5" }', 7000), ("lautsprecher.local", 7000), ("192.0.2.5", 0),
    ("192.0.2.5", 70000), ("192.0.2.5", "7000"), ("192.0.2.5", True),
])
def test_raop_sink_takes_only_an_address_and_a_port(host, port):
    with pytest.raises(ValueError):
        RaopSink(host, port, "Box")


def test_null_sink_description_stays_one_value():
    quoted = _quote("O'Brien \"Box\" \\")
    assert quoted == "'OBrien Box'"
    assert NullSink(EVIL).description == EVIL      # erst beim Einsetzen bereinigt


# ----- SSDP-Adressen -----

@pytest.mark.parametrize(("loc", "ok"), [
    ("http://192.0.2.7:8080/dd.xml", True),
    ("https://192.0.2.7/dd.xml", True),
    ("file:///dev/zero", False),
    ("http://127.0.0.1:631/", False),
    ("http://192.0.2.8/dd.xml", False),
    ("ftp://192.0.2.7/dd.xml", False),
    ("http://[::1/", False),
    ("192.0.2.7/dd.xml", False),
])
def test_location_must_be_http_on_the_answering_device(loc, ok):
    assert net.location_of(loc, "192.0.2.7") is ok


class FakeUdp:
    """Socket mit vorgegebenen SSDP-Antworten."""

    answers: list[tuple[bytes, tuple[str, int]]] = []

    def __init__(self, *_args) -> None:
        self._answers = list(self.answers)

    def setsockopt(self, *_args) -> None: ...
    def settimeout(self, *_args) -> None: ...
    def sendto(self, *_args) -> None: ...
    def close(self) -> None: ...

    def recvfrom(self, _size):
        if not self._answers:
            raise TimeoutError
        return self._answers.pop(0)


def ssdp(location: str) -> bytes:
    return f"HTTP/1.1 200 OK\r\nST: x\r\nLOCATION: {location}\r\n\r\n".encode()


@pytest.fixture
def fake_udp(monkeypatch):
    monkeypatch.setattr(discovery.socket, "socket", FakeUdp)
    monkeypatch.setattr(dlna.socket, "socket", FakeUdp)
    FakeUdp.answers = [
        (ssdp("file:///dev/zero"), ("192.0.2.7", 1900)),
        (ssdp("http://127.0.0.1:631/printers"), ("192.0.2.8", 1900)),
        (ssdp("http://192.0.2.9:1400/xml/device_description.xml"), ("192.0.2.9", 1900)),
        (b"HTTP/1.1 200 OK\r\n\r\n", ("192.0.2.10", 1900)),
    ]


def test_ssdp_search_keeps_only_descriptions_on_the_device(fake_udp):
    assert discovery._ssdp_search("ssdp:all", timeout=1) == {
        "192.0.2.9": "http://192.0.2.9:1400/xml/device_description.xml"}


def test_renderer_lookup_keeps_only_descriptions_on_the_device(fake_udp):
    assert dlna._ssdp_locations("192.0.2.7", "ssdp:all", timeout=1) == []
    assert dlna._ssdp_locations("192.0.2.9", "ssdp:all", timeout=1) == [
        "http://192.0.2.9:1400/xml/device_description.xml"]


def test_description_without_http_base_is_ignored(monkeypatch):
    monkeypatch.setattr(net, "http_read", lambda *a, **k: b"<root/>")
    assert dlna.DlnaRenderer("192.0.2.7")._services_from_description("192.0.2.7/x") == {}


# ----- Antworten der Geräte -----

class Device(http.server.BaseHTTPRequestHandler):
    """Ein Gerät im LAN: /big sendet zu viel, /moved leitet um, /ok antwortet."""

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"x" * (net.MAX_REPLY + 10) if self.path == "/big" else b"<root/>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def device():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Device)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_http_read_reads_a_reply(device, monkeypatch):
    # Ein Proxy aus der Umgebung gilt nicht: Die Geräte stehen im LAN
    monkeypatch.setenv("http_proxy", "http://192.0.2.250:3128")
    assert net.http_read(f"{device}/ok", timeout=5) == b"<root/>"


def test_http_read_stops_at_the_limit(device):
    with pytest.raises(OSError):
        net.http_read(f"{device}/big", timeout=5)
    assert net.http_read(f"{device}/big", timeout=5, limit=net.MAX_REPLY + 10)


def test_http_read_does_not_follow_redirects(device):
    with pytest.raises(OSError):
        net.http_read(f"{device}/moved", timeout=5)


@pytest.mark.parametrize("url", ["file:///etc/hostname", "ftp://192.0.2.7/", "data:,x"])
def test_http_read_only_speaks_http(url):
    with pytest.raises(OSError):
        net.http_read(url, timeout=1)


# ----- Config nur für den Nutzer -----

def test_config_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "schwupp" / "config.json"
    path.parent.mkdir()
    path.write_text("{}")
    path.chmod(0o644)                     # wie von älteren Versionen geschrieben
    cfg = Config()
    cfg["webos_keys"] = {"192.0.2.33": "geheim"}
    cfg.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["config.json"]


def test_config_save_survives_parallel_writers(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = Config()
    errors: list[BaseException] = []

    def write() -> None:
        try:
            for _ in range(20):
                cfg.save()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    assert [p.name for p in (tmp_path / "schwupp").iterdir()] == ["config.json"]


def test_config_save_leaves_no_temp_file_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = Config()
    cfg["kaputt"] = object()              # nicht als JSON darstellbar
    with pytest.raises(TypeError):
        cfg.save()
    assert list((tmp_path / "schwupp").iterdir()) == []


# ----- Medienserver -----

def test_silent_client_is_dropped(monkeypatch):
    monkeypatch.setattr(httpserver, "REQUEST_TIMEOUT_S", 0.3)
    server = httpserver.MediaServer()
    server.start()
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            start = time.monotonic()
            assert sock.recv(1) == b""        # Server schließt, ohne zu antworten
            assert time.monotonic() - start < 3
    finally:
        server.stop()


def test_file_is_served_after_a_slow_start(tmp_path, monkeypatch):
    """Das Zeitlimit gilt der Anfrage, nicht dem Ausliefern."""
    monkeypatch.setattr(httpserver, "REQUEST_TIMEOUT_S", 0.5)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v" * 1000)
    server = httpserver.MediaServer()
    server.start()
    try:
        url = server.add_file(str(video), client_host="127.0.0.1")
        path = url.split(str(server.port), 1)[1]
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            sock.sendall(f"GET {path} HTTP/1.1\r\n".encode())
            time.sleep(0.2)
            sock.sendall(b"Host: x\r\n\r\n")
            data = b""
            while not data.endswith(b"v" * 1000):
                chunk = sock.recv(4096)
                assert chunk
                data += chunk
        assert data.startswith(b"HTTP/1.1 200")
    finally:
        server.stop()
