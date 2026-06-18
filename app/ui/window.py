"""Hauptfenster: Geräte (Chromecast + LG webOS) finden, verbinden, casten, steuern.

Adaptiv über eine Adw.NavigationView (Geräte-Seite → Steuer-Seite). Spricht nur
das einheitliche Receiver-Interface; welche Aktionen sichtbar sind, richtet sich
nach den Fähigkeiten des verbundenen Geräts (``receiver.supports``).
"""
from __future__ import annotations

import mimetypes
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..discovery import Discovery, ReceiverInfo  # noqa: E402
from ..i18n import t  # noqa: E402
from ..mirror import engines_for_kind, get_engine_class  # noqa: E402
from ..receivers import Feature, create_receiver  # noqa: E402
from ..receivers.base import Context  # noqa: E402
from ..sources import youtube  # noqa: E402
from .settings import SettingsDialog  # noqa: E402

_KIND_ICON = {"chromecast": "video-display-symbolic", "webos": "tv-symbolic",
              "dlna": "tv-symbolic"}


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app) -> None:  # noqa: ANN001
        super().__init__(application=app, title="Schwupp")
        self.app = app
        self.set_default_size(420, 640)

        self.receiver = None
        self.engine = None
        self._rows: dict[str, Adw.ActionRow] = {}

        self._toasts = Adw.ToastOverlay()
        self.set_content(self._toasts)
        self._nav = Adw.NavigationView()
        self._toasts.set_child(self._nav)
        self._nav.add(self._build_devices_page())
        self._nav.connect("popped", self._on_popped)

        self._discovery = Discovery(on_add=self._device_added, on_remove=self._device_removed)
        self._discovery.start()
        self._context = Context(
            zconf=self._discovery.zconf, config=app.config, server=app.server
        )
        self.connect("close-request", self._on_close)

    # ====================================================================
    # Geräte-Seite
    # ====================================================================
    def _build_devices_page(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text(t("window.settings"))
        settings_btn.connect("clicked", self._open_settings)
        header.pack_end(settings_btn)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=600, margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
        scroller.set_child(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(box)

        self._devices_group = Adw.PreferencesGroup(title=t("window.devices_found"))
        box.append(self._devices_group)
        self._empty = Adw.StatusPage(
            icon_name="video-display-symbolic",
            title=t("window.searching"),
            description=t("window.searching_hint"),
        )
        self._empty.set_vexpand(True)
        self._devices_group.add(self._empty)

        toolbar.set_content(scroller)
        return Adw.NavigationPage(child=toolbar, title="Schwupp", tag="devices")

    def _device_added(self, info: ReceiverInfo) -> None:
        GLib.idle_add(self._add_row, info)

    def _device_removed(self, uuid: str) -> None:
        GLib.idle_add(self._remove_row, uuid)

    def _add_row(self, info: ReceiverInfo) -> bool:
        if self._empty is not None:
            self._devices_group.remove(self._empty)
            self._empty = None
        if info.uuid in self._rows:
            self._rows[info.uuid].set_title(info.name)
            return False
        kind_label = t(f"device.{info.kind}")
        row = Adw.ActionRow(title=info.name,
                            subtitle=f"{kind_label} · {info.model or info.host}",
                            activatable=True)
        row.add_prefix(Gtk.Image.new_from_icon_name(
            _KIND_ICON.get(info.kind, "video-display-symbolic")))
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", lambda _r, i=info: self._connect(i))
        self._devices_group.add(row)
        self._rows[info.uuid] = row
        return False

    def _remove_row(self, uuid: str) -> bool:
        row = self._rows.pop(uuid, None)
        if row is not None:
            self._devices_group.remove(row)
        return False

    # ====================================================================
    # Verbinden
    # ====================================================================
    def _connect(self, info: ReceiverInfo) -> None:
        self._toast(t("window.connecting", name=info.name))
        receiver = create_receiver(info, self._context)

        def prompt_cb() -> None:
            GLib.idle_add(self._toast, t("window.pairing"))

        def work() -> None:
            try:
                receiver.connect(prompt_cb=prompt_cb)
                GLib.idle_add(self._on_connected, receiver)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._toast, t("window.connect_failed", error=exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_connected(self, receiver) -> bool:  # noqa: ANN001
        self.receiver = receiver
        self.app.config["last_device_uuid"] = receiver.info.uuid
        self.app.config.save()
        self._nav.push(self._build_control_page(receiver))
        return False

    # ====================================================================
    # Steuer-Seite
    # ====================================================================
    def _build_control_page(self, receiver) -> Adw.NavigationPage:  # noqa: ANN001
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.connect("clicked", self._open_settings)
        header.pack_end(settings_btn)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=600, margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
        scroller.set_child(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)

        # -- Quellen ---------------------------------------------------------
        grp_src = Adw.PreferencesGroup(title=t("window.cast_group"))
        box.append(grp_src)

        if receiver.supports(Feature.MEDIA):
            row_file = Adw.ActionRow(title=t("window.media_file"),
                                     subtitle=t("window.media_file_sub"), activatable=True)
            row_file.add_prefix(Gtk.Image.new_from_icon_name("folder-videos-symbolic"))
            row_file.connect("activated", self._choose_file)
            grp_src.add(row_file)

        if engines_for_kind(receiver.kind):
            self._mirror_row = Adw.ActionRow(title=t("window.mirror"),
                                             subtitle=self._mirror_subtitle(receiver))
            self._mirror_row.add_prefix(Gtk.Image.new_from_icon_name("video-display-symbolic"))
            self._mirror_btn = Gtk.Button(label=t("window.start"), valign=Gtk.Align.CENTER)
            self._mirror_btn.add_css_class("suggested-action")
            self._mirror_btn.connect("clicked", self._toggle_mirror)
            self._mirror_row.add_suffix(self._mirror_btn)
            grp_src.add(self._mirror_row)
        else:
            self._mirror_row = None

        # -- Link ------------------------------------------------------------
        if receiver.supports(Feature.MEDIA):
            grp_link = Adw.PreferencesGroup(title=t("window.link_group"))
            box.append(grp_link)
            self._url_row = Adw.EntryRow(title=t("window.url_placeholder"))
            self._url_row.set_show_apply_button(True)
            self._url_row.connect("apply", self._cast_url)
            grp_link.add(self._url_row)

        # -- Wiedergabe ------------------------------------------------------
        if receiver.supports(Feature.PLAYBACK) or receiver.supports(Feature.VOLUME):
            grp_play = Adw.PreferencesGroup(title=t("window.playback"))
            box.append(grp_play)
            if receiver.supports(Feature.PLAYBACK):
                ctl = Adw.ActionRow(title=t("window.controls"))
                btns = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
                for icon, cb in (
                    ("media-playback-start-symbolic", lambda _b: self._safe(self.receiver.play)),
                    ("media-playback-pause-symbolic", lambda _b: self._safe(self.receiver.pause)),
                    ("media-playback-stop-symbolic", lambda _b: self._safe(self.receiver.stop)),
                ):
                    b = Gtk.Button(icon_name=icon)
                    b.connect("clicked", cb)
                    btns.append(b)
                ctl.add_suffix(btns)
                grp_play.add(ctl)
            if receiver.supports(Feature.VOLUME):
                vol = Adw.ActionRow(title=t("window.volume"))
                scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
                scale.set_value(0.5)
                scale.set_size_request(160, -1)
                scale.set_valign(Gtk.Align.CENTER)
                scale.connect("value-changed",
                              lambda s: self._safe(lambda: self.receiver.set_volume(s.get_value())))
                vol.add_suffix(scale)
                grp_play.add(vol)

        toolbar.set_content(scroller)
        return Adw.NavigationPage(child=toolbar, title=receiver.name, tag="control")

    # ====================================================================
    # Aktionen
    # ====================================================================
    def _choose_file(self, _row) -> None:  # noqa: ANN001
        dialog = Gtk.FileDialog(title=t("window.choose_file"))
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result) -> None:  # noqa: ANN001
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        path = gfile.get_path()
        if not path:
            return
        url = self.app.server.add_file(path)
        mime = mimetypes.guess_type(path)[0] or "video/mp4"
        title = os.path.basename(path)
        self._toast(t("window.sending", title=title))
        self._async(lambda: self.receiver.play_media(url, mime, title=title),
                    err=t("window.play_failed"))

    def _cast_url(self, row) -> None:  # noqa: ANN001
        url = row.get_text().strip()
        if not url:
            return
        vid = youtube.parse_video_id(url)
        if vid and self.receiver.supports(Feature.YOUTUBE):
            self._toast(t("window.youtube_starting"))
            self._async(lambda: self.receiver.play_youtube(vid), err=t("window.youtube_failed"))
            return

        def work():
            if url.lower().split("?")[0].endswith((".mp4", ".webm", ".mkv", ".mp3", ".m3u8")):
                self.receiver.play_media(url, mimetypes.guess_type(url)[0] or "video/mp4", title=url)
            else:
                stream = youtube.resolve_stream(url)
                self.receiver.play_media(stream.url, stream.mime, title=stream.title)

        self._toast(t("window.resolving_link"))
        self._async(work, err=t("window.link_failed"))

    def _toggle_mirror(self, _btn) -> None:  # noqa: ANN001
        if self.engine is not None and self.engine.running:
            self.engine.stop()
            self.engine = None
            self._mirror_btn.set_label(t("window.start"))
            self._mirror_btn.remove_css_class("destructive-action")
            self._mirror_btn.add_css_class("suggested-action")
            self._toast(t("window.mirror_stopped"))
            return

        # Gerätespezifisch gewählte Engine (Default = erste passende für den Typ)
        name = self._chosen_engine_name(self.receiver)
        if name is None:
            self._toast(t("window.unknown_engine"))
            return
        try:
            cls = get_engine_class(name)
        except KeyError:
            self._toast(t("window.unknown_engine"))
            return
        ok, detail = cls.check_available()
        if not ok:
            self._toast(f"{cls.display_name}: {detail}")
            return
        self.engine = cls(self.receiver, self.app.server, self.app.config)
        try:
            self.engine.start()
        except Exception as exc:  # noqa: BLE001
            self.engine = None
            self._toast(str(exc))
            return
        self._mirror_btn.set_label(t("window.stop"))
        self._mirror_btn.remove_css_class("suggested-action")
        self._mirror_btn.add_css_class("destructive-action")
        self._toast(t("window.mirror_running", engine=cls.display_name))

    # ====================================================================
    # Navigation / Aufräumen
    # ====================================================================
    def _on_popped(self, _nav, page) -> None:  # noqa: ANN001
        if page.get_tag() == "control":
            self._cleanup_receiver()

    def _cleanup_receiver(self) -> None:
        if self.engine is not None:
            try:
                self.engine.stop()
            finally:
                self.engine = None
        if self.receiver is not None:
            self.receiver.disconnect()
            self.receiver = None

    def _on_close(self, _win) -> bool:  # noqa: ANN001
        self._cleanup_receiver()
        self._discovery.stop()
        return False

    # ====================================================================
    # Hilfen
    # ====================================================================
    # -- Engine-Auswahl / Spiegel-Beschreibung -------------------------------
    def _chosen_engine_name(self, receiver) -> str | None:  # noqa: ANN001
        engines = engines_for_kind(receiver.kind)
        if not engines:
            return None
        names = [e.name for e in engines]
        chosen = self.app.config.device_value(receiver.info.uuid, "mirror_engine")
        return chosen if chosen in names else names[0]

    def _mirror_subtitle(self, receiver) -> str:  # noqa: ANN001
        name = self._chosen_engine_name(receiver)
        return t(f"mirror.desc.{name}") if name else t("window.mirror_sub")

    def _refresh_mirror_subtitle(self) -> None:
        if self.receiver is not None and getattr(self, "_mirror_row", None) is not None:
            self._mirror_row.set_subtitle(self._mirror_subtitle(self.receiver))

    def _open_settings(self, _btn) -> None:  # noqa: ANN001
        dialog = SettingsDialog(self.app.config, receiver=self.receiver)
        if self.receiver is not None:
            dialog.connect("closed", lambda *_: self._refresh_mirror_subtitle())
        dialog.present(self)

    def _toast(self, text: str) -> bool:
        self._toasts.add_toast(Adw.Toast(title=text, timeout=3))
        return False

    def _safe(self, fn) -> None:
        if self.receiver is None:
            return
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self._toast(str(exc))

    def _async(self, fn, *, err: str | None = None) -> None:
        err = err or t("common.error")

        def work() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._toast, f"{err}: {exc}")

        threading.Thread(target=work, daemon=True).start()
