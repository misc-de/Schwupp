"""Netzwerk-Hilfen."""
from __future__ import annotations

import socket


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
