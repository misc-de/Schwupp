"""DLNA-Live-Stream-Mirror-Engine (für DLNA-Renderer mit Live-Unterstützung).

Nimmt den Bildschirm auf, kodiert nach H.264, multiplext fortlaufend nach
MPEG-TS und schiebt die Bytes über einen Live-Endpoint des lokalen Servers an
den TV (DLNA ``SetAVTransportURI`` auf eine endlose ``video/mp2t``-URL).

Im Gegensatz zu HLS gibt es keine Playlist/Segmentdateien – ein einziger,
nie endender HTTP-Stream. Latenz typischerweise ~1–3 s (deutlich besser als
HLS), rein in der App, ohne Fremd-Tool.

Achtung: Viele TVs (insbesondere LG webOS) verwerfen größenlose Live-Streams –
siehe docs/MIRRORING.md. Die Engine steht deshalb nur für Geräte zur Auswahl,
bei denen sie erfahrungsgemäß trägt.
"""
from __future__ import annotations

import contextlib

from .capture import audio_source_desc, default_monitor_device, h264_encoder_desc
from .engine import MirrorEngine, gst_element_exists, gst_init


class DlnaTsMirrorEngine(MirrorEngine):
    name = "dlnats"
    display_name = "DLNA Live-Stream"

    def __init__(self, receiver, server, config, on_error=None) -> None:  # noqa: ANN001
        super().__init__(receiver, server, config, on_error)
        self._pipeline = None
        self._Gst = None
        self._live = None
        self._live_url: str | None = None

    @staticmethod
    def check_available() -> tuple[bool, str]:
        from .capture import available_encoders, capture_available

        missing = [e for e in ("h264parse", "mpegtsmux") if not gst_element_exists(e)]
        if missing:
            return False, f"GStreamer-Elemente fehlen: {', '.join(missing)}"
        if not available_encoders():
            return False, "Kein H.264-Encoder in GStreamer gefunden (gst-plugins-ugly)"
        ok, detail = capture_available()
        if not ok:
            return False, detail
        return True, "Bereit"

    def _audio_branch(self) -> str:
        """AAC-Zweig in den TS-Mux (Systemton), oder leer ohne Audioquelle."""
        if not self.cfg("mirror_audio") or not gst_element_exists("avenc_aac"):
            return ""
        src = audio_source_desc()
        if src is None:
            return ""
        device = default_monitor_device()
        if device and src.startswith("pulsesrc"):
            src = src.replace("pulsesrc ", f'pulsesrc device="{device}" ', 1)
        return f" {src} ! avenc_aac ! aacparse ! mux."

    # -- Lauf (im Worker-Thread) ---------------------------------------------
    def run(self, source_desc: str) -> None:
        Gst = gst_init()
        self._Gst = Gst

        _width, _height, fps, bitrate = self.video_params()
        encoder = h264_encoder_desc(bitrate, fps, str(self.cfg("mirror_encoder") or "auto"))
        self._live_url, self._live = self.server.add_live(
            "video/mp2t", client_host=self.receiver.host)

        desc = (
            f"{source_desc} "
            f"! {encoder} "
            f"! h264parse ! mpegtsmux name=mux alignment=7 "
            f"! appsink name=sink emit-signals=true sync=false max-buffers=16 drop=true"
            f"{self._audio_branch()}"
        )
        self._pipeline = Gst.parse_launch(desc)
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)

        self._pipeline.set_state(Gst.State.PLAYING)
        # TV den Live-Stream öffnen lassen (DLNA SetAVTransportURI + Play)
        self.receiver.play_media(
            self._live_url, "video/mp2t", title="Bildschirm (Schwupp)", live=True
        )

    def _on_sample(self, sink):  # noqa: ANN001
        sample = sink.emit("pull-sample")
        if sample is not None and self._live is not None:
            buf = sample.get_buffer()
            ok, info = buf.map(self._Gst.MapFlags.READ)
            if ok:
                try:
                    self._live.write(bytes(info.data))
                finally:
                    buf.unmap(info)
        return self._Gst.FlowReturn.OK

    def _on_error(self, _bus, message) -> None:  # noqa: ANN001
        err, _dbg = message.parse_error()
        self.fail(f"GStreamer-Fehler beim Spiegeln: {err}")

    # -- Stop ----------------------------------------------------------------
    def stop(self) -> None:
        self._running = False
        if self._pipeline is not None:
            with contextlib.suppress(Exception):
                self._pipeline.set_state(self._Gst.State.NULL)
            self._pipeline = None
        self._stop_capture()
        with contextlib.suppress(Exception):
            self.receiver.stop()
        if self._live_url:
            self.server.remove_live(self._live_url)
            self._live_url = None
            self._live = None
