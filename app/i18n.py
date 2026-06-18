"""Leichtgewichtige Internationalisierung.

Übersetzungen liegen als flache ``{key: text}``-JSON-Dateien unter ``lang/<code>.json``
im Projekt-Root. Englisch (``en``) ist die Quellsprache und der Fallback: Wird die
Systemsprache nicht unterstützt (oder fehlt ein Schlüssel), greift Englisch.

Sprachwahl: ``SCHWUPP_LANG`` > ``LC_ALL`` > ``LC_MESSAGES`` > ``LANG`` > locale.
"""
from __future__ import annotations

import json
import locale
import os
from pathlib import Path

SOURCE_LANGUAGE = "en"
_LANG_DIR = Path(__file__).resolve().parent.parent / "lang"


def _load() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if _LANG_DIR.exists():
        for f in _LANG_DIR.glob("*.json"):
            try:
                out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    out.setdefault(SOURCE_LANGUAGE, {})
    return out


_TRANSLATIONS = _load()
SUPPORTED_LANGUAGES = sorted(_TRANSLATIONS)


def _normalize(language: str | None) -> str:
    """Macht z. B. 'de_DE.UTF-8' zu 'de'; fällt auf Englisch zurück."""
    if not language:
        return SOURCE_LANGUAGE
    code = language.split(".", 1)[0].split("_", 1)[0].split("-", 1)[0].lower()
    return code if code in _TRANSLATIONS else SOURCE_LANGUAGE


def _detect() -> str:
    for var in ("SCHWUPP_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val and val not in ("C", "POSIX"):
            return _normalize(val)
    try:
        return _normalize(locale.getlocale()[0])
    except (ValueError, TypeError):
        return SOURCE_LANGUAGE


LANGUAGE = _detect()


def t(key: str, **values: object) -> str:
    """Übersetzt *key* in die erkannte Sprache (mit Englisch-Fallback)."""
    text = _TRANSLATIONS.get(LANGUAGE, {}).get(key)
    if text is None:
        text = _TRANSLATIONS.get(SOURCE_LANGUAGE, {}).get(key, key)
    return text.format(**values) if values else text
