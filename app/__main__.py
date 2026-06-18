"""Einstiegspunkt: ``python -m app``.

Prüft beim Start die Laufzeit-Abhängigkeiten:
* fehlt etwas **Erforderliches** -> Start abbrechen, Hinweis zeigen (GUI-Dialog,
  sonst auf der Konsole);
* fehlt nur **Optionales** -> die App startet und fragt, ob trotzdem fortgefahren
  werden soll (betroffene Funktionen sind dann deaktiviert).
"""
from __future__ import annotations

import sys


def main() -> int:
    from . import deps

    missing_req, missing_opt = deps.check()
    if missing_req:
        return _abort_required(missing_req)

    from .ui.app import SchwuppApp

    return SchwuppApp(missing_optional=missing_opt).run(sys.argv)


def _format(deps_list) -> str:  # noqa: ANN001
    from .i18n import t
    return "\n".join(f"  •  {d.package} — {t(d.feature_key)}" for d in deps_list)


def _abort_required(missing) -> int:  # noqa: ANN001
    from . import deps
    from .i18n import t

    title = t("deps.required.title")
    body = t("deps.required.body", list=_format(missing))
    if deps.gui_available():
        try:
            _show_blocking_dialog(title, body)
            return 1
        except Exception:  # noqa: BLE001
            pass
    print(f"\n{title}\n\n{body}\n", file=sys.stderr)
    return 1


def _show_blocking_dialog(title: str, body: str) -> None:
    """Zeigt einen modalen Hinweis ohne Hauptfenster (für Required-Fehler)."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    from .i18n import t

    app = Adw.Application(application_id="de.cais.Schwupp.deps")

    def on_activate(a) -> None:  # noqa: ANN001
        win = Gtk.ApplicationWindow(application=a, default_width=1, default_height=1)
        win.set_decorated(False)
        win.present()
        dialog = Adw.AlertDialog(heading=title, body=body)
        dialog.add_response("quit", t("deps.quit"))
        dialog.set_default_response("quit")
        dialog.connect("response", lambda *_: a.quit())
        dialog.present(win)

    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
