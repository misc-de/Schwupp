"""Den ganzen Ton des Rechners an einen Lautsprecher schicken.

Zwei Wege, je nach Gerät:

* **Live-Stream** (Chromecast, Cast-Lautsprecher und -Gruppen, DLNA, Sonos):
  Eine virtuelle Ausgabe wird Standard, ihr Monitor wird als MP3 (320 kbit/s
  CBR – das spielt jeder Renderer) kodiert und über den lokalen Server als
  endloser Stream angeboten; der Lautsprecher holt ihn selbst ab, wie ein
  Internetradio. Verzögerung je nach Gerät 2–4 s. Sonos nimmt so einen Stream
  nur unter seinem Radio-Schema ``x-rincon-mp3radio://`` an (aus Emilia).
* **AirPlay** (RAOP): PipeWires RAOP-Ausgabe wird Standard und schiebt den Ton
  selbst zum Lautsprecher – kein Kodieren in Schwupp.

Start und Fehler laufen wie bei den Spiegel-Engines über Callbacks aus einem
Worker-Thread.
"""
from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable

from ..i18n import t
from ..mirror.engine import gst_element_exists, gst_init
from .pipewire import NullSink, RaopSink, pactl_available, raop_available

MP3_BITRATE = 320
# Holt der Lautsprecher den Stream so lange nicht ab, ist etwas schiefgegangen
FETCH_TIMEOUT_S = 20


def check_available(kind: str) -> tuple[bool, str]:
    """(nutzbar?, Hinweis) für den Ton an ein Gerät dieses Typs."""
    if not pactl_available():
        return False, t("sound.missing_pactl")
    if kind == "raop":
        if not raop_available():
            return False, t("sound.missing_raop")
        return True, ""
    missing = [e for e in ("pulsesrc", "lamemp3enc") if not gst_element_exists(e)]
    if missing:
        return False, t("sound.missing_gst", elements=", ".join(missing))
    return True, ""


class SoundCast:
    """Eine laufende Übertragung des Systemtons an *receiver*."""

    def __init__(self, receiver, server, on_error: Callable[[str], None] | None = None,
                 on_started: Callable[[], None] | None = None) -> None:  # noqa: ANN001
        self.receiver = receiver
        self.server = server
        self._on_error = on_error
        self._on_started = on_started
        self._running = False
        self._sink: NullSink | RaopSink | None = None
        self._pipeline = None
        self._Gst = None
        self._live = None
        self._live_url: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    def _description(self) -> str:
        """So heißt die Ausgabe in den Ton-Einstellungen: man sieht, wohin der
        Ton gerade geht."""
        return f"Schwupp → {self.receiver.name}"

    # -- Lebenszyklus ---------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._run_guarded, daemon=True, name="soundcast").start()

    def _run_guarded(self) -> None:
        try:
            if self.receiver.kind == "raop":
                self._run_raop()
            else:
                self._run_stream()
        except Exception as exc:  # noqa: BLE001
            self.fail(exc)
            return
        if self._running and self._on_started is not None:
            self._on_started()

    def fail(self, message) -> None:  # noqa: ANN001
        self._running = False
        with contextlib.suppress(Exception):
            self.stop()
        if self._on_error is not None:
            self._on_error(str(message))
        else:
            print(f"[sound] {message}")

    def stop(self) -> None:
        """Beendet die Übertragung, die Ausgabe des Rechners kommt zurück."""
        self._running = False
        if self._pipeline is not None:
            with contextlib.suppress(Exception):
                self._pipeline.set_state(self._Gst.State.NULL)
            self._pipeline = None
        if self.receiver.kind != "raop":
            with contextlib.suppress(Exception):
                self.receiver.stop()
        if self._live_url:
            self.server.remove_live(self._live_url)
            self._live_url = None
            self._live = None
        sink, self._sink = self._sink, None
        if sink is not None:
            sink.stop()

    # -- AirPlay --------------------------------------------------------------
    def _run_raop(self) -> None:
        self._sink = RaopSink(self.receiver.host, self.receiver.info.port, self._description())
        self._sink.start()
        threading.Thread(target=self._watch_raop, daemon=True, name="soundcast-raop").start()

    def _watch_raop(self) -> None:
        """Endet PipeWires AirPlay-Prozess von selbst, ist die Übertragung vorbei."""
        while self._running:
            sink = self._sink
            if sink is None:
                return
            if not sink.alive:
                self.fail(t("sound.raop_ended"))
                return
            time.sleep(1)

    # -- Live-Stream ----------------------------------------------------------
    def _run_stream(self) -> None:
        Gst = gst_init()
        self._Gst = Gst
        self._sink = NullSink(self._description())
        self._sink.start()
        self._live_url, self._live = self.server.add_live(
            "audio/mpeg", client_host=self.receiver.host)
        desc = (
            f'pulsesrc device="{self._sink.monitor}" provide-clock=false do-timestamp=true '
            "! audio/x-raw,channels=2 ! audioconvert ! audioresample "
            "! audio/x-raw,rate=44100,channels=2 "
            f"! lamemp3enc target=bitrate cbr=true bitrate={MP3_BITRATE} "
            "! appsink name=sink emit-signals=true sync=false max-buffers=64 drop=true"
        )
        self._pipeline = Gst.parse_launch(desc)
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_gst_error)
        self._pipeline.set_state(Gst.State.PLAYING)

        self.receiver.play_live_audio(self._live_url, t("sound.title", host=socket.gethostname()))
        deadline = time.monotonic() + FETCH_TIMEOUT_S
        while self._running and self._live is not None and self._live.consumer_count == 0:
            if time.monotonic() > deadline:
                raise RuntimeError(t("sound.not_fetched"))
            time.sleep(0.3)

    def _on_sample(self, sink):  # noqa: ANN001
        sample = sink.emit("pull-sample")
        live = self._live
        if sample is not None and live is not None:
            buf = sample.get_buffer()
            ok, info = buf.map(self._Gst.MapFlags.READ)
            if ok:
                try:
                    live.write(bytes(info.data))
                finally:
                    buf.unmap(info)
        return self._Gst.FlowReturn.OK

    def _on_gst_error(self, _bus, message) -> None:  # noqa: ANN001
        err, _dbg = message.parse_error()
        self.fail(t("sound.gst_error", error=err.message))
