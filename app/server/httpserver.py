"""Schlanker HTTP-Server für lokale Medien und HLS.

Ein Chromecast spielt nur Inhalte ab, die er per HTTP von einer im LAN
erreichbaren Adresse laden kann. Dieser Server:

* registriert einzelne lokale Dateien unter ``/file/<token>`` (mit Range-Support,
  damit das Gerät in MP4-Dateien spulen kann), und
* liefert ein Verzeichnis unter ``/hls/<datei>`` aus (für die HLS-Mirror-Engine:
  ``.m3u8``-Playlist + ``.ts``-Segmente).

Läuft in einem eigenen Thread; threadsicher gegenüber dem GTK-Hauptthread.
"""
from __future__ import annotations

import mimetypes
import os
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("video/fmp4", ".m4s")


def _guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


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
                try:
                    q.get_nowait()          # ältesten Block verwerfen
                    q.put_nowait(data)
                except (queue.Empty, queue.Full):
                    pass

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
            try:
                q.put_nowait(None)          # Sentinel -> Handler beendet
            except queue.Full:
                pass


class MediaServer:
    def __init__(self, bind_ip: str, port: int = 0) -> None:
        self._bind_ip = bind_ip
        self._files: dict[str, tuple[str, str]] = {}  # token -> (pfad, mime)
        self._hls_dir: Optional[Path] = None
        self._live: dict[str, LiveStream] = {}        # token -> Live-Stream
        self._lock = threading.Lock()

        handler = self._make_handler()
        # bind an "" (alle Interfaces), damit das Gerät uns über die LAN-IP erreicht
        self._httpd = ThreadingHTTPServer(("", port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self._bind_ip}:{self.port}"

    # -- Registrierung --------------------------------------------------------
    def add_file(self, path: str, mime: str | None = None) -> str:
        """Macht eine lokale Datei abrufbar und liefert ihre vollständige URL."""
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._files[token] = (os.fspath(path), mime or _guess_mime(path))
        ext = Path(path).suffix
        return f"{self.base_url}/file/{token}{ext}"

    def set_hls_dir(self, directory: str) -> str:
        """Setzt das Verzeichnis, aus dem ``/hls/<datei>`` ausgeliefert wird."""
        with self._lock:
            self._hls_dir = Path(directory)
        return f"{self.base_url}/hls/"

    def add_live(self, mime: str = "video/mp2t") -> tuple[str, LiveStream]:
        """Registriert einen Live-Stream und liefert (URL, LiveStream)."""
        token = secrets.token_urlsafe(12)
        ls = LiveStream()
        with self._lock:
            self._live[token] = ls
        return f"{self.base_url}/live/{token}", ls

    def remove_live(self, url_or_token: str) -> None:
        token = url_or_token.rsplit("/", 1)[-1]
        with self._lock:
            ls = self._live.pop(token, None)
        if ls is not None:
            ls.close()

    # -- Lebenszyklus ---------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    # -- Handler --------------------------------------------------------------
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # noqa: ANN002
                pass  # nicht auf stderr spammen

            def do_HEAD(self) -> None:
                if self.path.split("?", 1)[0].startswith("/live/"):
                    self._serve_live(head_only=True)
                else:
                    self._serve(head_only=True)

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0].startswith("/live/"):
                    self._serve_live(head_only=False)
                else:
                    self._serve(head_only=False)

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

            # -- Live-Stream (endloser MPEG-TS) -------------------------------
            def _serve_live(self, head_only: bool) -> None:
                token = self.path.split("?", 1)[0][len("/live/"):]
                with server._lock:
                    ls = server._live.get(token)
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

            # -- Routing ------------------------------------------------------
            def _resolve(self) -> Optional[tuple[str, str]]:
                """Mappt den Pfad auf (Dateipfad, MIME) oder None (404)."""
                path = self.path.split("?", 1)[0]
                if path.startswith("/file/"):
                    token = path[len("/file/") :].split(".", 1)[0]
                    with server._lock:
                        entry = server._files.get(token)
                    return entry
                if path.startswith("/hls/"):
                    with server._lock:
                        base = server._hls_dir
                    if base is None:
                        return None
                    name = os.path.basename(path[len("/hls/") :])
                    fp = (base / name).resolve()
                    # Pfad-Traversal verhindern
                    if base.resolve() not in fp.parents and fp != base.resolve():
                        return None
                    if not fp.is_file():
                        return None
                    return (str(fp), _guess_mime(str(fp)))
                return None

            def _serve(self, head_only: bool) -> None:
                entry = self._resolve()
                if entry is None:
                    self.send_error(404)
                    return
                fpath, mime = entry
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    self.send_error(404)
                    return

                # Range-Request (Spulen in MP4) auswerten
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                partial = False
                if rng and rng.startswith("bytes="):
                    partial = True
                    spec = rng[len("bytes=") :].split(",")[0]
                    s, _, e = spec.partition("-")
                    if s.strip():
                        start = int(s)
                    if e.strip():
                        end = int(e)
                    end = min(end, size - 1)
                    if start > end:
                        self.send_error(416)
                        return

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
                        chunk = fh.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        remaining -= len(chunk)

        return Handler
