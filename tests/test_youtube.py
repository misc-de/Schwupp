"""Erkennung von YouTube-Links (entscheidet, ob die TV-App statt yt-dlp läuft)."""
from __future__ import annotations

import pytest

from app.sources import youtube
from app.sources.youtube import parse_video_id

VIDEO_ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VIDEO_ID}",
    f"https://youtube.com/watch?v={VIDEO_ID}",
    f"https://m.youtube.com/watch?v={VIDEO_ID}",
    f"https://music.youtube.com/watch?v={VIDEO_ID}",
    f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123&index=2",
    f"https://youtu.be/{VIDEO_ID}",
    f"https://youtu.be/{VIDEO_ID}?t=42",
    f"https://www.youtube.com/embed/{VIDEO_ID}",
    f"https://www.youtube.com/shorts/{VIDEO_ID}",
    f"https://www.youtube.com/live/{VIDEO_ID}",
    f"https://www.youtube.com/v/{VIDEO_ID}",
])
def test_recognises_all_common_url_shapes(url):
    assert parse_video_id(url) == VIDEO_ID


@pytest.mark.parametrize("url", [
    "https://vimeo.com/123456",
    "https://example.com/watch?v=abc",
    "https://www.youtube.com/",
    "https://www.youtube.com/results?search_query=cats",
    "not a url at all",
    "",
])
def test_ignores_everything_else(url):
    assert parse_video_id(url) is None


def test_host_matching_is_case_insensitive():
    assert parse_video_id(f"https://WWW.YouTube.COM/watch?v={VIDEO_ID}") == VIDEO_ID


def test_lookalike_domain_is_not_youtube():
    """youtube.com.evil.example darf nicht als YouTube durchgehen."""
    assert parse_video_id(f"https://youtube.com.evil.example/watch?v={VIDEO_ID}") is None


def test_yt_dlp_gets_the_link_as_one_video_never_as_an_option(monkeypatch):
    import subprocess

    calls = []

    def run(cmd, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"url": "https://cdn.example.org/v.mp4", "ext": "mp4"}', stderr="")

    monkeypatch.setattr(youtube, "yt_dlp_cmd", lambda: ["yt-dlp"])
    monkeypatch.setattr(youtube.subprocess, "run", run)
    stream = youtube.resolve_stream("https://www.youtube.com/watch?v=abc&list=PL1")
    assert stream.url == "https://cdn.example.org/v.mp4"
    cmd = calls[0]
    assert "--no-playlist" in cmd
    assert cmd[-2:] == ["--", "https://www.youtube.com/watch?v=abc&list=PL1"]
