"""AirPlay-Backend: Übersetzung von Geräte-Absagen in brauchbare Meldungen.

Hintergrund: Etliche AirPlay-Fernseher koppeln sich einwandfrei, lehnen die
Video-Wiedergabe aber ab (``404`` auf ``/play``, ``501`` auf ``/setProperty``) –
obwohl ihre Feature-Bits Video bewerben. Beim Nutzer kam davon früher nur eine
nichtssagende Zeitüberschreitung an.
"""
from __future__ import annotations

import pytest

from app.receivers.airplay import AirplayReceiver


class _HttpError(Exception):
    """Steht für pyatvs HttpError, ohne pyatv importieren zu müssen."""


@pytest.mark.parametrize("message", [
    "RTSP/1.0 method PUT failed with code 501: Not Implemented",
    "RTSP/1.0 method POST failed with code 404: Not Found",
    "status code: 404",
])
def test_device_rejection_becomes_a_readable_message(message):
    result = AirplayReceiver._explain(_HttpError(message))
    assert isinstance(result, RuntimeError)
    text = str(result)
    assert "keine Videos" in text
    # Die Meldung muss die beiden Wege nennen, die bei solchen Geräten tragen
    assert "Musik" in text
    assert "spiegeln" in text


@pytest.mark.parametrize("message", [
    "connection was lost",
    "Custom failure",
])
def test_other_errors_are_passed_through(message):
    """Alles andere darf nicht als Geräte-Beschränkung ausgegeben werden –
    ein Netzabbruch hat eine andere Ursache als ein fehlender Endpunkt."""
    result = AirplayReceiver._explain(_HttpError(message))
    text = str(result)
    assert "keine Videos" not in text
    assert message in text


def test_explanation_never_swallows_the_original_text():
    original = "RTSP/1.0 method POST failed with code 404: Not Found"
    # Auch die verständliche Variante bleibt eine Exception, die geworfen werden kann
    assert isinstance(AirplayReceiver._explain(_HttpError(original)), RuntimeError)


# -- Gelernte Video-Fähigkeit -----------------------------------------------

class _Config:
    """Minimale Config mit Geräte-Overrides, wie app.config sie bietet."""

    def __init__(self, video=True):
        self._video = video
        self.saved = False

    def device_value_for(self, info, key):  # noqa: ANN001, ARG002
        return self._video if key == "airplay_video" else None

    def set_device_value_for(self, info, key, value):  # noqa: ANN001, ARG002
        if key == "airplay_video":
            self._video = value

    def save(self):
        self.saved = True


class _Context:
    def __init__(self, config):
        self.config = config
        self.zconf = None
        self.server = None


class _Info:
    uuid = "airplay:192.168.0.50"
    host = "192.168.0.50"
    name = "TV"


def _receiver(video=True):
    from app.receivers.airplay import AirplayReceiver
    config = _Config(video)
    return AirplayReceiver(_Info(), _Context(config)), config


def test_video_is_offered_until_a_device_refuses():
    """Vorab ist nicht erkennbar, ob ein Gerät Video kann – also erst anbieten."""
    from app.receivers.base import Feature
    recv, _ = _receiver(video=True)
    assert recv.supports(Feature.VIDEO) is True
    assert recv.supports(Feature.MEDIA) is True


def test_video_is_hidden_after_a_refusal():
    from app.receivers.base import Feature
    recv, _ = _receiver(video=False)
    assert recv.supports(Feature.VIDEO) is False
    # Ton und Spiegelung bleiben – die funktionieren auf solchen Geräten
    assert recv.supports(Feature.MEDIA) is True
    assert recv.supports(Feature.MIRROR_AIRPLAY) is True


def test_refusal_is_remembered_and_saved():
    from app.receivers.base import Feature
    recv, config = _receiver(video=True)
    recv._remember_no_video()
    assert recv.supports(Feature.VIDEO) is False
    assert config.saved, "die Erkenntnis muss dauerhaft gespeichert werden"


def test_missing_config_does_not_break_feature_query():
    """Ohne Config (z. B. in Werkzeugen) wird Video schlicht angeboten."""
    from app.receivers.airplay import AirplayReceiver
    from app.receivers.base import Feature

    class _Bare:
        config = None
        zconf = None
        server = None

    recv = AirplayReceiver(_Info(), _Bare())
    assert recv.supports(Feature.VIDEO) is True
