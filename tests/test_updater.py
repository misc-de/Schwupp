"""Selbst-Update: Staging, Prüfung und atomare Übernahme.

Ein abgebrochenes Update darf keine halb aktualisierte Installation
hinterlassen – das war der Grund, den direkten Kopiervorgang über das laufende
Verzeichnis durch ein geprüftes Staging zu ersetzen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import updater


def _make_release(root: Path, version: str = "1.2.3", extra: dict | None = None) -> Path:
    """Legt einen vollständigen, gültigen Programmstand an."""
    (root / "app" / "ui").mkdir(parents=True)
    (root / "lang").mkdir()
    (root / "VERSION").write_text(f"{version}\n")
    (root / "app" / "__init__.py").write_text("VERSION = 'x'\n")
    (root / "app" / "__main__.py").write_text("def main():\n    return 0\n")
    (root / "app" / "ui" / "window.py").write_text("class MainWindow:\n    pass\n")
    (root / "lang" / "en.json").write_text('{"a": "b"}')
    for rel, content in (extra or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


# -- Validierung -------------------------------------------------------------

def test_valid_release_passes(tmp_path):
    assert updater._validate_staged(_make_release(tmp_path)) is None


@pytest.mark.parametrize("missing", ["VERSION", "app/__main__.py", "lang/en.json"])
def test_incomplete_archive_is_rejected(tmp_path, missing):
    root = _make_release(tmp_path)
    (root / missing).unlink()
    problem = updater._validate_staged(root)
    assert problem and missing in problem


def test_bogus_version_is_rejected(tmp_path):
    root = _make_release(tmp_path)
    (root / "VERSION").write_text("not-a-version\n")
    assert "Versionsangabe" in updater._validate_staged(root)


def test_syntax_error_is_caught_before_installing(tmp_path):
    """Ein kaputtes Modul im Archiv würde die App unstartbar machen."""
    root = _make_release(tmp_path, extra={"app/broken.py": "def (:\n"})
    assert "kompilierbare" in updater._validate_staged(root)


# -- Übernahme ---------------------------------------------------------------

def test_install_replaces_and_adds_files(tmp_path):
    src = _make_release(tmp_path / "new", version="2.0.0",
                        extra={"app/neu.py": "print('neu')\n"})
    dst = _make_release(tmp_path / "old", version="1.0.0")

    updater._install_staged(src, dst)

    assert (dst / "VERSION").read_text().strip() == "2.0.0"
    assert (dst / "app" / "neu.py").exists()


def test_install_prunes_files_that_are_gone(tmp_path):
    """Gelöschte Module dürfen nicht als Altlast liegen bleiben."""
    src = _make_release(tmp_path / "new")
    dst = _make_release(tmp_path / "old", extra={"app/veraltet.py": "x = 1\n"})

    updater._install_staged(src, dst)
    assert not (dst / "app" / "veraltet.py").exists()


def test_install_keeps_local_data(tmp_path):
    src = _make_release(tmp_path / "new")
    dst = _make_release(tmp_path / "old")
    (dst / ".venv" / "bin").mkdir(parents=True)
    (dst / ".venv" / "bin" / "python").write_text("binary")
    (dst / ".git").mkdir()
    (dst / ".git" / "HEAD").write_text("ref: refs/heads/main")

    updater._install_staged(src, dst)

    assert (dst / ".venv" / "bin" / "python").exists()
    assert (dst / ".git" / "HEAD").exists()


def test_install_leaves_no_temporary_files(tmp_path):
    src = _make_release(tmp_path / "new")
    dst = _make_release(tmp_path / "old")
    updater._install_staged(src, dst)
    assert not list(dst.rglob("*.update-tmp"))


def test_relative_files_skips_local_directories(tmp_path):
    root = _make_release(tmp_path)
    (root / ".venv").mkdir()
    (root / ".venv" / "pyvenv.cfg").write_text("home = /usr")
    (root / "app" / "__pycache__").mkdir()
    (root / "app" / "__pycache__" / "x.pyc").write_bytes(b"\x00")

    found = updater._relative_files(root)
    assert Path("app/__main__.py") in found
    assert not any(".venv" in str(f) or "__pycache__" in str(f) for f in found)


# -- Gating ------------------------------------------------------------------

def test_no_self_update_inside_flatpak(monkeypatch):
    monkeypatch.setattr(updater, "in_flatpak", lambda: True)
    ok, reason = updater.updates_supported()
    assert not ok and reason == "flatpak"
    assert updater.check_for_update() == (False, None, "flatpak")
    assert updater.apply_update() is False


def test_no_self_update_when_the_directory_is_read_only(monkeypatch):
    monkeypatch.setattr(updater, "in_flatpak", lambda: False)
    monkeypatch.setattr(updater.os, "access", lambda *a, **k: False)
    ok, reason = updater.updates_supported()
    assert not ok and reason == "readonly"
