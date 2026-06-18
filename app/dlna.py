"""Minimaler DLNA/UPnP-AVTransport-Client.

Damit kann ein UPnP-MediaRenderer (z. B. LG webOS) angewiesen werden, eine
HTTP-URL abzuspielen (lokale Datei aus unserem Server, HLS-Stream …). Es werden
nur die nötigen SOAP-Aktionen umgesetzt: SetAVTransportURI, Play, Pause, Stop.

Wichtig: Progressive MP4s müssen den ``moov``-Atom am Dateianfang haben
(„faststart"), sonst meldet der TV „Datei kann nicht erkannt werden".
"""
from __future__ import annotations

import re
import socket
import time
import urllib.request
from xml.sax.saxutils import escape

AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"


def _ssdp_locations(host: str, st: str, timeout: float = 3.0) -> list[str]:
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1", "HOST:239.255.255.250:1900",
        'MAN:"ssdp:discover"', "MX:2", f"ST:{st}", "", "",
    ]).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(timeout)
    s.sendto(msg, ("239.255.255.250", 1900))
    locs: list[str] = []
    t = time.time()
    while time.time() - t < timeout:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            break
        if addr[0] == host:
            m = re.search(r"LOCATION:\s*(\S+)", data.decode(errors="ignore"), re.I)
            if m:
                locs.append(m.group(1))
    s.close()
    return locs


class DlnaRenderer:
    """AVTransport-Steuerung eines MediaRenderers an *host*."""

    def __init__(self, host: str) -> None:
        self.host = host
        self._control: str | None = None

    def resolve(self) -> bool:
        """Ermittelt die AVTransport-Control-URL via SSDP. True bei Erfolg."""
        for st in ("urn:schemas-upnp-org:device:MediaRenderer:1", "ssdp:all"):
            for loc in _ssdp_locations(self.host, st):
                ctrl = self._control_from_description(loc)
                if ctrl:
                    self._control = ctrl
                    return True
        return False

    def _control_from_description(self, loc: str) -> str | None:
        try:
            xml = urllib.request.urlopen(loc, timeout=5).read().decode(errors="ignore")
        except OSError:
            return None
        base = re.match(r"(https?://[^/]+)", loc).group(1)
        m = re.search(
            r"AVTransport:1</serviceType>.*?<controlURL>([^<]+)</controlURL>", xml, re.S
        )
        if not m:
            return None
        ctrl = m.group(1)
        return base + (ctrl if ctrl.startswith("/") else "/" + ctrl)

    # -- SOAP ----------------------------------------------------------------
    def _soap(self, action: str, inner: str, timeout: float = 6.0) -> bytes:
        assert self._control, "AVTransport nicht aufgelöst"
        env = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f"<s:Body>{inner}</s:Body></s:Envelope>"
        )
        req = urllib.request.Request(
            self._control, data=env.encode(),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{AVTRANSPORT}#{action}"',
            },
        )
        return urllib.request.urlopen(req, timeout=timeout).read()

    def play_url(self, url: str, mime: str, title: str = "Schwupp") -> None:
        cls = "object.item.audioItem" if mime.startswith("audio") else "object.item.videoItem"
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="0" parentID="-1" restricted="1"><dc:title>{escape(title)}</dc:title>'
            f"<upnp:class>{cls}</upnp:class>"
            f'<res protocolInfo="http-get:*:{mime}:*">{escape(url)}</res></item></DIDL-Lite>'
        )
        self._soap(
            "SetAVTransportURI",
            f'<u:SetAVTransportURI xmlns:u="{AVTRANSPORT}"><InstanceID>0</InstanceID>'
            f"<CurrentURI>{escape(url)}</CurrentURI>"
            f"<CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData></u:SetAVTransportURI>",
        )
        time.sleep(0.5)
        # Manche Renderer starten automatisch; Play kann verzögert antworten -> kurz, tolerant.
        try:
            self._soap(
                "Play",
                f'<u:Play xmlns:u="{AVTRANSPORT}"><InstanceID>0</InstanceID>'
                "<Speed>1</Speed></u:Play>",
                timeout=4,
            )
        except OSError:
            pass

    def _simple(self, action: str) -> None:
        try:
            self._soap(action, f'<u:{action} xmlns:u="{AVTRANSPORT}">'
                               f"<InstanceID>0</InstanceID></u:{action}>", timeout=4)
        except OSError:
            pass

    def stop(self) -> None:
        self._simple("Stop")
