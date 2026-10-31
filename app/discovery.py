"""Vereinheitlichte Geräte-Discovery für alle Backends.

* **Chromecast / Google-TV** – via pychromecast ``CastBrowser`` (``_googlecast._tcp``)
* **LG webOS** – via ``_airplay._tcp`` (LG-TVs ab ~2019 kündigen AirPlay an;
  gefiltert auf Hersteller „LG"). Ältere LG ließen sich zusätzlich per SSDP
  finden – hier bewusst schlank gehalten.
* **AirPlay-2-TVs** (Hisense/VIDAA, Samsung, Sony, Apple TV …) – alle übrigen
  ``_airplay._tcp``-Ankündigungen mit Video als eigenes Backend ``airplay``
  (PIN-Pairing + Wiedergabe via pyatv, siehe receivers/airplay.py).
* **AirPlay-Lautsprecher** – ``_raop._tcp`` als Backend ``raop``; sie bekommen
  den Systemton über PipeWires RAOP-Sink (siehe speaker/).
* **Sonos** – per SSDP wie DLNA, aber als eigenes Backend ``sonos``: nur der
  Koordinator einer Raumgruppe erscheint („Küche +1"), wie in Emilia.

Liefert einheitliche :class:`ReceiverInfo`-Objekte. Callbacks kommen aus
zeroconf-Threads – die GUI marshallt per ``GLib.idle_add``.
"""
from __future__ import annotations

import html
import re
import socket
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from uuid import UUID

import zeroconf
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo
from zeroconf import IPVersion, ServiceStateChange

from .dlna import guess_controls

AIRPLAY_SERVICE = "_airplay._tcp.local."
RAOP_SERVICE = "_raop._tcp.local."
DLNA_RENDERER_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
# Sonos antwortet zuverlässig nur auf seinen eigenen Gerätetyp
SONOS_ST = "urn:schemas-upnp-org:device:ZonePlayer:1"
ZONE_TOPOLOGY = "urn:schemas-upnp-org:service:ZoneGroupTopology:1"

# Wartezeiten zwischen den SSDP-Runden (Sekunden). Anfangs eng, damit die Liste
# schnell steht, danach sparsam – auf dem Telefon ist jeder Broadcast Akkulast.
DLNA_INTERVALS = (5, 10, 20, 60)
# So oft darf ein DLNA-Gerät schweigen, bevor es aus der Liste fliegt.
DLNA_MISSES_UNTIL_GONE = 3

# Backend-Vorrang bei gleichem Host: Cast > webOS > Sonos > AirPlay > DLNA > RAOP
# (mehr Funktionen gewinnt; AirPlay schlägt DLNA wegen HLS-Spiegelung). Sonos
# kündigt auch AirPlay an, holt sich den Ton aber selbst – billiger als ihn per
# RAOP hinzuschieben. RAOP ist nur der Weg für reine AirPlay-Lautsprecher.
_KIND_PRIORITY = {"chromecast": 5, "webos": 4, "sonos": 3, "airplay": 2, "dlna": 1, "raop": 0}

# AirPlay-Feature-Bits: Video (0) und Bildschirm (7). Ohne beide ist das Gerät
# ein Lautsprecher und kein Spiegelziel.
_AIRPLAY_VIDEO_BITS = (1 << 0) | (1 << 7)


def _tag(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([^<]+)</{tag}>", xml)
    return m.group(1).strip() if m else ""


def _ssdp_search(st: str, timeout: float = 3.0) -> dict[str, str]:
    """SSDP M-SEARCH; liefert {host: LOCATION-URL} aller Antwortenden."""
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1", "HOST:239.255.255.250:1900",
        'MAN:"ssdp:discover"', "MX:2", f"ST:{st}", "", "",
    ]).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(timeout)
    out: dict[str, str] = {}
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        t = time.time()
        while time.time() - t < timeout:
            try:
                data, addr = s.recvfrom(4096)
            except TimeoutError:
                break
            m = re.search(rb"LOCATION:\s*(\S+)", data, re.I)
            if m:
                out.setdefault(addr[0], m.group(1).decode(errors="ignore"))
    finally:
        s.close()
    return out


@dataclass(frozen=True)
class DlnaInfo:
    """Control-URLs eines DLNA-Renderers, deren Pfade geraten werden mussten."""

    avtransport: str
    rendering: str | None


@dataclass(frozen=True)
class SonosInfo:
    """Was ein Sonos-Lautsprecher zusätzlich zur AVTransport-URL braucht."""

    avtransport: str
    rendering: str | None        # RenderingControl (Lautstärke des Raums)
    group_rendering: str | None  # GroupRenderingControl (Lautstärke der Gruppe)
    topology: str | None         # ZoneGroupTopology (Raumgruppen)
    udn: str                     # RINCON_…, so nennt die Topologie den Koordinator


def _control_url(xml: str, base: str, service: str) -> str | None:
    m = re.search(rf"{service}</serviceType>.*?<controlURL>([^<]+)</controlURL>", xml, re.S)
    if not m:
        return None
    ctrl = m.group(1)
    return base + (ctrl if ctrl.startswith("/") else "/" + ctrl)


def _fetch_renderer(loc: str) -> tuple[str, str, object] | None:
    """Lädt die UPnP-Geräte-Beschreibung. Gibt (name, model, raw) zurück oder
    None, wenn es kein AVTransport-MediaRenderer ist. *raw* ist die
    AVTransport-Control-URL, bei Sonos ein :class:`SonosInfo`."""
    try:
        xml = urllib.request.urlopen(loc, timeout=5).read().decode(errors="ignore")
    except OSError:
        return None
    base_m = re.match(r"(https?://[^/]+)", loc)
    if not base_m:
        return None
    base = base_m.group(1)
    name = _tag(xml, "friendlyName") or "DLNA-Renderer"
    model = _tag(xml, "modelName")
    if "AVTransport:1" not in xml:
        # Renderer ohne Dienstliste (Frontier-Silicon-Radios): Pfade raten
        if ":MediaRenderer:" not in _tag(xml, "deviceType"):
            return None
        guessed = guess_controls(base)
        if guessed is None:
            return None
        return name, model, DlnaInfo(*guessed)
    control = _control_url(xml, base, "AVTransport:1")
    if control is None:
        return None
    if "sonos" not in _tag(xml, "manufacturer").lower() and ":ZonePlayer:" not in xml:
        return name, model, control
    # Sonos heißt nach seinem Raum; die friendlyName ist „IP - Modell"
    return _tag(xml, "roomName") or name, model, SonosInfo(
        avtransport=control,
        rendering=_control_url(xml, base, "RenderingControl:1"),
        group_rendering=_control_url(xml, base, "GroupRenderingControl:1"),
        topology=_control_url(xml, base, "ZoneGroupTopology:1"),
        udn=_tag(xml, "UDN").removeprefix("uuid:"),
    )


def sonos_groups(topology: str) -> dict[str, int] | None:
    """Raumgruppen eines Sonos-Haushalts: {Koordinator-UDN: Zahl der Räume}.

    Nicht sichtbare Mitglieder (Sub, Surround-Boxen: ``Invisible="1"``) zählen
    nicht als Raum. None, wenn die Topologie nicht zu lesen war.
    """
    env = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:GetZoneGroupState xmlns:u="{ZONE_TOPOLOGY}"/></s:Body></s:Envelope>'
    )
    req = urllib.request.Request(topology, data=env.encode(), headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{ZONE_TOPOLOGY}#GetZoneGroupState"',
    })
    try:
        body = urllib.request.urlopen(req, timeout=5).read().decode(errors="ignore")
    except OSError:
        return None
    return parse_zone_groups(html.unescape(body))


def parse_zone_groups(xml: str) -> dict[str, int]:
    groups: dict[str, int] = {}
    for m in re.finditer(r"<ZoneGroup\b([^>]*)>(.*?)</ZoneGroup>", xml, re.S):
        coord = re.search(r'Coordinator="([^"]+)"', m.group(1))
        if not coord:
            continue
        members = re.findall(r"<ZoneGroupMember\b([^>]*)", m.group(2))
        groups[coord.group(1)] = sum(1 for a in members if 'Invisible="1"' not in a)
    return groups


def airplay_has_video(features: str) -> bool:
    """Wirbt ein AirPlay-Gerät mit Video oder Bildschirm (``features``-TXT)?

    Das Feld ist ``0x…`` oder ``0x…,0x…`` (untere, obere 32 Bit). Ohne Angabe
    gilt das Gerät als Fernseher – so verhielt sich Schwupp bisher.
    """
    first = features.split(",", 1)[0].strip()
    if not first:
        return True
    try:
        return bool(int(first, 16) & _AIRPLAY_VIDEO_BITS)
    except ValueError:
        return True


def _is_cast_receiver(host: str, port: int = 8009, timeout: float = 1.5) -> bool:
    """True, wenn auf *host:port* ein TLS-Cast-Receiver lauscht (auch ohne mDNS)."""
    import socket
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw, \
                ctx.wrap_socket(raw, server_hostname=host) as s:
            return s.getpeercert(binary_form=True) is not None
    except OSError:
        return False


@dataclass(frozen=True)
class ReceiverInfo:
    kind: str          # "chromecast" | "webos" | "airplay" | "dlna"
    uuid: str
    name: str
    host: str
    port: int
    model: str
    raw: object = None  # CastInfo (cast) | None (webos/airplay) | AVTransport-URL (dlna)


class Discovery:
    def __init__(
        self,
        on_add: Callable[[ReceiverInfo], None],
        on_remove: Callable[[str], None],
    ) -> None:
        self._on_add = on_add
        self._on_remove = on_remove
        self.zconf = zeroconf.Zeroconf()
        self._cast: CastBrowser | None = None
        self._airplay: zeroconf.ServiceBrowser | None = None
        self._raop: zeroconf.ServiceBrowser | None = None
        self._by_host: dict[str, ReceiverInfo] = {}
        self._lock = threading.Lock()
        self._dlna_stop = threading.Event()
        self._dlna_thread: threading.Thread | None = None
        # mDNS-Servicename -> uuid, damit ein "Removed" den Eintrag findet
        self._airplay_by_service: dict[str, str] = {}
        self._raop_by_service: dict[str, str] = {}
        # DLNA hat kein Removal-Signal: zählt, wie viele Scans ein Host schweigt
        self._dlna_misses: dict[str, int] = {}

    @staticmethod
    def _key(info: ReceiverInfo) -> str:
        """Schlüssel fürs Dedup: der Host – außer bei Cast-Gruppen, die auf der
        IP eines Mitglieds unter einem eigenen Port laufen."""
        if info.kind == "chromecast" and info.port not in (0, 8009):
            return f"{info.host}:{info.port}"
        return info.host

    # -- Dedup über Host (ein TV kann via _googlecast UND _airplay/8009 auftauchen) --
    def _emit_add(self, info: ReceiverInfo) -> None:
        old_uuid = None
        key = self._key(info)
        with self._lock:
            prev = self._by_host.get(key)
            if prev is not None:
                if prev.uuid == info.uuid:
                    return
                pp = _KIND_PRIORITY.get(prev.kind, 0)
                ip = _KIND_PRIORITY.get(info.kind, 0)
                # höherwertiges Backend behalten (z. B. Cast/webOS schlägt DLNA)
                if pp > ip:
                    return
                # gleiche Stufe: echte CastInfo (raw) schlägt den reinen 8009-Probe
                if pp == ip and prev.raw is not None and info.raw is None:
                    return
                old_uuid = prev.uuid
            # Dasselbe Gerät unter einer anderen IP (mehrere Netze): nur einmal
            for other_key, other in list(self._by_host.items()):
                if other.uuid == info.uuid and other_key != key:
                    del self._by_host[other_key]
            self._by_host[key] = info
        if old_uuid:
            self._on_remove(old_uuid)
        self._on_add(info)

    def _emit_remove(self, uuid: str) -> None:
        with self._lock:
            for h, inf in list(self._by_host.items()):
                if inf.uuid == uuid:
                    del self._by_host[h]
                    break
        self._on_remove(uuid)

    # -- Steuerung ------------------------------------------------------------
    def start(self) -> None:
        self._cast = CastBrowser(
            SimpleCastListener(add_callback=self._cast_added,
                               update_callback=self._cast_added,
                               remove_callback=self._cast_removed),
            self.zconf,
        )
        self._cast.start_discovery()
        self._airplay = zeroconf.ServiceBrowser(
            self.zconf, AIRPLAY_SERVICE, handlers=[self._airplay_change]
        )
        self._raop = zeroconf.ServiceBrowser(
            self.zconf, RAOP_SERVICE, handlers=[self._raop_change]
        )
        self._dlna_thread = threading.Thread(target=self._dlna_loop, daemon=True)
        self._dlna_thread.start()

    def stop(self) -> None:
        self._dlna_stop.set()
        try:
            if self._cast:
                self._cast.stop_discovery()
            if self._airplay:
                self._airplay.cancel()
            if self._raop:
                self._raop.cancel()
        finally:
            self.zconf.close()

    # -- DLNA / UPnP (SSDP-Polling: Samsung, Sony, Panasonic, Philips, Hisense …) --
    def _dlna_loop(self) -> None:
        """Sucht DLNA-Renderer per SSDP (die haben kein mDNS-Push).

        Das Intervall wächst nach den ersten Runden von 10 auf 60 s: Direkt nach
        dem Start soll die Geräteliste schnell stehen, danach ist ein Broadcast
        alle paar Sekunden auf einem Telefon nur Funk- und Akkulast.
        """
        round_no = 0
        while not self._dlna_stop.is_set():
            seen: set[str] = set()
            try:
                locations = _ssdp_search(SONOS_ST)
                for host, loc in _ssdp_search(DLNA_RENDERER_ST).items():
                    locations.setdefault(host, loc)
                sonos: list[ReceiverInfo] = []
                for host, loc in locations.items():
                    if self._dlna_stop.is_set():
                        break
                    seen.add(host)
                    with self._lock:
                        prev = self._by_host.get(host)
                    # Host schon als höherwertiges Backend bekannt? -> Beschreibung sparen
                    if prev is not None and _KIND_PRIORITY.get(prev.kind, 0) > _KIND_PRIORITY["sonos"]:
                        continue
                    found = _fetch_renderer(loc)
                    if not found:
                        continue
                    name, model, raw = found
                    if isinstance(raw, SonosInfo):
                        sonos.append(ReceiverInfo(
                            kind="sonos", uuid=f"sonos:{raw.udn or host}", name=name,
                            host=host, port=1400, model=model, raw=raw,
                        ))
                        continue
                    self._emit_add(ReceiverInfo(
                        kind="dlna", uuid=f"dlna:{host}", name=name,
                        host=host, port=0, model=model, raw=raw,
                    ))
                self._emit_sonos(sonos)
                self._expire_dlna(seen)
            except Exception:  # noqa: BLE001
                pass
            round_no += 1
            self._dlna_stop.wait(DLNA_INTERVALS[min(round_no, len(DLNA_INTERVALS) - 1)])

    def _emit_sonos(self, speakers: list[ReceiverInfo]) -> None:
        """Meldet von den Sonos-Lautsprechern nur die Koordinatoren ihrer
        Raumgruppe, mit der Zahl der weiteren Räume im Namen („Küche +1").
        Wer in eine Gruppe aufgegangen ist, verschwindet aus der Liste – Ton an
        ein Gruppenmitglied lehnt Sonos ab."""
        topology = next((i.raw.topology for i in speakers if i.raw.topology), None)
        groups = sonos_groups(topology) if topology else None
        for info in speakers:
            if groups is None:          # Topologie unbekannt: jeder Raum für sich
                self._emit_add(info)
                continue
            rooms = groups.get(info.raw.udn)
            if rooms is None:
                with self._lock:
                    known = any(i.uuid == info.uuid for i in self._by_host.values())
                if known:
                    self._emit_remove(info.uuid)
                continue
            name = f"{info.name} +{rooms - 1}" if rooms > 1 else info.name
            self._emit_add(replace(info, name=name))

    def _expire_dlna(self, seen: set[str]) -> None:
        """Entfernt DLNA-Geräte, die mehrere Scans in Folge nicht geantwortet haben.

        Ein ausgeschalteter TV blieb sonst dauerhaft in der Liste stehen – der
        Verbindungsversuch lief dann in einen Timeout.
        """
        with self._lock:
            stale = [inf for host, inf in self._by_host.items()
                     if inf.kind in ("dlna", "sonos") and host not in seen]
        for inf in stale:
            misses = self._dlna_misses.get(inf.host, 0) + 1
            self._dlna_misses[inf.host] = misses
            if misses >= DLNA_MISSES_UNTIL_GONE:
                self._dlna_misses.pop(inf.host, None)
                self._emit_remove(inf.uuid)
        for host in seen:
            self._dlna_misses.pop(host, None)

    # -- Chromecast -----------------------------------------------------------
    def _cast_added(self, uuid: UUID, service: str) -> None:
        info: CastInfo | None = self._cast.devices.get(uuid)
        if info is None:
            return
        self._emit_add(ReceiverInfo(
            kind="chromecast", uuid=f"cast:{uuid}", name=info.friendly_name or "Chromecast",
            host=info.host, port=info.port, model=info.model_name or "", raw=info,
        ))

    def _cast_removed(self, uuid: UUID, service: str, info: CastInfo) -> None:
        self._emit_remove(f"cast:{uuid}")

    # -- AirPlay-Announcements (LG webOS + generische AirPlay-2-TVs) -----------
    def _airplay_change(self, zeroconf, service_type, name, state_change) -> None:  # noqa: ANN001
        if state_change is ServiceStateChange.Removed:
            # Gerät ist aus dem Netz verschwunden (TV aus). Nur entfernen, wenn
            # der Eintrag noch der von uns gemeldete ist – ein zwischenzeitlich
            # höherwertig erkanntes Backend (Cast) bleibt bestehen.
            uuid = self._airplay_by_service.pop(name, None)
            if uuid:
                with self._lock:
                    current = next((i for i in self._by_host.values() if i.uuid == uuid), None)
                if current is not None:
                    self._emit_remove(uuid)
            return
        info = zeroconf.get_service_info(service_type, name, timeout=3000)
        if info is None:
            return
        props = {k.decode(errors="ignore"): (v or b"").decode(errors="ignore")
                 for k, v in (info.properties or {}).items()}
        addrs = info.parsed_addresses(IPVersion.V4Only)  # IPv4 -> kein v4/v6-Duplikat
        if not addrs:
            return
        host = addrs[0]
        friendly = name.split("._airplay")[0]
        model = props.get("model", "")
        # Nicht-LG (Hisense/VIDAA, Samsung, Sony, Apple TV …) -> AirPlay-Backend.
        # Hat das Gerät zusätzlich einen Cast-Receiver (Android/Google-TV), kommt
        # der über _googlecast bzw. Host-Dedup ohnehin mit höherem Vorrang.
        if props.get("manufacturer", "").upper() != "LG":
            # Lautsprecher (HomePod, Sonos, AV-Receiver …) melden sich hier auch;
            # sie laufen über _raop._tcp, als Spiegelziel wären sie eine Sackgasse.
            if not airplay_has_video(props.get("features", "")):
                return
            self._airplay_by_service[name] = f"airplay:{host}"
            self._emit_add(ReceiverInfo(
                kind="airplay", uuid=f"airplay:{host}", name=friendly,
                host=host, port=info.port or 7000, model=model, raw=None,
            ))
            return
        # Viele LG-TVs haben einen versteckten Cast-Receiver auf 8009 (ohne
        # _googlecast-mDNS). Cast bietet Media + YouTube + HLS-Mirror -> bevorzugen.
        if _is_cast_receiver(host):
            self._airplay_by_service[name] = f"cast:{host}"
            self._emit_add(ReceiverInfo(
                kind="chromecast", uuid=f"cast:{host}", name=friendly,
                host=host, port=8009, model=model, raw=None,
            ))
        else:
            self._airplay_by_service[name] = f"webos:{host}"
            self._emit_add(ReceiverInfo(
                kind="webos", uuid=f"webos:{host}", name=friendly,
                host=host, port=3001, model=model, raw=None,
            ))

    # -- AirPlay-Lautsprecher (RAOP) -------------------------------------------
    def _raop_change(self, zeroconf, service_type, name, state_change) -> None:  # noqa: ANN001
        """``_raop._tcp``: Name ist ``<MAC>@<Gerätename>``. Lautsprecher mit
        Passwort bleiben draußen (wie in Emilia), dafür gibt es kein Eingabefeld."""
        if state_change is ServiceStateChange.Removed:
            uuid = self._raop_by_service.pop(name, None)
            if uuid:
                with self._lock:
                    current = next((i for i in self._by_host.values() if i.uuid == uuid), None)
                if current is not None:
                    self._emit_remove(uuid)
            return
        info = zeroconf.get_service_info(service_type, name, timeout=3000)
        if info is None:
            return
        props = {k.decode(errors="ignore"): (v or b"").decode(errors="ignore")
                 for k, v in (info.properties or {}).items()}
        if props.get("pw", "").lower() == "true":
            return
        addrs = info.parsed_addresses(IPVersion.V4Only)
        if not addrs:
            return
        host = addrs[0]
        label = name.split("._raop")[0]
        mac, _, friendly = label.rpartition("@")
        # Die MAC aus dem Dienstnamen bleibt gleich, auch wenn der Lautsprecher
        # über mehrere Netze (und damit IPs) gefunden wird
        uuid = f"raop:{mac or host}"
        self._raop_by_service[name] = uuid
        self._emit_add(ReceiverInfo(
            kind="raop", uuid=uuid, name=friendly or label, host=host,
            port=info.port or 7000, model=props.get("am", ""), raw=props,
        ))
