"""Geräte-Capability-Prober.

Klopft ein (neues) Gerät systematisch ab und meldet, welche Cast-/Mirror-/
Steuerungs-Möglichkeiten es bietet – die Automatisierung der manuellen Analyse,
die wir beim LG OLED durchgespielt haben.

Aufruf:
    .venv/bin/python -m app.probe 192.168.0.33     # gezielt ein Gerät
    .venv/bin/python -m app.probe                  # alle gefundenen Geräte
"""
from __future__ import annotations

import plistlib
import socket
import sys
import urllib.request
from dataclasses import dataclass, field

# Charakteristische Ports je Protokoll
PORTS = {
    8009: "Google Cast",
    7000: "AirPlay",
    3001: "LG webOS (wss)",
    3000: "LG webOS (ws)",
    7236: "Miracast/WFD (RTSP)",
    9080: "DLNA (LG)",
}


@dataclass
class ProbeResult:
    host: str
    ports: dict[int, bool] = field(default_factory=dict)
    airplay: dict | None = None
    dlna_avtransport: str | None = None
    capabilities: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_features(raw) -> int | None:
    """features kann int (aus /info) oder 'low,high' (aus mDNS-TXT) sein."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    s = str(raw)
    if "," in s:  # "0xLOW,0xHIGH"
        low, high = (int(x, 16) for x in s.split(",", 1))
        return (high << 32) | low
    return int(s, 16) if s.startswith("0x") else int(s)


def _airplay_flags(feat: int) -> list[str]:
    try:
        from pyatv.protocols.airplay.auth import AirPlayFlags

        return [x.name for x in AirPlayFlags if feat & int(x)]
    except Exception:
        return []


def _probe_airplay(host: str) -> dict | None:
    try:
        data = urllib.request.urlopen(f"http://{host}:7000/info", timeout=4).read()
        info = plistlib.loads(data)
    except Exception:
        return None
    feat = _parse_features(info.get("features"))
    flags = _airplay_flags(feat) if feat else []
    return {
        "name": info.get("name"),
        "model": info.get("model"),
        "manufacturer": info.get("manufacturer"),
        "features_hex": hex(feat) if feat else None,
        "flags": flags,
        "screen_mirror": "SupportsAirPlayScreen" in flags,
        "video": "SupportsAirPlayVideoV2" in flags or "SupportsAirPlayVideoPlayQueue" in flags,
        "access_control": any("AccessControl" in f for f in flags),
    }


def _probe_dlna(host: str) -> str | None:
    try:
        from .dlna import DlnaRenderer

        r = DlnaRenderer(host)
        return r._control if r.resolve() else None
    except Exception:
        return None


def probe(host: str) -> ProbeResult:
    res = ProbeResult(host=host)
    res.ports = {p: _port_open(host, p) for p in PORTS}
    if res.ports.get(7000):
        res.airplay = _probe_airplay(host)
    res.dlna_avtransport = _probe_dlna(host)
    _assess(res)
    return res


def _assess(res: ProbeResult) -> None:
    caps, notes = res.capabilities, res.notes

    # Google Cast
    if res.ports.get(8009):
        caps.append("✅ Google Cast: Medien, YouTube, Bildschirm (HLS + nativ möglich)")

    # LG webOS
    if res.ports.get(3001) or res.ports.get(3000):
        caps.append("✅ LG webOS: Steuerung, YouTube (Deep-Link), Medien via DLNA")

    # DLNA
    if res.dlna_avtransport:
        caps.append("✅ DLNA: lokale Mediendateien abspielen (MP4 mit faststart)")
        notes.append("DLNA spielt KEINE Live-Streams (kein Bildschirm-Mirror).")

    # AirPlay
    ap = res.airplay
    if ap:
        if ap["screen_mirror"]:
            notes.append("AirPlay-Mirror vom Gerät unterstützt – Sender braucht aber FairPlay (Apples DRM).")
        if ap["video"]:
            if ap["access_control"]:
                notes.append("AirPlay-Video (/play) vorhanden, ABER Pairing durch HomeKit-Access-Control "
                             "meist blockiert (Nicht-Apple-Geräte).")
            else:
                caps.append("✅ AirPlay-Video: Medien/HLS via play_url möglich (Pairing testen)")
        if ap["access_control"]:
            notes.append("AirPlay-Zugriffskontrolle aktiv → Pairing von Linux/pyatv vermutlich nicht möglich.")

    # Miracast-Hinweis
    if res.ports.get(7236):
        notes.append("Miracast/WFD-RTSP-Port offen – Bildschirm-Mirror via Wi-Fi-Direct denkbar "
                     "(unter NetworkManager aber instabil, siehe docs/MIRRORING.md).")

    if not caps:
        caps.append("⚠️ Kein direkt nutzbares Cast-Protokoll erkannt.")


def _format(res: ProbeResult) -> str:
    lines = [f"\n=== Geräte-Analyse: {res.host} ==="]
    if res.airplay and res.airplay.get("model"):
        a = res.airplay
        lines.append(f"  Modell: {a.get('model')} ({a.get('manufacturer')})  Name: {a.get('name')}")
    lines.append("  Offene Ports: " + (", ".join(
        f"{p}/{PORTS[p]}" for p, ok in res.ports.items() if ok) or "(keine bekannten)"))
    if res.dlna_avtransport:
        lines.append("  DLNA-AVTransport: " + res.dlna_avtransport)
    if res.airplay and res.airplay.get("flags"):
        lines.append(f"  AirPlay-Flags: {len(res.airplay['flags'])} (screen={res.airplay['screen_mirror']}, "
                     f"access_control={res.airplay['access_control']})")
    lines.append("\n  Möglichkeiten:")
    for c in res.capabilities:
        lines.append(f"    {c}")
    if res.notes:
        lines.append("\n  Hinweise:")
        for n in res.notes:
            lines.append(f"    • {n}")
    return "\n".join(lines)


def _discover(timeout: float = 6.0) -> list[str]:
    """Findet Geräte im Netz und liefert ihre Hosts (für den No-Arg-Modus)."""
    import time

    from .discovery import Discovery

    hosts: dict[str, str] = {}
    disc = Discovery(on_add=lambda i: hosts.setdefault(i.host, i.name),
                     on_remove=lambda u: None)
    disc.start()
    time.sleep(timeout)
    disc.stop()
    return list(hosts)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv:
        hosts = argv
    else:
        print("Suche Geräte im Netz (6 s) …")
        hosts = _discover()
        if not hosts:
            print("Keine Geräte gefunden.")
            return 1
    for h in hosts:
        print(_format(probe(h)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
