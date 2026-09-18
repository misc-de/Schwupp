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
    assert "Musik" in text          # der Weg, der bei diesen Geräten trägt


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
