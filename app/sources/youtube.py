"""YouTube- und generische Web-Video-Quelle.

Zwei Wege:

* **Native YouTube-App** (bevorzugt für YouTube-Links): über pychromecasts
  ``YouTubeController`` (casttube) – startet die echte YouTube-App auf dem
  Gerät und spielt die Video-ID. Beste Qualität, Warteschlange, Untertitel.
* **yt-dlp-Fallback** (für sonstige Seiten oder wenn die App nicht geht):
  extrahiert eine direkte, Chromecast-kompatible Stream-URL, die dann über den
  Default Media Receiver abgespielt wird.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}


def parse_video_id(url: str) -> str | None:
    """Extrahiert die YouTube-Video-ID aus den gängigen URL-Formen, sonst None."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host not in _YT_HOSTS:
        return None
    if host == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
        return vid or None
    if u.path == "/watch":
        return parse_qs(u.query).get("v", [None])[0]
    m = re.match(r"^/(?:embed|shorts|live|v)/([^/?#]+)", u.path)
    if m:
        return m.group(1)
    return None


@dataclass
class ResolvedStream:
    url: str
    mime: str
    title: str | None


def yt_dlp_available() -> bool:
    return shutil.which("yt-dlp") is not None


def resolve_stream(page_url: str, max_height: int = 1080) -> ResolvedStream:
    """Ermittelt mit yt-dlp eine direkte, abspielbare Stream-URL.

    Bevorzugt ein progressives MP4 (Audio+Video in einer Datei) – das spielt der
    Chromecast Default Media Receiver am zuverlässigsten ab.
    """
    if not yt_dlp_available():
        raise RuntimeError("yt-dlp ist nicht installiert")

    fmt = (
        f"best[ext=mp4][vcodec!=none][acodec!=none][height<=?{max_height}]/"
        f"best[vcodec!=none][acodec!=none]/best"
    )
    out = subprocess.run(
        ["yt-dlp", "-f", fmt, "-j", page_url],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"yt-dlp fehlgeschlagen: {out.stderr.strip()[:300]}")
    meta = json.loads(out.stdout)
    url = meta.get("url")
    if not url:
        raise RuntimeError("yt-dlp lieferte keine direkte URL")
    ext = meta.get("ext", "")
    mime = {
        "mp4": "video/mp4",
        "webm": "video/webm",
        "m4a": "audio/mp4",
        "mp3": "audio/mpeg",
    }.get(ext, "video/mp4")
    return ResolvedStream(url=url, mime=mime, title=meta.get("title"))
