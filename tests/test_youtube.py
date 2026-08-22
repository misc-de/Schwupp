"""Erkennung von YouTube-Links (entscheidet, ob die TV-App statt yt-dlp läuft)."""
from __future__ import annotations

import pytest

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
