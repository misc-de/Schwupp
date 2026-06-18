"""Austauschbare Engines fürs Bildschirm-Spiegeln.

* ``native``     – eigenes Cast-Streaming (RTP/Offer-Answer, geringe Latenz)
* ``hls``        – GStreamer → HLS → Default Media Receiver (robust, träge)
* ``openscreen`` – externes openscreen ``cast_sender``-Binary

Auswahl über die Einstellungen (siehe :mod:`app.config`).
"""
from .engine import (  # noqa: F401
    MirrorEngine,
    available_engines,
    engines_for_kind,
    get_engine_class,
)
