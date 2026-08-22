"""Auffinden der Datendateien in den drei Layouts (Git-Klon, Prefix, Flatpak)."""
from __future__ import annotations

import importlib

from app import paths


def test_env_override_wins(tmp_path, monkeypatch):
    (tmp_path / "lang").mkdir()
    (tmp_path / "VERSION").write_text("9.9.9\n")
    monkeypatch.setenv("SCHWUPP_DATA_DIR", str(tmp_path))
    assert paths.data_dir() == tmp_path
    assert paths.data_file("VERSION").read_text().strip() == "9.9.9"


def test_ignores_an_env_dir_without_data(tmp_path, monkeypatch):
    """Ein leeres Verzeichnis darf den Git-Klon nicht verdecken."""
    monkeypatch.setenv("SCHWUPP_DATA_DIR", str(tmp_path))
    assert paths.data_dir() != tmp_path
    assert (paths.data_dir() / "lang").is_dir()


def test_repository_layout_is_found_by_default(monkeypatch):
    monkeypatch.delenv("SCHWUPP_DATA_DIR", raising=False)
    assert (paths.data_dir() / "lang" / "en.json").is_file()


def test_version_is_read_from_the_data_dir(monkeypatch):
    monkeypatch.delenv("SCHWUPP_DATA_DIR", raising=False)
    import app

    importlib.reload(app)
    assert app.VERSION.count(".") == 2
    assert (paths.data_file("VERSION")).read_text().strip() == app.VERSION
