#!/usr/bin/env python3
"""Rückt eine Eigenheit von flatpak-pip-generator zurecht.

Der Generator sucht das SDK mit ``flatpak info --user`` und, falls das
fehlschlägt, mit ``--system`` – ohne die Architektur zu nennen. Liegt im
Benutzerbereich ein SDK derselben Version für eine *andere* Architektur (hier
das aarch64-SDK für den Build auf dem Telefon), meldet ``flatpak info`` einen
Treffer, der spätere ``flatpak run`` findet für die Host-Architektur aber
nichts und bricht ab:

    Fehler: app/org.gnome.Sdk/x86_64/49 ist nicht installiert

Der Patch gibt der Prüfung die Host-Architektur mit. Er ist idempotent und
wird von ``make pip-modules`` vor jedem Lauf angewandt, weil das Werkzeug
selbst nicht im Repository liegt.
"""
from __future__ import annotations

import pathlib
import sys

SRC = '["flatpak", "info", scope, runtime],'
DST = '["flatpak", "info", scope, "--arch", _HOST_ARCH, runtime],'
ANCHOR = "def get_flatpak_runtime_scope"
PRELUDE = (
    '_HOST_ARCH = subprocess.run(\n'
    '    ["flatpak", "--default-arch"], capture_output=True, text=True\n'
    ').stdout.strip()\n\n\n'
)


def main(path: str) -> int:
    file = pathlib.Path(path)
    if not file.is_file():
        print(f"{file}: nicht gefunden", file=sys.stderr)
        return 1
    text = file.read_text(encoding="utf-8")

    if DST in text:
        print(f"{file.name}: bereits angepasst")
        return 0
    if SRC not in text or ANCHOR not in text:
        print(f"{file.name}: erwartete Stelle nicht gefunden – "
              f"vermutlich neue Version, bitte prüfen", file=sys.stderr)
        return 1

    text = text.replace(SRC, DST, 1).replace(ANCHOR, PRELUDE + ANCHOR, 1)
    file.write_text(text, encoding="utf-8")
    print(f"{file.name}: Architektur-Prüfung ergänzt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1
                          else ".flathub-tools/flatpak-pip-generator.py"))
