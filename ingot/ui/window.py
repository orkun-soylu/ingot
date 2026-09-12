"""Ingot main window."""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import remote, source as source_mod
from ..devices import Disk, human_bytes, list_disks
from ..i18n import _
from ..messages import describe_seed_reason, stage_label
from ..picfg import (
    ConfigError,
    PiConfig,
    hash_password,
    list_public_keys,
    system_country,
    system_keymap,
    system_timezone,
)
from ..privileged import WriteJob, WriteOutcome, WriteRequest

IMAGE_PATTERNS = ("*.iso", "*.img", "*.raw", "*.xz", "*.gz", "*.zst", "*.zip")


class IngotWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="Ingot")
        # Height -1: the window takes its content's natural height. A fixed
        # height left a wide empty gap under short content.
        self.set_default_size(620, -1)

        self._disks: list[Disk] = []
        self._source: source_mod.ImageSource | None = None
        self._remote: remote.RemoteInfo | None = None
        self._job: WriteJob | None = None
        self._busy = False
        self._toml_tmp: Path | None = None
        self._cancel_download = threading.Event()

        self.set_content(self._build())
        self.refresh_disks()

    # ------------------------------------------------------------- layout --

    def _build(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Ingot", subtitle=_("Disk image writer")))
        view.add_top_bar(header)

        self.banner = Adw.Banner(revealed=False)
        view.add_top_bar(self.banner)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(12)
        box.set_margin_end(12)

        box.append(self._build_source_group())
        box.append(self._build_target_group())
        box.append(self._build_pi_group())
        box.append(self._build_options_group())

        clamp = Adw.Clamp(maximum_size=680, child=box)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(clamp)
        # Open at the content's height while it is short; scroll once it grows.
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(720)
        view.set_content(scroller)

        # The Write button sits in a bottom bar so it stays reachable without
        # scrolling when the Raspberry Pi panel makes the content overflow.
        view.add_bottom_bar(self._build_action_area())
        return view

    def _build_source_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Source"))

        self.file_row = Adw.ActionRow(
            title=_("Image file"), subtitle=_("None selected"), activatable=True
        )
        self.file_row.add_suffix(Gtk.Image.new_from_icon_name("document-open-symbolic"))
        self.file_row.connect("activated", lambda *_args: self.choose_file())
        group.add(self.file_row)

        self.url_row = Adw.EntryRow(title=_("…or a URL"))
        self.url_row.set_show_apply_button(False)
        query_button = Gtk.Button(label=_("Query"), valign=Gtk.Align.CENTER)
        query_button.add_css_class("flat")
        query_button.connect("clicked", lambda *_args: self.query_url())
        self.url_row.add_suffix(query_button)
        self.url_row.connect("entry-activated", lambda *_args: self.query_url())
        group.add(self.url_row)

        self.remote_row = Adw.ActionRow(visible=False)
        self.download_button = Gtk.Button(label=_("Download"), valign=Gtk.Align.CENTER)
        self.download_button.add_css_class("suggested-action")
        self.download_button.connect("clicked", lambda *_args: self.download_remote())
        self.remote_row.add_suffix(self.download_button)
        group.add(self.remote_row)

        self.source_row = Adw.ActionRow(visible=False)
        self.source_row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic"))
        group.add(self.source_row)
        return group

    def _build_target_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Target"))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        refresh.add_css_class("flat")
        refresh.set_tooltip_text(_("Refresh the device list"))
        refresh.connect("clicked", lambda *_args: self.refresh_disks())
        group.set_header_suffix(refresh)

        self.disk_row = Adw.ComboRow(title=_("Device"), model=Gtk.StringList.new([]))
        self.disk_row.connect("notify::selected", self._on_disk_selected)
        group.add(self.disk_row)
        return group

    def _build_pi_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Raspberry Pi OS",
            description=_("Applied on first boot through bootfs/custom.toml."),
            visible=False,
        )
        self.pi_group = group

        self.hostname_row = Adw.EntryRow(title=_("Hostname"))
        self.username_row = Adw.EntryRow(title=_("Username"))
        self.password_row = Adw.PasswordEntryRow(title=_("Password"))
        group.add(self.hostname_row)
        group.add(self.username_row)
        group.add(self.password_row)

        self._keys = list_public_keys()
        labels = [_("No key")] + [path.name for path, _key in self._keys]
        self.key_row = Adw.ComboRow(title=_("SSH public key"), model=Gtk.StringList.new(labels))
        if self._keys:
            self.key_row.set_selected(1)
        group.add(self.key_row)

        self.ssh_password_row = Adw.SwitchRow(
            title=_("Password login over SSH"),
            subtitle=_("Keep this on if the machine will be enrolled over SSH with a password."),
            active=True,
        )
        group.add(self.ssh_password_row)

        wifi = Adw.ExpanderRow(title=_("Wi-Fi"), subtitle=_("Leave empty to skip wireless setup"))
        self.wifi_ssid_row = Adw.EntryRow(title="SSID")
        self.wifi_password_row = Adw.PasswordEntryRow(title=_("Wi-Fi password"))
        self.wifi_country_row = Adw.EntryRow(title=_("Country code"))
        self.wifi_country_row.set_text(system_country())
        for row in (self.wifi_ssid_row, self.wifi_password_row, self.wifi_country_row):
            wifi.add_row(row)
        group.add(wifi)

        locale = Adw.ExpanderRow(title=_("Locale"))
        self.keymap_row = Adw.EntryRow(title=_("Keyboard layout"))
        self.keymap_row.set_text(system_keymap())
        self.timezone_row = Adw.EntryRow(title=_("Time zone"))
        self.timezone_row.set_text(system_timezone())
        locale.add_row(self.keymap_row)
        locale.add_row(self.timezone_row)
        group.add(locale)
        return group

    def _build_options_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Options"))
        self.verify_row = Adw.SwitchRow(
            title=_("Verify after writing"),
            subtitle=_("Reads the data back and compares SHA-256. Roughly doubles the time."),
            active=True,
        )
        group.add(self.verify_row)
        return group

    def _build_action_area(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("toolbar")
        box.set_margin_top(10)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        self.write_button = Gtk.Button(label=_("Write"), halign=Gtk.Align.CENTER, sensitive=False)
        self.write_button.add_css_class("destructive-action")
        self.write_button.add_css_class("pill")
        self.write_button.connect("clicked", lambda *_args: self.confirm_and_write())
        box.append(self.write_button)

        self.cancel_button = Gtk.Button(label=_("Cancel"), halign=Gtk.Align.CENTER, visible=False)
        self.cancel_button.connect("clicked", lambda *_args: self.cancel_job())
        box.append(self.cancel_button)

        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        box.append(self.progress)

        self.status_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, visible=False)
        self.status_label.add_css_class("dim-label")
        box.append(self.status_label)
        return box

    # ------------------------------------------------------------ helpers --

    def _fit_to_content(self) -> bool:
        """Resize the window to its content's natural height.

        Showing or hiding the Raspberry Pi panel changes the content height a
        lot; without this the window did not grow and the settings were
        clipped. Leave a window the user maximised alone.
        """
        if not self.is_maximized() and not self.is_fullscreen():
            self.set_default_size(self.get_width() or 620, -1)
        return False

    def notify_user(self, message: str, style: str = "") -> None:
        self.banner.set_title(message)
        self.banner.set_revealed(bool(message))
        for css in ("error", "warning"):
            self.banner.remove_css_class(css)
        if style:
            self.banner.add_css_class(style)

    def set_status(self, message: str) -> None:
        self.status_label.set_text(message)
        self.status_label.set_visible(bool(message))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.write_button.set_visible(not busy)
        self.cancel_button.set_visible(busy)
        self.progress.set_visible(busy)
        for widget in (
            self.file_row, self.url_row, self.remote_row, self.disk_row,
            self.pi_group, self.verify_row,
        ):
            widget.set_sensitive(not busy)
        if not busy:
            self._update_ready()

    def selected_disk(self) -> Disk | None:
        index = self.disk_row.get_selected()
        if 0 <= index < len(self._disks):
            return self._disks[index]
        return None

    def _update_ready(self) -> None:
        ready = self._source is not None and self.selected_disk() is not None
        self.write_button.set_sensitive(ready and not self._busy)

    # ------------------------------------------------------------ devices --

    def refresh_disks(self) -> None:
        try:
            self._disks = list_disks()
        except Exception as exc:
            self.notify_user(_("Could not list devices: {error}").format(error=exc), "error")
            self._disks = []

        model = Gtk.StringList.new([f"{d.title} — {d.path}" for d in self._disks])
        self.disk_row.set_model(model)
        if self._disks:
            self.disk_row.set_selected(0)
            self.disk_row.set_subtitle(self._disks[0].subtitle)
        else:
            self.disk_row.set_subtitle(
                _("No writable device — insert a USB drive or a memory card.")
            )
        self._update_ready()

    def _on_disk_selected(self, *_args) -> None:
        disk = self.selected_disk()
        if disk:
            self.disk_row.set_subtitle(disk.subtitle)
        self._update_ready()

    # ------------------------------------------------------------- source --

    def choose_file(self) -> None:
        dialog = Gtk.FileDialog(title=_("Choose an image"))
        image_filter = Gtk.FileFilter()
        image_filter.set_name(_("Disk images"))
        for pattern in IMAGE_PATTERNS:
            image_filter.add_pattern(pattern)
        any_filter = Gtk.FileFilter()
        any_filter.set_name(_("All files"))
        any_filter.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(image_filter)
        filters.append(any_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(image_filter)
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        path = gfile.get_path()
        if path:
            self.load_source(Path(path))

    def load_source(self, path: Path) -> None:
        self.set_status(_("Inspecting the image…"))
        self.file_row.set_subtitle(str(path))

        def work() -> None:
            try:
                probed = source_mod.probe(path)
            except Exception as exc:
                GLib.idle_add(self._source_failed, str(exc))
                return
            GLib.idle_add(self._source_ready, probed)

        threading.Thread(target=work, daemon=True).start()

    def _source_failed(self, message: str) -> bool:
        self._source = None
        self.source_row.set_visible(False)
        self.pi_group.set_visible(False)
        self.notify_user(_("Could not read the image: {error}").format(error=message), "error")
        self.set_status("")
        self._update_ready()
        return False

    def _source_ready(self, probed: source_mod.ImageSource) -> bool:
        self._source = probed
        self.notify_user("")
        self.set_status("")

        bits = [
            probed.format_label,
            _("{size} decompressed").format(size=human_bytes(probed.payload_size)),
        ]
        if probed.is_pi_image:
            bits.append(_("Raspberry Pi OS detected"))
        self.source_row.set_title(probed.name)
        self.source_row.set_subtitle(" · ".join(bits))
        self.source_row.set_visible(True)
        self.pi_group.set_visible(probed.is_pi_image)
        if probed.is_pi_image and not self.hostname_row.get_text():
            self.username_row.set_text(GLib.get_user_name() or "")
        self._update_ready()
        GLib.idle_add(self._fit_to_content)  # once the layout has settled
        return False

    # ---------------------------------------------------------------- URL --

    def query_url(self) -> None:
        url = self.url_row.get_text().strip()
        if not url:
            return
        self.set_status(_("Querying the URL…"))
        self.remote_row.set_visible(False)

        def work() -> None:
            try:
                info = remote.query(url)
            except Exception as exc:
                GLib.idle_add(self._query_failed, str(exc))
                return
            GLib.idle_add(self._query_ready, info)

        threading.Thread(target=work, daemon=True).start()

    def _query_failed(self, message: str) -> bool:
        self.notify_user(_("Query failed: {error}").format(error=message), "error")
        self.set_status("")
        return False

    def _query_ready(self, info: remote.RemoteInfo) -> bool:
        self._remote = info
        self.remote_row.set_title(info.filename)
        self.remote_row.set_subtitle(info.summary)
        self.remote_row.set_visible(True)
        self.notify_user(info.warning, "warning" if info.warning else "")
        self.set_status("")
        return False

    def download_remote(self) -> None:
        if self._remote is None:
            return
        info = self._remote
        self._set_busy(True)
        self.set_status(_("Downloading {name}…").format(name=info.filename))
        self._cancel_download = threading.Event()  # fresh for every download

        def progress(done: int, total: int | None) -> None:
            GLib.idle_add(self._show_progress, stage_label("download"), done, total or 0)

        def work() -> None:
            try:
                path = remote.download(info, on_progress=progress, cancel=self._cancel_download)
            except Exception as exc:
                GLib.idle_add(self._download_failed, str(exc))
                return
            GLib.idle_add(self._download_done, path)

        threading.Thread(target=work, daemon=True).start()

    def _download_failed(self, message: str) -> bool:
        self._set_busy(False)
        self.notify_user(_("Download failed: {error}").format(error=message), "error")
        self.set_status("")
        return False

    def _download_done(self, path: Path) -> bool:
        self._set_busy(False)
        self.set_status("")
        self.load_source(path)
        return False

    def _show_progress(self, stage: str, done: int, total: int) -> bool:
        if total > 0:
            self.progress.set_fraction(min(1.0, done / total))
            self.progress.set_text(f"{stage} · {human_bytes(done)} / {human_bytes(total)}")
        else:
            self.progress.pulse()
            self.progress.set_text(f"{stage} · {human_bytes(done)}")
        return False

    # -------------------------------------------------------------- write --

    def _build_pi_config(self) -> PiConfig | None:
        if not self.pi_group.get_visible():
            return None
        keys: list[str] = []
        index = self.key_row.get_selected()
        if 1 <= index <= len(self._keys):
            keys.append(self._keys[index - 1][1])

        config = PiConfig(
            hostname=self.hostname_row.get_text().strip(),
            username=self.username_row.get_text().strip(),
            ssh_password_auth=self.ssh_password_row.get_active(),
            authorized_keys=keys,
            keymap=self.keymap_row.get_text().strip() or "us",
            timezone=self.timezone_row.get_text().strip() or "Etc/UTC",
            wifi_ssid=self.wifi_ssid_row.get_text().strip(),
            wifi_password=self.wifi_password_row.get_text(),
            wifi_country=self.wifi_country_row.get_text().strip(),
        )
        config.password_hash = hash_password(self.password_row.get_text())
        config.validate()
        return config

    def confirm_and_write(self) -> None:
        disk = self.selected_disk()
        if disk is None or self._source is None:
            return

        try:
            config = self._build_pi_config()
        except ConfigError as exc:
            self.notify_user(
                _("The Raspberry Pi settings are incomplete: {error}").format(error=exc), "error"
            )
            return

        body = "\n".join([
            disk.title,
            disk.path,
            "",
            _("Image: {image}").format(image=self._source.name),
            _("All existing data on the device will be permanently lost."),
        ])
        dialog = Adw.AlertDialog(heading=_("Erase everything on this device?"), body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("write", _("Write"))
        dialog.set_response_appearance("write", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_confirm_response, disk, config)
        dialog.present(self)

    def _on_confirm_response(
        self, dialog: Adw.AlertDialog, response: str, disk: Disk, config: PiConfig | None
    ) -> None:
        if response == "write":
            self.start_write(disk, config)

    def start_write(self, disk: Disk, config: PiConfig | None) -> None:
        assert self._source is not None
        toml_path = None
        if config is not None:
            try:
                handle = Path(GLib.get_user_runtime_dir() or "/tmp") / "ingot-custom.toml"
                handle.write_text(config.to_toml(), encoding="utf-8")
                handle.chmod(0o600)
                toml_path = handle
            except (OSError, ConfigError) as exc:
                self.notify_user(
                    _("Could not write custom.toml: {error}").format(error=exc), "error"
                )
                return
        self._toml_tmp = toml_path

        request = WriteRequest(
            device=disk.path,
            source=self._source.path,
            compression=self._source.compression,
            payload_size=self._source.payload_size or 0,
            verify=self.verify_row.get_active(),
            toml_path=toml_path,
        )

        self._set_busy(True)
        self.notify_user("")
        self.set_status(_("Waiting for authorization…"))
        self.progress.set_fraction(0.0)
        self.progress.set_text(_("Preparing"))

        self._job = WriteJob(
            request, lambda kind, *payload: GLib.idle_add(self._on_job_event, kind, payload)
        )
        self._job.start()

    def cancel_job(self) -> None:
        if self._job is not None:
            self.set_status(_("Cancelling…"))
            self._job.cancel()
        self._cancel_download.set()

    def _on_job_event(self, kind: str, payload: tuple) -> bool:
        if kind == "status":
            self.set_status(payload[0])
        elif kind == "progress":
            stage, done, total = payload
            self._show_progress(stage_label(stage), done, total)
        elif kind == "fact":
            key, value = payload
            if key == "pi_seeded":
                if value == "yes":
                    self.set_status(_("custom.toml was written to the boot partition."))
                else:
                    reason = describe_seed_reason(value.split(":", 1)[-1])
                    self.notify_user(
                        _(
                            "The Raspberry Pi settings could not be applied ({reason}) "
                            "— the image was still written."
                        ).format(reason=reason),
                        "warning",
                    )
        elif kind == "finished":
            self._on_job_finished(payload[0])
        return False

    def _on_job_finished(self, outcome: WriteOutcome) -> None:
        self._set_busy(False)
        self._job = None
        if self._toml_tmp is not None:
            self._toml_tmp.unlink(missing_ok=True)
            self._toml_tmp = None

        if outcome.ok:
            size = human_bytes(int(outcome.facts.get("written", 0)))
            self.progress.set_fraction(1.0)
            if self.verify_row.get_active():
                message = _("Done — {size} written and verified. You can remove the device.")
            else:
                message = _("Done — {size} written. You can remove the device.")
            self.set_status(message.format(size=size))
            self.notify_user("")
        else:
            self.set_status("")
            self.notify_user(outcome.error, "error")
        self.refresh_disks()
