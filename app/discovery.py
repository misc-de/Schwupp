"""Vereinheitlichte Geräte-Discovery für alle Backends.

* **Chromecast / Google-TV** – via pychromecast ``CastBrowser`` (``_googlecast._tcp``)
* **LG webOS** – via ``_airplay._tcp`` (LG-TVs ab ~2019 kündigen AirPlay an;
  gefiltert auf Hersteller „LG"). Ältere LG ließen sich zusätzlich per SSDP
  finden – hier bewusst schlank gehalten.

Liefert einheitliche :class:`ReceiverInfo`-Objekte. Callbacks kommen aus
zeroconf-Threads – die GUI marshallt per ``GLib.idle_add``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

import zeroconf
from zeroconf import IPVersion, ServiceStateChange
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo

AIRPLAY_SERVICE = "_airplay._tcp.local."


def _is_cast_receiver(host: str, port: int = 8009, timeout: float = 1.5) -> bool:
    """True, wenn auf *host:port* ein TLS-Cast-Receiver lauscht (auch ohne mDNS)."""
    import socket
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                return s.getpeercert(binary_form=True) is not None
    except OSError:
        return False


@dataclass(frozen=True)
class ReceiverInfo:
    kind: str          # "chromecast" | "webos"
    uuid: str
    name: str
    host: str
    port: int
    model: str
    raw: object = None  # CastInfo (cast) bzw. None (webos)


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

    # -- Dedup über Host (ein TV kann via _googlecast UND _airplay/8009 auftauchen) --
    def _emit_add(self, info: "ReceiverInfo") -> None:
        old_uuid = None
        with self._lock:
            prev = self._by_host.get(info.host)
            if prev is not None:
                if prev.uuid == info.uuid:
                    return
                # echte CastInfo (raw gesetzt) schlägt den reinen 8009-Probe
                if prev.raw is not None and info.raw is None:
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

    def stop(self) -> None:
        try:
            if self._cast:
                self._cast.stop_discovery()
            if self._airplay:
                self._airplay.cancel()
        finally:
            self.zconf.close()

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

    # -- LG webOS (AirPlay-Announcement) -------------------------------------
    def _airplay_change(self, zeroconf, service_type, name, state_change) -> None:  # noqa: ANN001
        if state_change is ServiceStateChange.Removed:
            return  # Host-Dedup hält den Eintrag; Cast-Remove räumt ihn ab
        info = zeroconf.get_service_info(service_type, name, timeout=3000)
        if info is None:
            return
        props = {k.decode(errors="ignore"): (v or b"").decode(errors="ignore")
                 for k, v in (info.properties or {}).items()}
        if props.get("manufacturer", "").upper() != "LG":
            return  # nur LG-webOS-TVs (keine Apple-TVs o. Ä.)
        addrs = info.parsed_addresses(IPVersion.V4Only)  # IPv4 -> kein v4/v6-Duplikat
        if not addrs:
            return
        host = addrs[0]
        friendly = name.split("._airplay")[0]
        model = props.get("model", "")
        # Viele LG-TVs haben einen versteckten Cast-Receiver auf 8009 (ohne
        # _googlecast-mDNS). Cast bietet Media + YouTube + HLS-Mirror -> bevorzugen.
        if _is_cast_receiver(host):
            self._emit_add(ReceiverInfo(
                kind="chromecast", uuid=f"cast:{host}", name=friendly,
                host=host, port=8009, model=model, raw=None,
            ))
        else:
            self._emit_add(ReceiverInfo(
                kind="webos", uuid=f"webos:{host}", name=friendly,
                host=host, port=3001, model=model, raw=None,
            ))
