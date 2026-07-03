"""Geräte-Backends (Receiver): einheitliche Schnittstelle über Chromecast,
LG webOS, AirPlay 2 (Hisense/VIDAA, Samsung, Sony, Apple TV …) und
generisches DLNA (Samsung, Sony, Panasonic, Philips, Hisense …)."""
from .base import Feature, Receiver  # noqa: F401


def create_receiver(info, context):  # noqa: ANN001
    """Erzeugt den passenden Receiver für eine ReceiverInfo.

    *context* bündelt geteilte Ressourcen (zeroconf-Instanz, Config).
    """
    if info.kind == "chromecast":
        from .chromecast import ChromecastReceiver

        return ChromecastReceiver(info, context)
    if info.kind == "webos":
        from .webos import WebosReceiver

        return WebosReceiver(info, context)
    if info.kind == "airplay":
        from .airplay import AirplayReceiver

        return AirplayReceiver(info, context)
    if info.kind == "dlna":
        from .dlna import DlnaReceiver

        return DlnaReceiver(info, context)
    raise ValueError(f"Unbekannter Receiver-Typ: {info.kind!r}")
