"""Generisches AirPlay-2-Backend (Hisense/VIDAA, Samsung, Sony, Apple TV …).

Nutzt ``pyatv`` für Pairing und Wiedergabe: Beim ersten Verbinden zeigt der TV
eine PIN an, die in Schwupp eingegeben wird (``pin_cb``); die dabei erzeugten
Credentials landen in der Config (``airplay_creds``) – danach verbindet sich
die App ohne Rückfrage. Medien/Links laufen über AirPlay ``play_url`` (der TV
holt die URL selbst, HLS ist AirPlay-nativ) – darüber funktioniert auch die
HLS-Bildschirmspiegelung.

pyatv ist asyncio-basiert; die Receiver-API ist aber synchron und wird aus
GUI-Worker-Threads gerufen. Deshalb betreibt jede Instanz einen eigenen
Event-Loop-Thread und marshallt per ``run_coroutine_threadsafe``.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading

from .base import Feature, Receiver

_FEATURES = {Feature.MEDIA, Feature.PLAYBACK, Feature.MIRROR_HLS}

_SCAN_TIMEOUT = 6       # s, unicast-Scan auf bekannten Host
_CONNECT_TIMEOUT = 15   # s
_PLAY_TIMEOUT = 40      # s, play_url wartet bis zum Wiedergabestart
_PAIR_STEP_TIMEOUT = 30  # s, begin()/finish() des Pairings


class AirplayReceiver(Receiver):
    kind = "airplay"

    def __init__(self, info, context) -> None:  # noqa: ANN001
        super().__init__(info, context)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._atv = None          # verbundenes pyatv-Interface
        self._conf = None         # pyatv BaseConfig des Geräts

    # -- Event-Loop-Thread -----------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or not self._thread or not self._thread.is_alive():
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, name="airplay-loop", daemon=True)
            self._thread.start()
        return self._loop

    def _run(self, coro, timeout: float):  # noqa: ANN001
        """Coroutine im Loop-Thread ausführen und synchron auf das Ergebnis warten."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise RuntimeError("AirPlay-Gerät antwortet nicht (Timeout)") from None

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
        self._run(self._atv.stream.play_url(url), _PLAY_TIMEOUT)

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
