"""Netzwerk-Hilfen."""
from __future__ import annotations

import socket
import urllib.parse
import urllib.request

# Größte Antwort, die Schwupp von einem Gerät im Netz liest (UPnP-Beschreibung,
# SOAP-Antwort, AirPlay-/info). Echte Antworten haben wenige KB, die
# Sonos-Topologie eines großen Haushalts gut 100 KB; ohne Grenze füllte ein
# Gerät, das endlos sendet, den Speicher.
MAX_REPLY = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Weiterleitungen nicht folgen: Ein Gerät im LAN soll Schwupp nicht auf
    andere Hosts (etwa Dienste auf diesem Rechner) umlenken können."""

    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return None


# Ohne Proxy (Geräte stehen im LAN, ein http_proxy aus der Umgebung führte
# woandershin) und ohne Weiterleitungen; nur http und https.
_OPENER = urllib.request.OpenerDirector()
for _handler in (urllib.request.ProxyHandler({}), urllib.request.HTTPHandler(),
                 urllib.request.HTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(),
                 _NoRedirect(), urllib.request.HTTPErrorProcessor()):
    _OPENER.add_handler(_handler)


def is_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def location_of(url: str, host: str) -> bool:
    """Zeigt eine per SSDP gemeldete Adresse (``LOCATION``) auf das Gerät, das
    geantwortet hat? Jeder im LAN kann SSDP beantworten; ohne diese Prüfung
    ließe er Schwupp beliebige Adressen abrufen (``file:``, Dienste auf
    diesem Rechner)."""
    if not is_http_url(url):
        return False
    try:
        return urllib.parse.urlsplit(url).hostname == host
    except ValueError:
        return False


def http_read(target: str | urllib.request.Request, timeout: float,
              limit: int = MAX_REPLY) -> bytes:
    """Antwort eines Geräts lesen: nur http(s), höchstens *limit* Bytes.

    Wirft OSError (wie ``urlopen``), auch für eine andere Adressart oder eine
    zu große Antwort.
    """
    url = target.full_url if isinstance(target, urllib.request.Request) else target
    if not is_http_url(url):
        raise OSError(f"keine HTTP-Adresse: {url!r}")
    reply = _OPENER.open(target, timeout=timeout)
    try:
        data = reply.read(limit + 1)
    finally:
        reply.close()
    if len(data) > limit:
        raise OSError(f"Antwort von {url} ist größer als {limit} Bytes")
    return data


def lan_ip(target: str = "8.8.8.8") -> str:
    """Liefert die lokale IP des Interfaces, das Richtung *target* routet.

    *target* sollte idealerweise die IP des Cast-Geräts sein – dann wird bei
    mehreren Netzwerk-Interfaces (WLAN, Docker-Bridges …) garantiert die
    Adresse gewählt, die das Gerät auch erreichen kann. Es werden keine Daten
    gesendet; der UDP-Socket ermittelt nur das Routing.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def free_port() -> int:
    """Reserviert einen freien TCP-Port vom Betriebssystem und gibt ihn zurück."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("", 0))
        return s.getsockname()[1]
    finally:
        s.close()
