"""Austauschbare Engines fürs Bildschirm-Spiegeln.

* ``native``  – eigenes Cast-Streaming (RTP/Offer-Answer, <1 s Latenz, mit Ton)
* ``hls``     – GStreamer → HLS → Default Media Receiver (robust, träge)
* ``dlnats``  – endloser MPEG-TS-Live-Stream an DLNA-Renderer

Auswahl über die Einstellungen (siehe :mod:`app.config`).
"""
from .engine import (  # noqa: F401
    MirrorEngine,
    available_engines,
    engines_for_kind,
    get_engine_class,
)
