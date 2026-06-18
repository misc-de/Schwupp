"""Einstellungs-Dialog.

* **Gerätespezifisch** (wenn ein verbundener Receiver übergeben wird): Spiegel-Engine
  und Videoqualität für *genau dieses* Gerät – gespeichert je Geräte-UUID. Die
  Engine-Auswahl ist auf das gefiltert, was der Gerätetyp kann.
* **Global** (immer): App / Updates.
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import VERSION, updater  # noqa: E402
from ..i18n import t  # noqa: E402
from ..mirror import engines_for_kind  # noqa: E402

_RESOLUTIONS = [720, 1080, 2160]  # 16:9-Zielhöhen für die Auswahl


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, config, receiver=None) -> None:  # noqa: ANN001
        super().__init__()
        self.set_title(t("settings.title"))
        self._config = config
        self._receiver = receiver
        self._uuid = receiver.info.uuid if receiver is not None else None

        page = Adw.PreferencesPage(title=t("settings.general"), icon_name="emblem-system-symbolic")
        self.add(page)

        if receiver is not None:
            self._build_device_section(page, receiver)
        self._build_app_section(page)

    # ====================================================================
    # Gerätespezifisch: Spiegel-Engine + Videoqualität
    # ====================================================================
    def _build_device_section(self, page, receiver) -> None:  # noqa: ANN001
        engines = engines_for_kind(receiver.kind)
        if not engines:
            return  # Gerät kann nicht spiegeln -> keine Sektion
        uuid = receiver.info.uuid

        grp = Adw.PreferencesGroup(title=t("settings.mirror_group"), description=receiver.name)
        page.add(grp)

        self._engines = engines
        names = [e.display_name + ("" if e.available else t("settings.unavailable_suffix"))
                 for e in engines]
        self._engine_row = Adw.ComboRow(title=t("settings.engine"), model=Gtk.StringList.new(names))
        current = self._config.device_value(uuid, "mirror_engine")
        for i, e in enumerate(engines):
            if e.name == current:
                self._engine_row.set_selected(i)
                break
        self._engine_row.connect("notify::selected", self._on_engine_changed)
        grp.add(self._engine_row)

        grp_video = Adw.PreferencesGroup(title=t("settings.video"))
        page.add(grp_video)

        res_labels = [f"{h}p" + (" (4K)" if h == 2160 else "") for h in _RESOLUTIONS]
        self._res_row = Adw.ComboRow(title=t("settings.resolution"),
                                     model=Gtk.StringList.new(res_labels))
        cur_h = int(self._config.device_value(uuid, "mirror_height"))
        self._res_row.set_selected(_RESOLUTIONS.index(cur_h) if cur_h in _RESOLUTIONS else 1)
        self._res_row.connect("notify::selected", self._on_res_changed)
        grp_video.add(self._res_row)

        self._bitrate = Adw.SpinRow.new_with_range(1000, 20000, 500)
        self._bitrate.set_title(t("settings.bitrate"))
        self._bitrate.set_value(int(self._config.device_value(uuid, "mirror_bitrate_kbps")))
        self._bitrate.connect("notify::value", self._on_bitrate)
        grp_video.add(self._bitrate)

        self._fps = Adw.SpinRow.new_with_range(10, 60, 5)
        self._fps.set_title(t("settings.fps"))
        self._fps.set_value(int(self._config.device_value(uuid, "mirror_fps")))
        self._fps.connect("notify::value", self._on_fps)
        grp_video.add(self._fps)

    def _on_engine_changed(self, row, _p) -> None:  # noqa: ANN001
        self._config.set_device_value(self._uuid, "mirror_engine",
                                      self._engines[row.get_selected()].name)
        self._config.save()

    def _on_res_changed(self, row, _p) -> None:  # noqa: ANN001
        self._config.set_device_value(self._uuid, "mirror_height",
                                      _RESOLUTIONS[row.get_selected()])
        self._config.save()

    def _on_bitrate(self, row, _p) -> None:  # noqa: ANN001
        self._config.set_device_value(self._uuid, "mirror_bitrate_kbps", int(row.get_value()))
        self._config.save()

    def _on_fps(self, row, _p) -> None:  # noqa: ANN001
        self._config.set_device_value(self._uuid, "mirror_fps", int(row.get_value()))
        self._config.save()

    # ====================================================================
    # Global: App / Updates
    # ====================================================================
    def _build_app_section(self, page) -> None:  # noqa: ANN001
        grp_app = Adw.PreferencesGroup(title=t("settings.app_group"))
        page.add(grp_app)

        self._remote_version: str | None = None
        self._reset_src = 0
        self._update_row = Adw.ActionRow(title="Schwupp")
        self._update_row.set_subtitle(self._version_subtitle())
        self._update_btn = Gtk.Button(label=t("settings.check_updates"), valign=Gtk.Align.CENTER)
        self._update_btn.connect("clicked", self._on_check_update)
        self._update_row.add_suffix(self._update_btn)
        grp_app.add(self._update_row)

    def _version_subtitle(self) -> str:
        last = self._config["last_update_check"]
        if last:
            try:
                last = t("settings.last_checked",
                         date=datetime.fromisoformat(last).strftime("%d.%m.%Y %H:%M"))
            except ValueError:
                pass
        return f"v{VERSION}" + (f"  ·  {last}" if last else "")

    def _on_check_update(self, _btn) -> None:  # noqa: ANN001
        self._cancel_reset()
        self._update_btn.set_label(t("settings.checking"))
        self._update_btn.set_sensitive(False)
        threading.Thread(target=self._do_check, daemon=True).start()

    def _do_check(self) -> None:
        info = updater.check_for_update()
        GLib.idle_add(self._on_check_done, info)

    def _on_check_done(self, info: updater.UpdateInfo) -> bool:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        self._config["last_update_check"] = now
        self._config.save()
        self._update_row.set_subtitle(self._version_subtitle())
        if info.available:
            self._remote_version = info.remote_version
            self._update_btn.set_label(t("settings.update_to", version=info.remote_version or "?"))
            self._update_btn.add_css_class("suggested-action")
            self._update_btn.set_sensitive(True)
            self._update_btn.disconnect_by_func(self._on_check_update)
            self._update_btn.connect("clicked", self._on_apply_update)
        else:
            self._update_btn.remove_css_class("suggested-action")
            if info.error:
                self._update_btn.set_label(t("settings.error_retry"))
                self._update_btn.set_sensitive(True)
                self._update_row.set_subtitle(f"v{VERSION}  ·  {info.error}")
            else:
                self._update_btn.set_label(t("settings.up_to_date"))
                self._update_btn.set_sensitive(False)
                self._reset_src = GLib.timeout_add_seconds(8, self._reset_btn_idle)
        return False

    def _on_apply_update(self, _btn) -> None:  # noqa: ANN001
        self._update_btn.set_label(t("settings.updating"))
        self._update_btn.set_sensitive(False)
        threading.Thread(target=self._do_apply, daemon=True).start()

    def _do_apply(self) -> None:
        ok = updater.apply_update()
        GLib.idle_add(self._on_apply_done, ok)

    def _on_apply_done(self, ok: bool) -> bool:
        if ok:
            self._update_btn.remove_css_class("suggested-action")
            self._update_btn.set_label(t("settings.restart_required"))
            self._update_btn.set_sensitive(True)
            try:
                self._update_btn.disconnect_by_func(self._on_apply_update)
            except TypeError:
                pass
            self._update_btn.connect("clicked", self._show_restart_dialog)
            self._show_restart_dialog(None)
        else:
            self._update_btn.set_label(t("settings.update_failed"))
            self._update_btn.set_sensitive(True)
        return False

    def _show_restart_dialog(self, _btn) -> None:  # noqa: ANN001
        dialog = Adw.AlertDialog(
            heading=t("settings.restart_title"),
            body=t("settings.restart_body"),
        )
        dialog.add_response("no", t("settings.later"))
        dialog.add_response("yes", t("settings.restart_now"))
        dialog.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("yes")
        dialog.set_close_response("no")
        dialog.connect("response", self._on_restart_response)
        dialog.present(self)

    def _on_restart_response(self, _dialog, response: str) -> None:  # noqa: ANN001
        if response == "yes":
            os.execv(sys.executable, [sys.executable, "-m", "app", *sys.argv[1:]])

    def _cancel_reset(self) -> None:
        if self._reset_src:
            GLib.source_remove(self._reset_src)
            self._reset_src = 0

    def _reset_btn_idle(self) -> bool:
        self._reset_src = 0
        self._update_btn.set_label(t("settings.check_updates"))
        self._update_btn.set_sensitive(True)
        return False
