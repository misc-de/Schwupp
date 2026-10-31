"""Ton an Lautsprecher: der ganze Systemton an Cast, DLNA, Sonos oder AirPlay.

Übernommen aus Emilia (Sonos-Raumgruppen, ``x-rincon-mp3radio``, Live-MP3 für
Renderer); siehe :mod:`.soundcast` und :mod:`.pipewire`.
"""
from .soundcast import SoundCast, check_available  # noqa: F401


def is_speaker(info) -> bool:  # noqa: ANN001
    """Ist das Gerät ein Lautsprecher (Ton statt Bildschirm)?

    Cast-Lautsprecher und -Gruppen melden sich wie Fernseher, haben aber kein
    Bild; AirPlay-Lautsprecher, Sonos und DLNA-Renderer bekommen den Ton.
    """
    if info.kind in ("raop", "sonos", "dlna"):
        return True
    cast_type = getattr(info.raw, "cast_type", None)
    return info.kind == "chromecast" and cast_type in ("audio", "group")
