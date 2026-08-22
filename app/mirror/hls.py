"""HLS-Mirror-Engine.

Nimmt den Bildschirm mit GStreamer auf, kodiert nach H.264, segmentiert per
``hlssink2`` in eine .m3u8-Playlist + .ts-Segmente in einem Temp-Verzeichnis,
liefert diese über den lokalen Server aus und sagt dem Gerät, es solle die
Playlist als Live-Stream abspielen.

Robust und mit jedem Chromecast (und jedem AirPlay-TV) kompatibel; Preis ist die
Latenz (einige Sekunden), weil HLS puffert. Gut für Film/Präsentation, nicht für
Spiele. Der Systemton wird als AAC-Spur mitgesendet, sofern eine Monitor-Quelle
verfügbar ist – sonst läuft eine stille Spur mit, weil manche Receiver einen
reinen Video-Stream verwerfen.
"""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

from .capture import audio_source_desc, default_monitor_device, h264_encoder_desc
from .engine import MirrorEngine, gst_element_exists, gst_init


class HlsMirrorEngine(MirrorEngine):
    name = "hls"
    display_name = "HLS"

    def __init__(self, receiver, server, config, on_error=None) -> None:  # noqa: ANN001
        super().__init__(receiver, server, config, on_error)
        self._pipeline = None
        self._Gst = None
        self._tmpdir: str | None = None
        self._hls_url: str | None = None

    @staticmethod
    def check_available() -> tuple[bool, str]:
        from .capture import available_encoders, capture_available

        missing = [e for e in ("h264parse", "hlssink2") if not gst_element_exists(e)]
        if missing:
            return False, f"GStreamer-Elemente fehlen: {', '.join(missing)} (gst-plugins-bad)"
        if not available_encoders():
            return False, "Kein H.264-Encoder in GStreamer gefunden (gst-plugins-ugly)"
        ok, detail = capture_available()
        if not ok:
            return False, detail
        return True, "Bereit"

    # -- Audiospur ------------------------------------------------------------
    def _audio_branch(self) -> str:
        """GStreamer-Zweig für die AAC-Spur (Systemton oder Stille)."""
        src = audio_source_desc() if self.cfg("mirror_audio") else None
        if src is not None:
            device = default_monitor_device()
            if device and src.startswith("pulsesrc"):
                src = src.replace("pulsesrc ", f'pulsesrc device="{device}" ', 1)
        else:
            src = "audiotestsrc wave=silence is-live=true ! audioconvert ! audioresample"
        return f"{src} ! avenc_aac ! aacparse ! hls.audio"

    # -- Lauf (im Worker-Thread) ---------------------------------------------
    def run(self, source_desc: str) -> None:
        Gst = gst_init()
        self._Gst = Gst

        self._tmpdir = tempfile.mkdtemp(prefix="schwupp-hls-")
        width, height, fps, bitrate = self.video_params()
        encoder = h264_encoder_desc(bitrate, fps, str(self.cfg("mirror_encoder") or "auto"))
        tmp = Path(self._tmpdir)
        # URL gezielt für dieses Gerät bilden (richtiges Interface bei VPN/Docker).
        base_url = self.server.add_hls_dir(str(tmp), client_host=self.receiver.host)
        self._hls_url = base_url

        # Cast-kompatibler HLS-Stack (live getestet am LG-Cast-Receiver):
        #  * 16:9-Rahmen mit add-borders=true -> Seitenverhältnis erhalten (kein Verzerren)
        #  * H.264 constrained-baseline (breiteste Receiver-Kompatibilität)
        #  * AAC-Audiospur (manche Receiver verlangen Audio)
        #  * Master-Playlist mit CODECS (sonst erkennt der Receiver den Stream nicht)
        #  * CORS-Header liefert der Server; Segment-Vorlauf siehe unten
        # Latenz-optimiert: 1-s-Segmente, kurze Playlist, 1 Keyframe/s.
        desc = (
            f'hlssink2 name=hls target-duration=1 playlist-length=3 max-files=6 '
            f'playlist-location="{tmp/"playlist.m3u8"}" location="{tmp/"seg%05d.ts"}" '
            f'playlist-root="{base_url}" '
            f"{source_desc} ! videoscale add-borders=true ! videoconvert "
            f"! video/x-raw,width={width},height={height},pixel-aspect-ratio=1/1 "
            f"! {encoder} "
            f"! video/x-h264,profile=constrained-baseline ! h264parse ! hls.video "
            f"{self._audio_branch()}"
        )
        self._pipeline = Gst.parse_launch(desc)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)

        self._pipeline.set_state(Gst.State.PLAYING)

        # Warten, bis Playlist + erste Segmente bereit sind: Cast-Receiver gehen
        # sonst sofort auf IDLE, statt zu puffern.
        if not self._wait_for_segments(tmp):
            raise RuntimeError("HLS-Segmente wurden nicht erzeugt – "
                               "Bildschirmaufnahme oder Encoder liefert nichts")

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
    def _wait_for_segments(tmp: Path, min_segments: int = 2, timeout: float = 15.0) -> bool:
        import time

        playlist = tmp / "playlist.m3u8"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            segs = list(tmp.glob("*.ts"))
            if playlist.exists() and len(segs) >= min_segments:
                return True
            time.sleep(0.3)
        return False

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
        if self._hls_url:
            self.server.remove_hls_dir(self._hls_url)
            self._hls_url = None
        # App am TV beenden (-> zurück zum Home), nicht nur die Wiedergabe stoppen.
        # Cast-Geräte via quit_app; andere (AirPlay) über das generische stop().
        try:
            self.receiver.session.quit_app()
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self.receiver.stop()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
