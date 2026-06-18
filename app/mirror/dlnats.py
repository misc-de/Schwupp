"""DLNA-Live-Stream-Mirror-Engine (für LG webOS u. a. DLNA-Renderer).

Nimmt den Bildschirm auf, kodiert nach H.264, multiplext fortlaufend nach
MPEG-TS und schiebt die Bytes über einen Live-Endpoint des lokalen Servers an
den TV (DLNA ``SetAVTransportURI`` auf eine endlose ``video/mp2t``-URL).

Im Gegensatz zu HLS gibt es keine Playlist/Segmentdateien – ein einziger,
nie endender HTTP-Stream. Latenz typischerweise ~1–3 s (deutlich besser als
HLS), rein in der App, ohne Fremd-Tool.
"""
from __future__ import annotations

from .capture import (PortalScreenCast, is_wayland, pipewire_source_desc,
                      x11_source_desc)
from .engine import MirrorEngine, gst_element_exists


class DlnaTsMirrorEngine(MirrorEngine):
    name = "dlnats"
    display_name = "DLNA Live-Stream"

    def __init__(self, receiver, server, config) -> None:  # noqa: ANN001
        super().__init__(receiver, server, config)
        self._pipeline = None
        self._live = None
        self._live_url: str | None = None
        self._portal: PortalScreenCast | None = None

    @staticmethod
    def check_available() -> tuple[bool, str]:
        missing = [e for e in ("x264enc", "h264parse", "mpegtsmux")
                   if not gst_element_exists(e)]
        if missing:
            return False, f"GStreamer-Elemente fehlen: {', '.join(missing)}"
        return True, "Bereit"

    # -- Start ---------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        fps = int(self.cfg("mirror_fps"))
        if is_wayland():
            self._portal = PortalScreenCast(fps=fps)
            self._portal.start(self._on_portal_ready)
        else:
            self._launch(x11_source_desc(fps=fps))

    def _on_portal_ready(self, fd, node_or_err) -> None:  # noqa: ANN001
        if fd is None:
            raise RuntimeError(f"Bildschirmfreigabe nicht möglich: {node_or_err}")
        self._launch(pipewire_source_desc(fd, node_or_err, int(self.cfg("mirror_fps"))))

    def _launch(self, source_desc: str) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)
        self._Gst = Gst

        bitrate = int(self.cfg("mirror_bitrate_kbps"))
        fps = int(self.cfg("mirror_fps"))
        self._live_url, self._live = self.server.add_live("video/mp2t")

        desc = (
            f"{source_desc} "
            f"! x264enc tune=zerolatency speed-preset=veryfast bitrate={bitrate} "
            f"key-int-max={fps} "             # ~1 Keyframe/s -> schneller Einstieg
            f"! h264parse ! mpegtsmux alignment=7 "
            f"! appsink name=sink emit-signals=true sync=false max-buffers=16 drop=true"
        )
        self._pipeline = Gst.parse_launch(desc)
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)

        self._pipeline.set_state(Gst.State.PLAYING)
        self._running = True
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

    def _on_error(self, bus, message) -> None:  # noqa: ANN001
        err, dbg = message.parse_error()
        print(f"[dlnats] GStreamer-Fehler: {err} – {dbg}")
        self.stop()

    # -- Stop ----------------------------------------------------------------
    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._pipeline = None
        try:
            self.receiver.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._live_url:
            self.server.remove_live(self._live_url)
            self._live_url = None
            self._live = None
        self._running = False
