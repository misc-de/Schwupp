"""HLS-Mirror-Engine.

Nimmt den Bildschirm mit GStreamer auf, kodiert nach H.264, segmentiert per
``hlssink2`` in eine .m3u8-Playlist + .ts-Segmente in einem Temp-Verzeichnis,
liefert diese über den lokalen Server aus und sagt dem Cast-Gerät, es solle die
Playlist als Live-Stream abspielen.

Robust und mit jedem Chromecast kompatibel; Preis ist die Latenz (einige
Sekunden), weil HLS puffert. Gut für Film/Präsentation, nicht für Spiele.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .capture import (
    PortalScreenCast,
    is_wayland,
    pipewire_source_desc,
    x11_source_desc,
)
from .engine import MirrorEngine, gst_element_exists


class HlsMirrorEngine(MirrorEngine):
    name = "hls"
    display_name = "HLS (kompatibel, etwas träge)"

    def __init__(self, receiver, server, config) -> None:  # noqa: ANN001
        super().__init__(receiver, server, config)
        self._pipeline = None
        self._tmpdir: str | None = None
        self._portal: PortalScreenCast | None = None

    @staticmethod
    def check_available() -> tuple[bool, str]:
        missing = [e for e in ("x264enc", "h264parse", "hlssink2") if not gst_element_exists(e)]
        if missing:
            return False, f"GStreamer-Elemente fehlen: {', '.join(missing)} (gst-plugins-bad/ugly)"
        return True, "Bereit"

    # -- Start ---------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._tmpdir = tempfile.mkdtemp(prefix="schwupp-hls-")
        fps = int(self.config["mirror_fps"])
        if is_wayland():
            # Asynchroner Portal-Handshake; Pipeline entsteht im Callback.
            self._portal = PortalScreenCast(fps=fps)
            self._portal.start(self._on_portal_ready)
        else:
            self._launch(x11_source_desc(fps=fps))

    def _on_portal_ready(self, fd, node_or_err) -> None:  # noqa: ANN001
        if fd is None:
            raise RuntimeError(f"Bildschirmfreigabe nicht möglich: {node_or_err}")
        fps = int(self.config["mirror_fps"])
        self._launch(pipewire_source_desc(fd, node_or_err, fps=fps))

    def _launch(self, source_desc: str) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)

        bitrate = int(self.config["mirror_bitrate_kbps"])
        fps = int(self.config["mirror_fps"])
        height = int(self.config["mirror_height"])
        width = (height * 16 // 9) // 2 * 2   # 16:9-Zielrahmen (gerade Zahl)
        tmp = Path(self._tmpdir)
        base_url = self.server.set_hls_dir(str(tmp))  # liefert ".../hls/"

        # Cast-kompatibler HLS-Stack (live getestet am LG-Cast-Receiver):
        #  * 16:9-Rahmen mit add-borders=true -> Seitenverhältnis erhalten (kein Verzerren)
        #  * H.264 constrained-baseline (breiteste Receiver-Kompatibilität)
        #  * stille AAC-Audiospur (manche Receiver verlangen Audio)
        #  * Master-Playlist mit CODECS (sonst erkennt der Receiver den Stream nicht)
        #  * CORS-Header liefert der Server; Segment-Vorlauf siehe unten
        # Latenz-optimiert: 1-s-Segmente, kurze Playlist, 1 Keyframe/s.
        desc = (
            f'hlssink2 name=hls target-duration=1 playlist-length=3 max-files=6 '
            f'playlist-location="{tmp/"playlist.m3u8"}" location="{tmp/"seg%05d.ts"}" '
            f'playlist-root="{base_url}" '
            f"{source_desc} ! videoscale add-borders=true ! videoconvert "
            f"! video/x-raw,width={width},height={height},pixel-aspect-ratio=1/1 "
            f"! x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate} "
            f"key-int-max={fps} "
            f"! video/x-h264,profile=constrained-baseline ! h264parse ! hls.video "
            f"audiotestsrc wave=silence is-live=true ! audioconvert ! audioresample "
            f"! avenc_aac ! aacparse ! hls.audio"
        )
        self._pipeline = Gst.parse_launch(desc)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)

        self._pipeline.set_state(Gst.State.PLAYING)
        self._running = True

        # Warten, bis Playlist + erste Segmente bereit sind: Cast-Receiver gehen
        # sonst sofort auf IDLE, statt zu puffern.
        self._wait_for_segments(tmp)

        # Master-Playlist mit Codec-Deklaration schreiben (H.264 CBP Lvl 4.0 + AAC-LC)
        (tmp / "master.m3u8").write_text(
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            f'#EXT-X-STREAM-INF:BANDWIDTH={bitrate * 1000 + 200000},'
            f'RESOLUTION={width}x{height},CODECS="avc1.42e028,mp4a.40.2"\n'
            "playlist.m3u8\n"
        )
        # Master-Stream am Gerät starten
        self.receiver.play_media(
            f"{base_url}master.m3u8",
            "application/vnd.apple.mpegurl",
            title="Bildschirm (Schwupp)",
            live=True,
        )

    @staticmethod
    def _wait_for_segments(tmp: Path, min_segments: int = 2, timeout: float = 15.0) -> None:
        import time

        playlist = tmp / "playlist.m3u8"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            segs = list(tmp.glob("*.ts"))
            if playlist.exists() and len(segs) >= min_segments:
                return
            time.sleep(0.3)

    def _on_error(self, bus, message) -> None:  # noqa: ANN001
        err, dbg = message.parse_error()
        print(f"[hls] GStreamer-Fehler: {err} – {dbg}")
        self.stop()

    # -- Stop ----------------------------------------------------------------
    def stop(self) -> None:
        if self._pipeline is not None:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        try:
            self.receiver.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        self._running = False
