"""Update-Prüfung und -Installation für Schwupp (git pull oder ZIP-Download).

Vorbild: DrivePulse. Zwei Strategien:
  • Ist das Projekt ein git-Klon mit Remote → `git fetch` + `git pull`.
  • Sonst → VERSION von GitHub-raw vergleichen und das ZIP des main-Branches
    herunterladen und über das Verzeichnis legen (Config liegt in
    ~/.config/schwupp und wird nicht berührt).

Repo: https://github.com/misc-de/Schwupp
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import NamedTuple

from . import VERSION as APP_VERSION

_APP_DIR = Path(__file__).resolve().parent.parent   # Projekt-Root (enthält VERSION)
_GITHUB_REPO = "misc-de/Schwupp"
_BRANCH = "main"
_RAW_BASE = f"https://raw.githubusercontent.com/{_GITHUB_REPO}/{_BRANCH}"
_ZIP_URL = f"https://github.com/{_GITHUB_REPO}/archive/refs/heads/{_BRANCH}.zip"

# Beim ZIP-Update niemals überschreiben/löschen (lokale Daten & Umgebung)
_ZIP_SKIP = {".git", ".venv", "__pycache__", "VERSION.local"}


class UpdateInfo(NamedTuple):
    available: bool
    remote_version: str | None   # None wenn kein Update / unbekannt
    error: str | None = None     # gesetzt, wenn die Prüfung scheiterte


def get_current_version() -> str:
    return APP_VERSION


def _log(msg: str) -> None:
    print(f"[updater] {msg}")


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
    if _is_git_repo():
        return _check_git()
    return _check_zip()


def apply_update() -> bool:
    """Lädt das Update herunter und wendet es an."""
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
                zf.extractall(extract_dir)
        except Exception as exc:  # noqa: BLE001
            _log(f"ZIP-Entpacken fehlgeschlagen: {exc}")
            return False

        # GitHub packt in EIN Unterverzeichnis (z. B. Schwupp-main)
        subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if len(subdirs) != 1:
            _log(f"Unerwartete ZIP-Struktur: {subdirs}")
            return False

        _copy_update(subdirs[0], _APP_DIR)
        return True


def _copy_update(src: Path, dst: Path) -> None:
    """Kopiert src → dst rekursiv, überspringt Einträge aus _ZIP_SKIP."""
    for item in src.iterdir():
        if item.name in _ZIP_SKIP:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _copy_update(item, target)
        else:
            shutil.copy2(item, target)
