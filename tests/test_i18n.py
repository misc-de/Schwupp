"""Übersetzungen: Vollständigkeit, Platzhalter und Sprachwahl.

Ein fehlender Schlüssel oder ein abweichender Platzhalter fällt erst zur
Laufzeit auf – meist im ungünstigsten Moment (Fehlermeldung im Toast).
"""
from __future__ import annotations

import json
import re

import pytest

from app import i18n
from app.paths import data_file

LANG_DIR = data_file("lang")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _catalog(code: str) -> dict[str, str]:
    return json.loads((LANG_DIR / f"{code}.json").read_text(encoding="utf-8"))


def test_english_is_the_source_language():
    assert i18n.SOURCE_LANGUAGE == "en"
    assert "en" in i18n.SUPPORTED_LANGUAGES


def test_all_catalogs_have_the_same_keys():
    english = set(_catalog("en"))
    for code in i18n.SUPPORTED_LANGUAGES:
        assert set(_catalog(code)) == english, f"Katalog {code} weicht ab"


def test_placeholders_match_across_languages():
    english = _catalog("en")
    for code in i18n.SUPPORTED_LANGUAGES:
        if code == "en":
            continue
        catalog = _catalog(code)
        for key, text in english.items():
            assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(catalog[key])), \
                f"Platzhalter in {code}/{key} passen nicht zu Englisch"


def test_no_empty_translations():
    for code in i18n.SUPPORTED_LANGUAGES:
        for key, text in _catalog(code).items():
            assert text.strip(), f"{code}/{key} ist leer"


@pytest.mark.parametrize("value,expected", [
    ("de_DE.UTF-8", "de"),
    ("de", "de"),
    ("de-AT", "de"),
    ("en_GB", "en"),
    ("fr_FR.UTF-8", "en"),      # nicht übersetzt -> Englisch
    ("", "en"),
    (None, "en"),
])
def test_locale_normalisation(value, expected):
    assert i18n._normalize(value) == expected


def test_translation_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(i18n, "LANGUAGE", "de")
    monkeypatch.setattr(i18n, "_TRANSLATIONS",
                        {"de": {}, "en": {"a.b": "English text"}})
    assert i18n.t("a.b") == "English text"


def test_unknown_key_returns_the_key():
    assert i18n.t("does.not.exist") == "does.not.exist"


def test_placeholders_are_filled(monkeypatch):
    monkeypatch.setattr(i18n, "LANGUAGE", "en")
    monkeypatch.setattr(i18n, "_TRANSLATIONS", {"en": {"greet": "Hi {name}!"}})
    assert i18n.t("greet", name="Chris") == "Hi Chris!"
