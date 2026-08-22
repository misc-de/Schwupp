#!/usr/bin/env bash
# Baut Schwupp für die Architektur des aktuellen Rechners in ./repo.
#
# Gedacht für Geräte, auf denen nur der geflatpakte org.flatpak.Builder zur
# Verfügung steht – etwa das FuriPhone FLX1. Dort scheitert dessen
# `build-finish`/`build-export`-Schritt an "fchown: Invalid argument" (die
# Sandbox darf im Home keine Eigentümer setzen). Deshalb läuft nur der
# eigentliche Bau im Builder-Flatpak; Abschluss und Export übernimmt das
# native `flatpak` des Systems.
#
#   scripts/build-arch.sh [manifest]
#
# Ergebnis: ./repo mit dem Ref app/de.cais.Schwupp/<arch>/master.
set -euo pipefail

manifest="${1:-de.cais.Schwupp.yaml}"
appid="$(basename "$manifest" .yaml)"
appid="${appid%.flathub}"
arch="$(flatpak --default-arch)"
builddir=".flatpak-build/$arch"
repo="${FP_REPO:-repo}"

if command -v flatpak-builder >/dev/null 2>&1; then
    builder=(flatpak-builder)
    extra=()
else
    builder=(flatpak run org.flatpak.Builder)
    # rofiles-fuse legt ein Overlay über den Baum, auf dem fchown mit EINVAL
    # scheitert – daran bricht der Abschluss-Schritt im Builder-Flatpak ab
    # ("Exporting share/applications/... error: fchown: Invalid argument").
    # Ohne das Overlay wird kopiert statt gehardlinkt: langsamer, aber es läuft.
    extra=(--disable-rofiles-fuse)
fi

echo "==> Baue $appid für $arch"
# --disable-updates: nichts nachladen, was das Manifest nicht nennt.
"${builder[@]}" --force-clean --disable-updates "${extra[@]}" "$builddir" "$manifest"

# finish-args und command aus dem Manifest ziehen (nur die Zeilen des
# finish-args-Blocks, Kommentare und leere Zeilen raus).
mapfile -t finish_args < <(
    awk '
        /^finish-args:/ { inblock = 1; next }
        inblock && /^[a-zA-Z]/ { inblock = 0 }
        inblock && /^[[:space:]]*-[[:space:]]*--/ {
            sub(/^[[:space:]]*-[[:space:]]*/, "")
            sub(/[[:space:]]+#.*$/, "")
            print
        }
    ' "$manifest"
)
command_name="$(awk '/^command:/ { print $2; exit }' "$manifest")"
[[ -n "$command_name" ]] || { echo "Kein 'command:' im Manifest gefunden." >&2; exit 1; }

# Konnte der Builder selbst abschließen, steht command= schon in der Metadata –
# dann würde ein zweiter Aufruf nur mit "already finalized" abbrechen.
if grep -q '^command=' "$builddir/metadata" 2>/dev/null; then
    echo "==> Build ist bereits abgeschlossen (command=$command_name)"
else
    echo "==> Schließe den Build nativ ab (${#finish_args[@]} finish-args, command=$command_name)"
    flatpak build-finish "$builddir" --command="$command_name" "${finish_args[@]}"
fi

echo "==> Exportiere nach $repo"
flatpak build-export "$repo" "$builddir" master

echo
echo "Fertig. Im Repo liegt jetzt:"
find "$repo/refs/heads/app" -type f 2>/dev/null | sed "s|$repo/refs/heads/|  |"
