"""Update-Prüfung und -Installation für Schwupp (git pull oder ZIP-Download).

Vorbild: DrivePulse. Zwei Strategien:
  • Ist das Projekt ein git-Klon mit Remote → `git fetch` + `git pull`.
  • Sonst → VERSION von GitHub-raw vergleichen und das ZIP des main-Branches
    herunterladen und über das Verzeichnis legen (Config liegt in
    ~/.config/schwupp und wird nicht berührt).

Das ZIP wird **nicht** direkt über die laufende Installation gebügelt: Es landet
zuerst in einem Staging-Verzeichnis, wird dort geprüft (Struktur, Version,
Kompilierbarkeit aller Python-Dateien) und erst dann Datei für Datei atomar
(``os.replace``) an seinen Platz gesetzt. Ein Netzabbruch mitten im Update kann
so keine halb aktualisierte Installation mehr hinterlassen.

Wo die App nicht in ihr eigenes Verzeichnis schreiben darf (Flatpak, systemweite
Installation), ist Selbst-Update abgeschaltet – siehe :func:`updates_supported`.

Repo: https://github.com/misc-de/Schwupp
"""
from __future__ import annotations

import compileall
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import NamedTuple

from . import VERSION as APP_VERSION
from .paths import project_root

_APP_DIR = project_root()               # Projekt-Root (enthält VERSION)
_GITHUB_REPO = "misc-de/Schwupp"
_BRANCH = "main"
_RAW_BASE = f"https://raw.githubusercontent.com/{_GITHUB_REPO}/{_BRANCH}"
_ZIP_URL = f"https://github.com/{_GITHUB_REPO}/archive/refs/heads/{_BRANCH}.zip"

# Beim ZIP-Update niemals überschreiben/löschen (lokale Daten & Umgebung)
_ZIP_SKIP = {".git", ".venv", "__pycache__", "VERSION.local", ".flatpak-builder",
             "repo", "repo-arm", ".flatpak-build"}
# In diesen Verzeichnissen werden Dateien entfernt, die es im neuen Stand nicht
# mehr gibt (sonst bleiben gelöschte Module als Altlast liegen und werden
# womöglich noch importiert).
_PRUNE_DIRS = ("app", "lang", "tools", "data")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class UpdateInfo(NamedTuple):
    available: bool
    remote_version: str | None   # None wenn kein Update / unbekannt
    error: str | None = None     # gesetzt, wenn die Prüfung scheiterte


def get_current_version() -> str:
    return APP_VERSION


def _log(msg: str) -> None:
    print(f"[updater] {msg}")


# ---------------------------------------------------------------------------
# Wo Selbst-Update überhaupt möglich ist
# ---------------------------------------------------------------------------

def in_flatpak() -> bool:
    return Path("/.flatpak-info").exists()


def updates_supported() -> tuple[bool, str]:
    """(möglich?, Grund) – darf sich die App selbst aktualisieren?"""
    if in_flatpak():
        return False, "flatpak"
    if not os.access(_APP_DIR, os.W_OK):
        return False, "readonly"
    return True, ""


# ---------------------------------------------------------------------------
# git-Helfer
# ---------------------------------------------------------------------------

def _git(*args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args], cwd=_APP_DIR, capture_output=True,
            text=True, timeout=timeout, check=False,
        )
        return r.returncode, (r.stdout.strip() or r.stderr.strip())
    except Exception as exc:  # noqa: BLE001
        _log(f"git {args}: {exc}")
        return -1, ""


def _is_git_repo() -> bool:
    if not (_APP_DIR / ".git").exists():
        return False
    code, out = _git("remote")          # Update nur sinnvoll mit Remote
    return code == 0 and bool(out)


def _current_branch() -> str:
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    return branch if branch and branch != "HEAD" else _BRANCH


# ---------------------------------------------------------------------------
# HTTP-Helfer
# ---------------------------------------------------------------------------

def _http_get_text(url: str, timeout: int = 15) -> str | None:
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text.strip()
    except Exception as exc:  # noqa: BLE001
        _log(f"HTTP GET {url}: {exc}")
        return None


def _http_download(url: str, dest: Path, timeout: int = 120) -> bool:
    try:
        import requests
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"Download {url} fehlgeschlagen: {exc}")
        return False


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

def check_for_update() -> UpdateInfo:
    """Prüft, ob eine neuere Version verfügbar ist."""
    ok, reason = updates_supported()
    if not ok:
        return UpdateInfo(False, None, reason)
    if _is_git_repo():
        return _check_git()
    return _check_zip()


def apply_update() -> bool:
    """Lädt das Update herunter und wendet es an."""
    ok, _reason = updates_supported()
    if not ok:
        return False
    if _is_git_repo():
        return _apply_git()
    return _apply_zip()


# ---------------------------------------------------------------------------
# git-Strategie
# ---------------------------------------------------------------------------

def _check_git() -> UpdateInfo:
    code, _ = _git("fetch", "--quiet", timeout=30)
    if code != 0:
        return UpdateInfo(False, None, "git fetch fehlgeschlagen (offline?)")
    branch = _current_branch()
    code, count_str = _git("rev-list", f"HEAD..origin/{branch}", "--count")
    try:
        behind = int(count_str) > 0
    except ValueError:
        return UpdateInfo(False, None, "Vergleich mit Remote fehlgeschlagen")
    if not behind:
        return UpdateInfo(False, None)
    _, remote_ver = _git("show", f"origin/{branch}:VERSION")
    return UpdateInfo(True, remote_ver.strip() or None)


def _apply_git() -> bool:
    code, out = _git("pull", "--quiet", timeout=120)
    if code != 0:
        _log(f"git pull fehlgeschlagen: {out}")
        return False
    return True


# ---------------------------------------------------------------------------
# ZIP-Strategie
# ---------------------------------------------------------------------------

def _check_zip() -> UpdateInfo:
    remote_ver = _http_get_text(f"{_RAW_BASE}/VERSION")
    if not remote_ver:
        return UpdateInfo(False, None, "Konnte Versionsinfo nicht abrufen")
    if remote_ver == APP_VERSION:
        return UpdateInfo(False, None)
    return UpdateInfo(True, remote_ver)


def _validate_staged(src: Path) -> str | None:
    """Prüft einen entpackten Stand. Gibt einen Fehlertext zurück oder None."""
    for required in ("VERSION", "app/__main__.py", "app/ui/window.py", "lang/en.json"):
        if not (src / required).exists():
            return f"Unvollständiges Update-Archiv: {required} fehlt"
    version = (src / "VERSION").read_text(encoding="utf-8").strip()
    if not _VERSION_RE.match(version):
        return f"Unerwartete Versionsangabe im Archiv: {version!r}"
    # Syntaxfehler früh erkennen – nach dem Kopieren wäre die App unstartbar.
    if not compileall.compile_dir(str(src / "app"), quiet=2, force=True):
        return "Update-Archiv enthält nicht kompilierbare Python-Dateien"
    return None


def _apply_zip() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "schwupp.zip"
        _log("Lade Update-ZIP …")
        if not _http_download(_ZIP_URL, zip_path):
            return False

        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()
        try:
            with zipfile.ZipFile(zip_path) as zf:
                broken = zf.testzip()
                if broken is not None:
                    _log(f"ZIP beschädigt (defekter Eintrag: {broken})")
                    return False
                zf.extractall(extract_dir)
        except Exception as exc:  # noqa: BLE001
            _log(f"ZIP-Entpacken fehlgeschlagen: {exc}")
            return False

        # GitHub packt in EIN Unterverzeichnis (z. B. Schwupp-main)
        subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if len(subdirs) != 1:
            _log(f"Unerwartete ZIP-Struktur: {subdirs}")
            return False
        staged = subdirs[0]

        problem = _validate_staged(staged)
        if problem:
            _log(problem)
            return False

        try:
            _install_staged(staged, _APP_DIR)
        except OSError as exc:
            _log(f"Update konnte nicht installiert werden: {exc}")
            return False
        return True


def _relative_files(root: Path) -> set[Path]:
    """Alle Dateien unterhalb *root* als relative Pfade (ohne _ZIP_SKIP)."""
    out: set[Path] = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in _ZIP_SKIP for part in rel.parts):
            continue
        if path.is_file():
            out.add(rel)
    return out


def _install_staged(src: Path, dst: Path) -> None:
    """Übernimmt den geprüften Stand *src* nach *dst* – Datei für Datei atomar."""
    new_files = _relative_files(src)
    for rel in sorted(new_files):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target.with_name(target.name + ".update-tmp")
        shutil.copy2(src / rel, tmp_target)
        os.replace(tmp_target, target)   # atomar: nie eine halbe Datei sichtbar

    # Verwaiste Dateien in den verwalteten Verzeichnissen entfernen.
    for folder in _PRUNE_DIRS:
        base = dst / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*"), reverse=True):
            rel = path.relative_to(dst)
            if any(part in _ZIP_SKIP for part in rel.parts):
                continue
            if path.is_file() and rel not in new_files:
                path.unlink(missing_ok=True)
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
