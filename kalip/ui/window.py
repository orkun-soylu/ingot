"""kalıp ana penceresi."""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import remote, source as source_mod
from ..devices import Disk, human_bytes, list_disks
from ..picfg import ConfigError, PiConfig, hash_password, list_public_keys
from ..privileged import WriteJob, WriteOutcome, WriteRequest

IMAGE_PATTERNS = ("*.iso", "*.img", "*.raw", "*.xz", "*.gz", "*.zst", "*.zip")


class KalipWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="kalıp")
        self.set_default_size(620, 760)

        self._disks: list[Disk] = []
        self._source: source_mod.ImageSource | None = None
        self._remote: remote.RemoteInfo | None = None
        self._job: WriteJob | None = None
        self._busy = False
        self._toml_tmp: Path | None = None
        self._cancel_download = threading.Event()

        self.set_content(self._build())
        self.refresh_disks()

    # ---------------------------------------------------------------- yapı --

    def _build(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="kalıp", subtitle="imaj yazıcı"))
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
        box.append(self._build_action_area())

        clamp = Adw.Clamp(maximum_size=680, child=box)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(clamp)
        view.set_content(scroller)
        return view

    def _build_source_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Kaynak")

        self.file_row = Adw.ActionRow(
            title="İmaj dosyası", subtitle="Henüz seçilmedi", activatable=True
        )
        self.file_row.add_suffix(Gtk.Image.new_from_icon_name("document-open-symbolic"))
        self.file_row.connect("activated", lambda *_: self.choose_file())
        group.add(self.file_row)

        self.url_row = Adw.EntryRow(title="…veya URL")
        self.url_row.set_show_apply_button(False)
        query_button = Gtk.Button(label="Sorgula", valign=Gtk.Align.CENTER)
        query_button.add_css_class("flat")
        query_button.connect("clicked", lambda *_: self.query_url())
        self.url_row.add_suffix(query_button)
        self.url_row.connect("entry-activated", lambda *_: self.query_url())
        group.add(self.url_row)

        self.remote_row = Adw.ActionRow(visible=False)
        self.download_button = Gtk.Button(label="İndir", valign=Gtk.Align.CENTER)
        self.download_button.add_css_class("suggested-action")
        self.download_button.connect("clicked", lambda *_: self.download_remote())
        self.remote_row.add_suffix(self.download_button)
        group.add(self.remote_row)

        self.source_row = Adw.ActionRow(visible=False)
        self.source_row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic"))
        group.add(self.source_row)
        return group

    def _build_target_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Hedef")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        refresh.add_css_class("flat")
        refresh.set_tooltip_text("Aygıt listesini yenile")
        refresh.connect("clicked", lambda *_: self.refresh_disks())
        group.set_header_suffix(refresh)

        self.disk_row = Adw.ComboRow(title="Aygıt", model=Gtk.StringList.new([]))
        self.disk_row.connect("notify::selected", self._on_disk_selected)
        group.add(self.disk_row)
        return group

    def _build_pi_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Raspberry Pi OS",
            description="İlk açılışta uygulanacak ayarlar (bootfs/custom.toml).",
            visible=False,
        )
        self.pi_group = group

        self.hostname_row = Adw.EntryRow(title="Hostname")
        self.username_row = Adw.EntryRow(title="Kullanıcı adı")
        self.password_row = Adw.PasswordEntryRow(title="Parola")
        group.add(self.hostname_row)
        group.add(self.username_row)
        group.add(self.password_row)

        self._keys = list_public_keys()
        labels = ["Anahtar ekleme"] + [path.name for path, _ in self._keys]
        self.key_row = Adw.ComboRow(
            title="SSH açık anahtarı", model=Gtk.StringList.new(labels)
        )
        if self._keys:
            self.key_row.set_selected(1)
        group.add(self.key_row)

        self.ssh_password_row = Adw.SwitchRow(
            title="Parola ile SSH girişi",
            subtitle="timar'ın enrolment akışı buna dayanır — ilk kurulumda açık bırak.",
            active=True,
        )
        group.add(self.ssh_password_row)

        wifi = Adw.ExpanderRow(title="WiFi", subtitle="Boş bırakılırsa kablosuz ayarlanmaz")
        self.wifi_ssid_row = Adw.EntryRow(title="SSID")
        self.wifi_password_row = Adw.PasswordEntryRow(title="WiFi parolası")
        self.wifi_country_row = Adw.EntryRow(title="Ülke kodu")
        self.wifi_country_row.set_text("TR")
        for row in (self.wifi_ssid_row, self.wifi_password_row, self.wifi_country_row):
            wifi.add_row(row)
        group.add(wifi)

        locale = Adw.ExpanderRow(title="Yerel ayarlar")
        self.keymap_row = Adw.EntryRow(title="Klavye")
        self.keymap_row.set_text("tr")
        self.timezone_row = Adw.EntryRow(title="Saat dilimi")
        self.timezone_row.set_text("Europe/Istanbul")
        locale.add_row(self.keymap_row)
        locale.add_row(self.timezone_row)
        group.add(locale)
        return group

    def _build_options_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Seçenekler")
        self.verify_row = Adw.SwitchRow(
            title="Yazdıktan sonra doğrula",
            subtitle="Geri okuyup SHA-256 karşılaştırır. Süreyi yaklaşık iki katına çıkarır.",
            active=True,
        )
        group.add(self.verify_row)
        return group

    def _build_action_area(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        self.write_button = Gtk.Button(label="Yaz", halign=Gtk.Align.CENTER, sensitive=False)
        self.write_button.add_css_class("destructive-action")
        self.write_button.add_css_class("pill")
        self.write_button.connect("clicked", lambda *_: self.confirm_and_write())
        box.append(self.write_button)

        self.cancel_button = Gtk.Button(
            label="İptal", halign=Gtk.Align.CENTER, visible=False
        )
        self.cancel_button.connect("clicked", lambda *_: self.cancel_job())
        box.append(self.cancel_button)

        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        box.append(self.progress)

        self.status_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.status_label.add_css_class("dim-label")
        box.append(self.status_label)
        return box

    # --------------------------------------------------------------- yardım --

    def notify_user(self, message: str, style: str = "") -> None:
        self.banner.set_title(message)
        self.banner.set_revealed(bool(message))
        for css in ("error", "warning"):
            self.banner.remove_css_class(css)
        if style:
            self.banner.add_css_class(style)

    def set_status(self, message: str) -> None:
        self.status_label.set_text(message)

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

    # -------------------------------------------------------------- aygıtlar --

    def refresh_disks(self) -> None:
        try:
            self._disks = list_disks()
        except Exception as exc:
            self.notify_user(f"Aygıtlar listelenemedi: {exc}", "error")
            self._disks = []

        model = Gtk.StringList.new([f"{d.title} — {d.path}" for d in self._disks])
        self.disk_row.set_model(model)
        if self._disks:
            self.disk_row.set_selected(0)
            self.disk_row.set_subtitle(self._disks[0].subtitle)
        else:
            self.disk_row.set_subtitle("Yazılabilir aygıt yok — USB veya kart tak.")
        self._update_ready()

    def _on_disk_selected(self, *_args) -> None:
        disk = self.selected_disk()
        if disk:
            self.disk_row.set_subtitle(disk.subtitle)
        self._update_ready()

    # --------------------------------------------------------------- kaynak --

    def choose_file(self) -> None:
        dialog = Gtk.FileDialog(title="İmaj seç")
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Disk imajları")
        for pattern in IMAGE_PATTERNS:
            image_filter.add_pattern(pattern)
        any_filter = Gtk.FileFilter()
        any_filter.set_name("Tüm dosyalar")
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
        self.set_status("İmaj inceleniyor…")
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
        self.notify_user(f"İmaj okunamadı: {message}", "error")
        self.set_status("")
        self._update_ready()
        return False

    def _source_ready(self, probed: source_mod.ImageSource) -> bool:
        self._source = probed
        self.notify_user("")
        self.set_status("")

        size = human_bytes(probed.payload_size) if probed.payload_size else "bilinmiyor"
        self.source_row.set_title(probed.name)
        self.source_row.set_subtitle(
            f"{probed.format_label} · açılmış {size}"
            + (" · Raspberry Pi OS algılandı" if probed.is_pi_image else "")
        )
        self.source_row.set_visible(True)
        self.pi_group.set_visible(probed.is_pi_image)
        if probed.is_pi_image and not self.hostname_row.get_text():
            self.username_row.set_text(GLib.get_user_name() or "")
        self._update_ready()
        return False

    # ------------------------------------------------------------------ URL --

    def query_url(self) -> None:
        url = self.url_row.get_text().strip()
        if not url:
            return
        self.set_status("URL sorgulanıyor…")
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
        self.notify_user(f"Sorgulanamadı: {message}", "error")
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
        self.set_status(f"{info.filename} indiriliyor…")
        self._cancel_download = threading.Event()  # her indirme icin taze

        def progress(done: int, total: int | None) -> None:
            GLib.idle_add(self._show_progress, "İndiriliyor", done, total or 0)

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
        self.notify_user(f"İndirme başarısız: {message}", "error")
        self.set_status("")
        return False

    def _download_done(self, path: Path) -> bool:
        self._set_busy(False)
        self.set_status("")
        self.load_source(path)
        return False

    def _show_progress(self, stage: str, done: int, total: int) -> bool:
        if total > 0:
            fraction = min(1.0, done / total)
            self.progress.set_fraction(fraction)
            self.progress.set_text(f"{stage} · {human_bytes(done)} / {human_bytes(total)}")
        else:
            self.progress.pulse()
            self.progress.set_text(f"{stage} · {human_bytes(done)}")
        return False

    # ---------------------------------------------------------------- yazma --

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
            wifi_country=self.wifi_country_row.get_text().strip() or "TR",
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
            self.notify_user(f"Pi ayarları eksik: {exc}", "error")
            return

        dialog = Adw.AlertDialog(
            heading="Bu aygıttaki her şey silinecek",
            body=(
                f"{disk.title}\n{disk.path}\n\n"
                f"Yazılacak: {self._source.name}\n"
                "Aygıttaki mevcut veri geri döndürülemez biçimde kaybolur."
            ),
        )
        dialog.add_response("cancel", "Vazgeç")
        dialog.add_response("write", "Yaz")
        dialog.set_response_appearance("write", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_confirm_response, disk, config)
        dialog.present(self)

    def _on_confirm_response(
        self, dialog: Adw.AlertDialog, response: str, disk: Disk, config: PiConfig | None
    ) -> None:
        if response != "write":
            return
        self.start_write(disk, config)

    def start_write(self, disk: Disk, config: PiConfig | None) -> None:
        assert self._source is not None
        toml_path = None
        if config is not None:
            try:
                handle = Path(GLib.get_user_runtime_dir() or "/tmp") / "kalip-custom.toml"
                handle.write_text(config.to_toml(), encoding="utf-8")
                handle.chmod(0o600)
                toml_path = handle
            except (OSError, ConfigError) as exc:
                self.notify_user(f"custom.toml yazılamadı: {exc}", "error")
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
        self.set_status("Yetkilendirme bekleniyor…")
        self.progress.set_fraction(0.0)
        self.progress.set_text("Hazırlanıyor")

        self._job = WriteJob(request, lambda kind, *payload: GLib.idle_add(
            self._on_job_event, kind, payload
        ))
        self._job.start()

    def cancel_job(self) -> None:
        if self._job is not None:
            self.set_status("İptal ediliyor…")
            self._job.cancel()
        self._cancel_download.set()

    def _on_job_event(self, kind: str, payload: tuple) -> bool:
        if kind == "status":
            self.set_status(payload[0])
        elif kind == "progress":
            stage, done, total = payload
            label = {"write": "Yazılıyor", "verify": "Doğrulanıyor"}.get(stage, stage)
            self._show_progress(label, done, total)
        elif kind == "fact":
            key, value = payload
            if key == "pi_seeded":
                if value == "yes":
                    self.set_status("custom.toml bootfs'e yazıldı.")
                else:
                    reason = value.split(":", 1)[-1]
                    self.notify_user(
                        f"Pi ayarları uygulanamadı ({reason}) — imaj yine de yazıldı.",
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
            written = int(outcome.facts.get("written", 0))
            verified = self.verify_row.get_active()
            self.progress.set_fraction(1.0)
            self.set_status(
                f"Bitti — {human_bytes(written)} yazıldı"
                + (" ve doğrulandı." if verified else ".")
                + " Aygıtı çıkarabilirsin."
            )
            self.notify_user("")
        else:
            self.set_status("")
            self.notify_user(outcome.error, "error")
        self.refresh_disks()
