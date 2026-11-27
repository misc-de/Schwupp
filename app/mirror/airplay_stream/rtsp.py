"""Blockierender RTSP-Client für den AirPlay-Steuerkanal.

Bis zum Pair-Verify läuft der Kanal im Klartext, danach in HAP-Rahmen
(:class:`~.hap.HapCipher`). Anfragen werden serialisiert: Die Sitzung schickt
von zwei Threads aus (Aufbau und ``/feedback``), auf einer TCP-Verbindung darf
aber immer nur eine Anfrage unterwegs sein.
"""
from __future__ import annotations

import contextlib
import plistlib
import socket
import threading

USER_AGENT = "AirPlay/935.7.1"
_HEADER_LIMIT = 64 * 1024
_BODY_LIMIT = 4 * 1024 * 1024


class RtspError(Exception):
    """Transport- oder Protokollfehler: Die Verbindung ist danach unbrauchbar."""


class StatusError(Exception):
    """Vollständige Antwort mit Fehlerstatus: Die Verbindung bleibt nutzbar."""

    def __init__(self, method: str, uri: str, status: int, reason: str,
                 detail: str = "") -> None:
        text = f"{method} {uri}: {status} {reason}".strip()
        super().__init__(f"{text} – {detail}" if detail else text)
        self.status = status


class Response:
    def __init__(self, status: int, reason: str, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.reason = reason
        self.headers = headers       # Namen klein geschrieben
        self.body = body

    def plist(self) -> dict:
        if not self.body:
            return {}
        try:
            value = plistlib.loads(self.body)
        except Exception as exc:  # noqa: BLE001
            raise RtspError(f"Antwort ist keine gültige plist: {exc}") from exc
        if not isinstance(value, dict):
            raise RtspError("Antwort ist kein plist-Wörterbuch")
        return value


def parse_head(head: bytes) -> tuple[int, str, dict[str, str]]:
    """Statuszeile und Header einer Antwort (ohne die Leerzeile)."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith(("RTSP/", "HTTP/")):
        raise RtspError(f"ungültige Statuszeile {lines[0]!r}")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise RtspError(f"ungültiger Status {parts[1]!r}") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            raise RtspError(f"ungültiger Header {line!r}")
        headers[name.strip().lower()] = value.strip()
    return status, parts[2] if len(parts) > 2 else "", headers


def content_length(headers: dict[str, str]) -> int:
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise RtspError("ungültige Content-Length") from exc
    if length < 0 or length > _BODY_LIMIT:
        raise RtspError(f"Content-Length {length} außerhalb der Grenzen")
    return length


class RtspClient:
    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.cipher = None           # HapCipher nach dem Pair-Verify
        self._cseq = 0
        self._lock = threading.Lock()
        self._buffer = b""

    # -- Adressen ---------------------------------------------------------------
    @property
    def peer_host(self) -> str:
        """Adresse, unter der der Empfänger wirklich antwortet (für Medienports)."""
        return self.sock.getpeername()[0]

    @property
    def local_host(self) -> str:
        return self.sock.getsockname()[0]

    # -- Anfragen ---------------------------------------------------------------
    def request(self, method: str, uri: str, body: bytes = b"",
                content_type: str = "", headers: dict[str, str] | None = None,
                timeout: float = 10.0, allow_error: bool = False) -> Response:
        with self._lock:
            self._cseq += 1
            lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {self._cseq}",
                     f"User-Agent: {USER_AGENT}"]
            for name, value in (headers or {}).items():
                lines.append(f"{name}: {value}")
            if content_type and body:
                lines.append(f"Content-Type: {content_type}")
            lines.append(f"Content-Length: {len(body)}")
            data = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
            if self.cipher is not None:
                data = self.cipher.encrypt(data)
            try:
                self.sock.settimeout(timeout)
                self.sock.sendall(data)
                response = self._read_response()
            except OSError as exc:
                raise RtspError(f"{method} {uri}: {exc}") from exc
        if not allow_error and not 200 <= response.status < 300:
            detail = ""
            if response.headers.get("content-type", "").startswith("text/"):
                detail = response.body[:200].decode("utf-8", "replace").strip()
            raise StatusError(method, uri, response.status, response.reason, detail)
        return response

    def request_plist(self, method: str, uri: str, payload: dict,
                      **kwargs) -> Response:
        body = plistlib.dumps(payload, fmt=plistlib.FMT_BINARY)
        return self.request(method, uri, body, "application/x-apple-binary-plist", **kwargs)

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise RtspError("Verbindung vom Gerät geschlossen")
        if self.cipher is not None:
            chunk = self.cipher.decrypt(chunk)
        self._buffer += chunk

    def _read_response(self) -> Response:
        while b"\r\n\r\n" not in self._buffer:
            if len(self._buffer) > _HEADER_LIMIT:
                raise RtspError("Antwort-Header zu groß")
            self._fill()
        head, _, rest = self._buffer.partition(b"\r\n\r\n")
        status, reason, headers = parse_head(head)
        length = content_length(headers)
        self._buffer = rest
        while len(self._buffer) < length:
            self._fill()
        body, self._buffer = self._buffer[:length], self._buffer[length:]
        return Response(status, reason, headers, body)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()
