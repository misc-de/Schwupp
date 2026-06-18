#!/usr/bin/env bash
# Bindet Schwupp ins System ein: App-Icon (aus logo.png) + Desktop-Eintrag.
# Läuft komplett im Benutzerkontext (~/.local), braucht KEIN sudo und
# installiert KEINE zusätzlichen Pakete – es nutzt nur, was schon da ist.
#
#   ./installation.sh            installieren
#   ./installation.sh --uninstall   wieder entfernen
set -euo pipefail

APP_ID="de.cais.Schwupp"
APP_NAME="Schwupp"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGO="$SRC_DIR/logo.png"
RUN="$SRC_DIR/run.sh"

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
ICONS="$DATA/icons/hicolor"
APPS="$DATA/applications"
DESKTOP="$APPS/$APP_ID.desktop"
SIZES=(16 24 32 48 64 128 256)

# --- Deinstallation --------------------------------------------------------
if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$DESKTOP"
    for s in "${SIZES[@]}"; do rm -f "$ICONS/${s}x${s}/apps/$APP_ID.png"; done
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -qtf "$ICONS" || true
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
    echo "✓ $APP_NAME entfernt."
    exit 0
fi

# --- Vorbedingungen --------------------------------------------------------
[[ -f "$LOGO" ]] || { echo "FEHLER: $LOGO nicht gefunden." >&2; exit 1; }
[[ -f "$RUN"  ]] || { echo "FEHLER: $RUN nicht gefunden." >&2; exit 1; }
chmod +x "$RUN"

# --- Skalierungs-Helfer (nutzt convert ODER venv-PIL ODER nur 128px) -------
scale_to() {  # $1 = Zielgröße, $2 = Zieldatei
    local size="$1" out="$2"
    if command -v magick >/dev/null 2>&1; then
        magick "$LOGO" -resize "${size}x${size}" "$out"
    elif command -v convert >/dev/null 2>&1; then
        convert "$LOGO" -resize "${size}x${size}" "$out"
    elif LOGO="$LOGO" "$SRC_DIR/.venv/bin/python" - "$size" "$out" <<'PY' 2>/dev/null
import os, sys
from PIL import Image
size, out = int(sys.argv[1]), sys.argv[2]
Image.open(os.environ["LOGO"]).convert("RGBA").resize((size, size), Image.LANCZOS).save(out)
PY
    then :
    else
        cp "$LOGO" "$out"
    fi
}

# --- Icons installieren ----------------------------------------------------
echo "Installiere App-Icon ($APP_ID) …"
for s in "${SIZES[@]}"; do
    dir="$ICONS/${s}x${s}/apps"
    mkdir -p "$dir"
    scale_to "$s" "$dir/$APP_ID.png"
done

# --- Desktop-Eintrag schreiben --------------------------------------------
echo "Schreibe Desktop-Eintrag …"
mkdir -p "$APPS"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$APP_NAME
GenericName=Cast-App
Comment=Bildschirm, YouTube und Medien auf den Fernseher casten (Chromecast / LG webOS)
Exec=$RUN
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;Video;
Keywords=Cast;Chromecast;webOS;Mirror;Spiegeln;Stream;TV;
StartupNotify=true
StartupWMClass=$APP_ID
EOF
chmod 644 "$DESKTOP"

# --- Caches aktualisieren --------------------------------------------------
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -qtf "$ICONS" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true

echo
echo "✓ $APP_NAME ist installiert."
echo "  Eintrag : $DESKTOP"
echo "  Icon    : $ICONS/<größe>/apps/$APP_ID.png"
echo "  Im App-Menü unter \"$APP_NAME\" zu finden (ggf. ab-/anmelden für den Cache)."
