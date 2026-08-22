"""Schlanker HTTP-Server für lokale Medien, HLS und Live-Streams.

Ein Cast-/DLNA-/AirPlay-Gerät spielt nur Inhalte ab, die es per HTTP von einer
im LAN erreichbaren Adresse laden kann. Dieser Server:

* registriert einzelne lokale Dateien unter ``/file/<token>`` (mit Range-Support,
  damit das Gerät in MP4-Dateien spulen kann),
* liefert HLS-Verzeichnisse unter ``/hls/<token>/<datei>`` aus (Playlist +
  Segmente der HLS-Mirror-Engine) und
* verteilt endlose Byte-Ströme unter ``/live/<token>`` (MPEG-TS-Mirroring).

Jede Freigabe hat ein eigenes, zufälliges Token und wird wieder **entfernt**,
sobald sie nicht mehr gebraucht wird – ohne das blieb jede jemals gecastete
Datei für die restliche Laufzeit der App im LAN abrufbar.

Die Basis-URL wird pro Gerät bestimmt (:func:`app.net.lan_ip` mit dessen IP als
Routing-Ziel): Bei mehreren Interfaces (WLAN, VPN, Docker-Bridges) oder nach
einem Netzwechsel zeigt sie sonst auf eine Adresse, die das Gerät nicht erreicht.

Läuft in einem eigenen Thread; threadsicher gegenüber dem GTK-Hauptthread.
"""
from __future__ import annotations

import contextlib
import mimetypes
import os
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..net import lan_ip

mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("video/fmp4", ".m4s")

_CHUNK = 64 * 1024


def _guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _token_of(url_or_token: str) -> str:
    """Extrahiert das Token aus einer vollständigen URL (oder reicht es durch)."""
    token = url_or_token.rstrip("/").rsplit("/", 1)[-1]
    return token.split(".", 1)[0]


class LiveStream:
    """Broadcast eines endlosen Byte-Stroms (z. B. MPEG-TS) an HTTP-Clients.

    Ein Producer (GStreamer-Pipeline) ruft :meth:`write`; jeder verbundene
    HTTP-Client bekommt seine eigene Queue. Langsame Clients verlieren die
    ältesten Blöcke (Drop statt Blockieren), damit der Producer nie hängt.
    """

    def __init__(self) -> None:
        self._consumers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            consumers = list(self._consumers)
        for q in consumers:
            try:
                q.put_nowait(data)
            except queue.Full:
                with contextlib.suppress(queue.Empty, queue.Full):
                    q.get_nowait()          # ältesten Block verwerfen
                    q.put_nowait(data)

    @property
    def consumer_count(self) -> int:
        with self._lock:
            return len(self._consumers)

    def add_consumer(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=512)
        with self._lock:
            self._consumers.append(q)
        return q

    def remove_consumer(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._consumers:
                self._consumers.remove(q)

    def close(self) -> None:
        with self._lock:
            consumers = list(self._consumers)
            self._consumers.clear()
        for q in consumers:
            with contextlib.suppress(queue.Full):
                q.put_nowait(None)          # Sentinel -> Handler beendet


class MediaServer:
    def __init__(self, port: int = 0) -> None:
        self._files: dict[str, tuple[str, str]] = {}   # token -> (pfad, mime)
        self._hls: dict[str, Path] = {}                # token -> Verzeichnis
        self._live: dict[str, LiveStream] = {}         # token -> Live-Stream
        self._lock = threading.Lock()

        handler = self._make_handler()
        # bind an "" (alle Interfaces), damit das Gerät uns über die LAN-IP
        # erreicht – welche das ist, hängt vom Gerät ab (siehe base_url).
        self._httpd = ThreadingHTTPServer(("", port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True,
                                        name="media-server")

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def base_url(self, client_host: str | None = None) -> str:
        """Basis-URL, unter der *client_host* diesen Server erreicht.

        Ohne Argument wird die Route Richtung Internet genommen – das ist nur
        eine Notlösung für Aufrufer ohne bekannte Gegenstelle.
        """
        ip = lan_ip(client_host) if client_host else lan_ip()
        return f"http://{ip}:{self.port}"

    # -- Registrierung --------------------------------------------------------
    def add_file(self, path: str, mime: str | None = None, *,
                 client_host: str | None = None) -> str:
        """Macht eine lokale Datei abrufbar und liefert ihre vollständige URL."""
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._files[token] = (os.fspath(path), mime or _guess_mime(path))
        ext = Path(path).suffix
        return f"{self.base_url(client_host)}/file/{token}{ext}"

    def remove_file(self, url_or_token: str) -> None:
        """Nimmt eine Datei-Freigabe zurück (nach Ende der Wiedergabe)."""
        with self._lock:
            self._files.pop(_token_of(url_or_token), None)

    def add_hls_dir(self, directory: str, *, client_host: str | None = None) -> str:
        """Gibt ein HLS-Verzeichnis frei und liefert dessen Basis-URL (mit ``/``)."""
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._hls[token] = Path(directory).resolve()
        return f"{self.base_url(client_host)}/hls/{token}/"

    def remove_hls_dir(self, url_or_token: str) -> None:
        token = url_or_token.rstrip("/").rsplit("/", 1)[-1]
        with self._lock:
            self._hls.pop(token, None)

    def add_live(self, mime: str = "video/mp2t", *,
                 client_host: str | None = None) -> tuple[str, LiveStream]:
        """Registriert einen Live-Stream und liefert (URL, LiveStream)."""
        token = secrets.token_urlsafe(12)
        ls = LiveStream()
        with self._lock:
            self._live[token] = ls
        return f"{self.base_url(client_host)}/live/{token}", ls

    def remove_live(self, url_or_token: str) -> None:
        with self._lock:
            ls = self._live.pop(_token_of(url_or_token), None)
        if ls is not None:
            ls.close()

    # -- Lebenszyklus ---------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            streams = list(self._live.values())
            self._live.clear()
            self._files.clear()
            self._hls.clear()
        for ls in streams:
            ls.close()
        self._httpd.shutdown()
        self._httpd.server_close()

    # -- Nachschlagen (vom Handler genutzt) -----------------------------------
    def _lookup_file(self, token: str) -> tuple[str, str] | None:
        with self._lock:
            return self._files.get(token)

    def _lookup_hls(self, token: str, name: str) -> tuple[str, str] | None:
        with self._lock:
            base = self._hls.get(token)
        if base is None:
            return None
        target = (base / os.path.basename(name)).resolve()
        # Pfad-Traversal verhindern: nur Dateien direkt im freigegebenen Ordner.
        if target.parent != base or not target.is_file():
            return None
        return str(target), _guess_mime(str(target))

    def _lookup_live(self, token: str) -> LiveStream | None:
        with self._lock:
            return self._live.get(token)

    # -- Handler --------------------------------------------------------------
    def _make_handler(self):  # noqa: C901 – zählt die eingebettete Handler-Klasse mit
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # noqa: ANN002
                pass  # nicht auf stderr spammen

            # -- Routing ---------------------------------------------------
            def _route(self) -> tuple[str, str]:
                """(Bereich, Rest) aus dem Pfad, z. B. ("file", "<token>.mp4")."""
                path = self.path.split("?", 1)[0].lstrip("/")
                head, _, rest = path.partition("/")
                return head, rest

            def do_HEAD(self) -> None:
                self._dispatch(head_only=True)

            def do_GET(self) -> None:
                self._dispatch(head_only=False)

            def do_OPTIONS(self) -> None:
                # CORS-Preflight (Cast-Receiver laden HLS-Segmente per XHR)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def end_headers(self) -> None:
                # CORS für ALLE Antworten – sonst lehnt der Cast-Receiver HLS ab
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                super().end_headers()

            def _dispatch(self, head_only: bool) -> None:
                area, rest = self._route()
                if area == "live":
                    self._serve_live(rest, head_only)
                    return
                entry = None
                if area == "file":
                    entry = server._lookup_file(rest.split(".", 1)[0])
                elif area == "hls":
                    token, _, name = rest.partition("/")
                    entry = server._lookup_hls(token, name) if name else None
                if entry is None:
                    self.send_error(404)
                    return
                self._serve_file(entry, head_only)

            # -- Live-Stream (endloser MPEG-TS) -------------------------------
            def _serve_live(self, token: str, head_only: bool) -> None:
                ls = server._lookup_live(token)
                if ls is None:
                    self.send_error(404)
                    return
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Accept-Ranges", "none")
                self.send_header("Connection", "close")  # Streamende = Verbindungsende
                self.end_headers()
                if head_only:
                    return
                q = ls.add_consumer()
                try:
                    while True:
                        data = q.get()
                        if data is None:        # Sentinel
                            break
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    ls.remove_consumer(q)

            # -- Datei (mit Range) --------------------------------------------
            def _parse_range(self, size: int) -> tuple[int, int, bool] | None:
                """(start, end, partial) oder None bei unerfüllbarem Range."""
                rng = self.headers.get("Range")
                if not rng or not rng.startswith("bytes="):
                    return 0, size - 1, False
                spec = rng[len("bytes="):].split(",")[0]
                s, _, e = spec.partition("-")
                try:
                    start = int(s) if s.strip() else 0
                    end = min(int(e), size - 1) if e.strip() else size - 1
                except ValueError:
                    return None
                if start > end or start >= size:
                    return None
                return start, end, True

            def _serve_file(self, entry: tuple[str, str], head_only: bool) -> None:
                fpath, mime = entry
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    self.send_error(404)
                    return

                parsed = self._parse_range(size)
                if parsed is None:
                    self.send_error(416)
                    return
                start, end, partial = parsed

                length = end - start + 1
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", mime)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()

                if head_only:
                    return
                with open(fpath, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(_CHUNK, remaining))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        remaining -= len(chunk)

        return Handler
