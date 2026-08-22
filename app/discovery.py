"""Vereinheitlichte Geräte-Discovery für alle Backends.

* **Chromecast / Google-TV** – via pychromecast ``CastBrowser`` (``_googlecast._tcp``)
* **LG webOS** – via ``_airplay._tcp`` (LG-TVs ab ~2019 kündigen AirPlay an;
  gefiltert auf Hersteller „LG"). Ältere LG ließen sich zusätzlich per SSDP
  finden – hier bewusst schlank gehalten.
* **AirPlay-2-TVs** (Hisense/VIDAA, Samsung, Sony, Apple TV …) – alle übrigen
  ``_airplay._tcp``-Ankündigungen als eigenes Backend ``airplay``
  (PIN-Pairing + Wiedergabe via pyatv, siehe receivers/airplay.py).

Liefert einheitliche :class:`ReceiverInfo`-Objekte. Callbacks kommen aus
zeroconf-Threads – die GUI marshallt per ``GLib.idle_add``.
"""
from __future__ import annotations

import re
import socket
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import zeroconf
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo
from zeroconf import IPVersion, ServiceStateChange

AIRPLAY_SERVICE = "_airplay._tcp.local."
DLNA_RENDERER_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"

# Wartezeiten zwischen den SSDP-Runden (Sekunden). Anfangs eng, damit die Liste
# schnell steht, danach sparsam – auf dem Telefon ist jeder Broadcast Akkulast.
DLNA_INTERVALS = (5, 10, 20, 60)
# So oft darf ein DLNA-Gerät schweigen, bevor es aus der Liste fliegt.
DLNA_MISSES_UNTIL_GONE = 3

# Backend-Vorrang bei gleichem Host: Cast > webOS > AirPlay > DLNA
# (mehr Funktionen gewinnt; AirPlay schlägt DLNA wegen HLS-Spiegelung)
_KIND_PRIORITY = {"chromecast": 4, "webos": 3, "airplay": 2, "dlna": 1}


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


def _fetch_renderer(loc: str) -> tuple[str, str, str] | None:
    """Lädt die UPnP-Geräte-Beschreibung. Gibt (name, model, avtransport_control_url)
    zurück oder None, wenn es kein AVTransport-MediaRenderer ist."""
    try:
        xml = urllib.request.urlopen(loc, timeout=5).read().decode(errors="ignore")
    except OSError:
        return None
    if "AVTransport:1" not in xml:
        return None
    base_m = re.match(r"(https?://[^/]+)", loc)
    ctrl_m = re.search(
        r"AVTransport:1</serviceType>.*?<controlURL>([^<]+)</controlURL>", xml, re.S)
    if not base_m or not ctrl_m:
        return None
    base, ctrl = base_m.group(1), ctrl_m.group(1)
    control = base + (ctrl if ctrl.startswith("/") else "/" + ctrl)
    return _tag(xml, "friendlyName") or "DLNA-Renderer", _tag(xml, "modelName"), control


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
        self._by_host: dict[str, ReceiverInfo] = {}
        self._lock = threading.Lock()
        self._dlna_stop = threading.Event()
        self._dlna_thread: threading.Thread | None = None
        # mDNS-Servicename -> uuid, damit ein "Removed" den Eintrag findet
        self._airplay_by_service: dict[str, str] = {}
        # DLNA hat kein Removal-Signal: zählt, wie viele Scans ein Host schweigt
        self._dlna_misses: dict[str, int] = {}

    # -- Dedup über Host (ein TV kann via _googlecast UND _airplay/8009 auftauchen) --
    def _emit_add(self, info: ReceiverInfo) -> None:
        old_uuid = None
        with self._lock:
            prev = self._by_host.get(info.host)
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
            self._by_host[info.host] = info
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
        self._dlna_thread = threading.Thread(target=self._dlna_loop, daemon=True)
        self._dlna_thread.start()

    def stop(self) -> None:
        self._dlna_stop.set()
        try:
            if self._cast:
                self._cast.stop_discovery()
            if self._airplay:
                self._airplay.cancel()
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
                for host, loc in _ssdp_search(DLNA_RENDERER_ST).items():
                    if self._dlna_stop.is_set():
                        break
                    seen.add(host)
                    with self._lock:
                        prev = self._by_host.get(host)
                    # Host schon als höherwertiges Backend bekannt? -> Beschreibung sparen
                    if prev is not None and _KIND_PRIORITY.get(prev.kind, 0) > _KIND_PRIORITY["dlna"]:
                        continue
                    found = _fetch_renderer(loc)
                    if not found:
                        continue
                    name, model, control = found
                    self._emit_add(ReceiverInfo(
                        kind="dlna", uuid=f"dlna:{host}", name=name,
                        host=host, port=0, model=model, raw=control,
                    ))
                self._expire_dlna(seen)
            except Exception:  # noqa: BLE001
                pass
            round_no += 1
            self._dlna_stop.wait(DLNA_INTERVALS[min(round_no, len(DLNA_INTERVALS) - 1)])

    def _expire_dlna(self, seen: set[str]) -> None:
        """Entfernt DLNA-Geräte, die mehrere Scans in Folge nicht geantwortet haben.

        Ein ausgeschalteter TV blieb sonst dauerhaft in der Liste stehen – der
        Verbindungsversuch lief dann in einen Timeout.
        """
        with self._lock:
            stale = [inf for host, inf in self._by_host.items()
                     if inf.kind == "dlna" and host not in seen]
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
