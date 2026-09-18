"""Generisches AirPlay-2-Backend (Hisense/VIDAA, Samsung, Sony, Apple TV …).

Nutzt ``pyatv`` für Pairing und Wiedergabe: Beim ersten Verbinden zeigt der TV
eine PIN an, die in Schwupp eingegeben wird (``pin_cb``); die dabei erzeugten
Credentials landen in der Config (``airplay_creds``) – danach verbindet sich
die App ohne Rückfrage. Medien/Links laufen über AirPlay ``play_url`` (der TV
holt die URL selbst, HLS ist AirPlay-nativ) – darüber funktioniert auch die
HLS-Bildschirmspiegelung.

**Nicht jeder AirPlay-Fernseher kann Video.** Etliche Geräte (nachgemessen an
einem Hisense 75A5FE) koppeln sich einwandfrei, beantworten ``POST /play`` in
der aufgebauten Sitzung aber mit ``404`` und ``/rate``/``/setProperty`` mit
``404``/``501`` – obwohl ihre mDNS-Feature-Bits ``SupportsAirPlayVideoV2``
melden. Der Audio-Weg (RAOP) funktioniert bei genau diesen Geräten. Deshalb
wird der Fehlschlag hier erkannt und als verständliche Meldung weitergereicht,
statt als nichtssagender Zeitüberlauf; Audiodateien nehmen von vornherein den
Streaming-Weg. Details: docs/MIRRORING.md.

pyatv ist asyncio-basiert; die Receiver-API ist aber synchron und wird aus
GUI-Worker-Threads gerufen. Deshalb betreibt jede Instanz einen eigenen
Event-Loop-Thread und marshallt per ``run_coroutine_threadsafe``.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError

from .base import Feature, Receiver

_FEATURES = {Feature.MEDIA, Feature.PLAYBACK, Feature.MIRROR_HLS}

_SCAN_TIMEOUT = 6       # s, unicast-Scan auf bekannten Host
_CONNECT_TIMEOUT = 15   # s
_PLAY_TIMEOUT = 40      # s, play_url wartet bis zum Wiedergabestart
_PAIR_STEP_TIMEOUT = 30  # s, begin()/finish() des Pairings
_STREAM_START_TIMEOUT = 20  # s, bis der Audio-Stream angelaufen ist

# HTTP-Codes, mit denen ein Gerät sagt "diesen Weg kenne ich nicht".
_UNSUPPORTED_CODES = ("404", "501")


class AirplayReceiver(Receiver):
    kind = "airplay"

    def __init__(self, info, context) -> None:  # noqa: ANN001
        super().__init__(info, context)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._atv = None          # verbundenes pyatv-Interface
        self._conf = None         # pyatv BaseConfig des Geräts
        self._stream_future = None  # laufende Audio-Übertragung

    # -- Event-Loop-Thread -----------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or not self._thread or not self._thread.is_alive():
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, name="airplay-loop", daemon=True)
            self._thread.start()
        return self._loop

    def _run(self, coro, timeout: float):  # noqa: ANN001
        """Coroutine im Loop-Thread ausführen und synchron auf das Ergebnis warten.

        Wichtig ist die Unterscheidung zweier Zeitüberläufe: Läuft *unsere* Frist
        ab, antwortet das Gerät wirklich nicht. Wirft dagegen die Coroutine selbst
        einen ``TimeoutError``, ist das ein Fehler von pyatv – der wurde früher
        ebenfalls als "Gerät antwortet nicht" ausgegeben und verdeckte damit die
        eigentliche Ursache (z. B. eine abgelehnte Wiedergabe).
        """
        fut = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return fut.result(timeout)
        except FuturesTimeoutError:
            fut.cancel()
            raise RuntimeError("AirPlay-Gerät antwortet nicht (Zeitüberschreitung)") from None

    # -- Credentials-Ablage ------------------------------------------------------
    def _stored_creds(self) -> str | None:
        return (self.context.config["airplay_creds"] or {}).get(self.host)

    def _store_creds(self, creds: str | None) -> None:
        all_creds = self.context.config["airplay_creds"] or {}
        if creds:
            all_creds[self.host] = creds
        else:
            all_creds.pop(self.host, None)
        self.context.config["airplay_creds"] = all_creds
        self.context.config.save()

    # -- Verbindung ---------------------------------------------------------------
    def connect(self, prompt_cb=None, pin_cb=None) -> None:  # noqa: ANN001
        import pyatv
        from pyatv import exceptions
        from pyatv.const import Protocol

        loop = self._ensure_loop()

        async def _scan():
            confs = await pyatv.scan(loop, hosts=[self.host],
                                     protocol=Protocol.AirPlay,
                                     timeout=_SCAN_TIMEOUT)
            if not confs:
                raise RuntimeError("Kein AirPlay-Dienst am Gerät gefunden")
            return confs[0]

        self._conf = self._run(_scan(), _SCAN_TIMEOUT + 5)

        creds = self._stored_creds()
        if creds:
            try:
                self._connect_with(creds)
                return
            except exceptions.AuthenticationError:
                # TV wurde zurückgesetzt / Key gelöscht -> neu pairen
                self._store_creds(None)

        creds = self._pair(prompt_cb, pin_cb)
        self._connect_with(creds)

    def _connect_with(self, creds: str | None) -> None:
        import pyatv
        from pyatv.const import Protocol

        loop = self._loop

        async def _connect():
            if creds:
                self._conf.set_credentials(Protocol.AirPlay, creds)
            return await pyatv.connect(self._conf, loop, protocol=Protocol.AirPlay)

        self._atv = self._run(_connect(), _CONNECT_TIMEOUT)

    def _pair(self, prompt_cb, pin_cb) -> str | None:  # noqa: ANN001
        """PIN-Pairing: TV zeigt die PIN, ``pin_cb`` liefert die Eingabe (blockiert)."""
        import pyatv
        from pyatv.const import Protocol

        loop = self._loop

        async def _begin():
            pairing = await pyatv.pair(self._conf, Protocol.AirPlay, loop)
            await pairing.begin()
            return pairing

        pairing = self._run(_begin(), _PAIR_STEP_TIMEOUT)
        try:
            if pairing.device_provides_pin:
                if pin_cb is None:
                    raise RuntimeError("PIN-Eingabe nötig, aber kein Dialog verfügbar")
                pin = pin_cb()          # blockiert, bis der Nutzer die TV-PIN eintippt
                if not pin:
                    raise RuntimeError("Pairing abgebrochen")
                pairing.pin(pin.strip())
            else:
                # Seltener Fall: WIR stellen die PIN, Eingabe erfolgt am Gerät.
                pairing.pin(1234)
                if prompt_cb:
                    prompt_cb()

            async def _finish():
                await pairing.finish()
                return pairing.service.credentials if pairing.has_paired else None

            creds = self._run(_finish(), _PAIR_STEP_TIMEOUT)
            if creds is None:
                raise RuntimeError("Pairing fehlgeschlagen (PIN falsch?)")
            self._store_creds(creds)
            return creds
        finally:
            with contextlib.suppress(Exception):
                self._run(pairing.close(), 10)

    def disconnect(self) -> None:
        self._cancel_stream()
        atv, self._atv = self._atv, None
        loop, self._loop = self._loop, None
        thread, self._thread = self._thread, None
        if loop is None:
            return
        if atv is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(
                    self._close_atv(atv), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)

    @staticmethod
    async def _close_atv(atv) -> None:  # noqa: ANN001
        atv.close()

    # -- Inhalte --------------------------------------------------------------
    def play_media(self, url, mime, *, title=None, live=False) -> None:  # noqa: ANN001
        if self._atv is None:
            # Session wurde per stop() beendet -> mit gespeicherten Creds neu verbinden
            self._connect_with(self._stored_creds())
        if mime.startswith("audio/"):
            self._stream_audio(url)
            return
        try:
            self._run(self._atv.stream.play_url(url), _PLAY_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            raise self._explain(exc) from exc

    def _stream_audio(self, url: str) -> None:
        """Audiodatei über den RAOP-Weg senden.

        ``stream_file`` läuft bis zum Ende der Datei; hier wird deshalb nur
        abgewartet, ob der Start gelingt – danach läuft die Übertragung im
        Hintergrund weiter und wird von :meth:`stop` abgebrochen.
        """
        self._cancel_stream()
        future = asyncio.run_coroutine_threadsafe(
            self._atv.stream.stream_file(url), self._ensure_loop())
        self._stream_future = future
        try:
            future.result(2)        # scheitert der Start, kommt der Fehler hier
        except FuturesTimeoutError:
            pass                    # läuft noch – genau das ist der Normalfall
        except Exception as exc:    # noqa: BLE001
            self._stream_future = None
            raise self._explain(exc) from exc

    def _cancel_stream(self) -> None:
        future, self._stream_future = getattr(self, "_stream_future", None), None
        if future is not None and not future.done():
            future.cancel()

    @staticmethod
    def _explain(exc: Exception) -> RuntimeError:
        """Übersetzt die Absage eines Geräts in eine Meldung, die weiterhilft.

        Antwortet der Fernseher auf den Wiedergabe-Befehl mit 404 oder 501,
        kennt er den AirPlay-Video-Weg nicht – auch wenn er ihn in seinen
        Feature-Bits bewirbt. Ohne diese Übersetzung landete beim Nutzer nur
        eine Zeitüberschreitung oder ein roher HTTP-Fehler.
        """
        text = str(exc)
        if any(code in text for code in _UNSUPPORTED_CODES):
            return RuntimeError(
                "Dieser Fernseher nimmt über AirPlay keine Videos an – er kennt "
                "den Wiedergabe-Befehl nicht. Musik lässt sich trotzdem senden.")
        return RuntimeError(f"AirPlay-Wiedergabe fehlgeschlagen: {exc}")

    # -- Steuerung (best effort; nicht jeder AirPlay-TV kann Remote-Befehle) ---
    def _remote(self, action: str) -> None:
        if self._atv is None:
            return
        # z. B. NotSupportedError – manche Geräte können nur Wiedergabe
        with contextlib.suppress(Exception):
            self._run(getattr(self._atv.remote_control, action)(), 10)

    def play(self) -> None:
        self._remote("play")

    def pause(self) -> None:
        self._remote("pause")

    def stop(self) -> None:
        self._cancel_stream()
        # stop() geht nicht überall; das Schließen der AirPlay-Session beendet
        # die Wiedergabe zuverlässig (TV kehrt zum vorherigen Bild zurück).
        # Neu verbunden wird lazy beim nächsten play_media – stop() kann vom
        # GTK-Hauptthread kommen und darf nicht lange blockieren.
        self._remote("stop")
        if self._atv is not None and self._loop is not None:
            atv, self._atv = self._atv, None
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(
                    self._close_atv(atv), self._loop).result(5)

    def supports(self, feature: str) -> bool:
        return feature in _FEATURES
