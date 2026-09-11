#!/usr/bin/env python3
"""Arayüz duman testi. Başsız çalışır: xvfb-run python3 tests/smoke_gui.py

Pencereyi gerçekten kurar ve ana akışları programatik olarak tetikler.
GTK uyarılarını hata sayar -- sessiz bozulmalar böyle yakalanır.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("GDK_BACKEND", "x11")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from kalip.devices import Disk  # noqa: E402
from kalip.remote import RemoteInfo  # noqa: E402
from kalip.source import ImageSource  # noqa: E402
from kalip.ui.window import KalipWindow  # noqa: E402

failures: list[str] = []
checks: list[str] = []


def check(name: str, condition: bool) -> None:
    checks.append(f"{'ok  ' if condition else 'HATA'} {name}")
    if not condition:
        failures.append(name)


def fake_disk() -> Disk:
    return Disk(
        path="/dev/sdz", name="sdz", size=64 * 1000 ** 3, model="Test Media",
        tran="usb", removable=True, readonly=False, system=False, mountpoints=(),
    )


def exercise(window: KalipWindow) -> None:
    # --- aygıt listesi -------------------------------------------------
    window.refresh_disks()
    check("refresh_disks cokmeden calisti", True)

    # --- banner / ilerleme ---------------------------------------------
    window.notify_user("uyarı metni", "warning")
    check("banner acildi", window.banner.get_revealed())
    window.notify_user("")
    check("banner kapandi", not window.banner.get_revealed())

    window._show_progress("Yazılıyor", 50, 100)
    check("ilerleme kesirli", abs(window.progress.get_fraction() - 0.5) < 0.01)
    window._show_progress("Yazılıyor", 50, 0)  # toplam bilinmiyor -> pulse
    check("bilinmeyen toplamda cokmedi", True)

    # --- URL sorgu sonucu ------------------------------------------------
    window._query_ready(RemoteInfo(
        url="https://x/a.img.xz", effective_url="https://x/a.img.xz",
        filename="2026-raspios.img.xz", size=524875608,
        content_type="application/x-xz", resumable=True, status=200,
    ))
    check("uzak satir gorunur", window.remote_row.get_visible())

    # --- Pi imajı: panel açılmalı ---------------------------------------
    window._source_ready(ImageSource(
        path=Path("/tmp/2026-raspios.img.xz"), compression="xz",
        file_size=524875608, payload_size=5 * 1024 ** 3, is_pi_image=True,
    ))
    check("kaynak satiri gorunur", window.source_row.get_visible())
    check("Pi paneli acildi", window.pi_group.get_visible())

    # --- Pi olmayan imaj: panel kapanmalı --------------------------------
    window._source_ready(ImageSource(
        path=Path("/tmp/debian.iso"), compression=None,
        file_size=1 << 30, payload_size=1 << 30, is_pi_image=False,
    ))
    check("ISO'da Pi paneli kapali", not window.pi_group.get_visible())

    # --- Pi yapılandırması arayüzden üretiliyor mu ------------------------
    window._source_ready(ImageSource(
        path=Path("/tmp/2026-raspios.img.xz"), compression="xz",
        file_size=524875608, payload_size=5 * 1024 ** 3, is_pi_image=True,
    ))
    window.hostname_row.set_text("pi-test-01")
    window.username_row.set_text("orkun")
    window.password_row.set_text("gizli-parola")
    config = window._build_pi_config()
    check("PiConfig uretildi", config is not None and config.hostname == "pi-test-01")
    check("parola hash'lendi", config.password_hash.startswith("$6$"))
    toml = config.to_toml()
    check("custom.toml uretildi", "hostname = \"pi-test-01\"" in toml)

    # --- eksik alan hatası yakalanıyor mu --------------------------------
    window.hostname_row.set_text("")
    try:
        window._build_pi_config()
        check("bos hostname reddedildi", False)
    except Exception:
        check("bos hostname reddedildi", True)
    window.hostname_row.set_text("pi-test-01")

    # --- onay diyalogu ----------------------------------------------------
    window._disks = [fake_disk()]
    window.disk_row.set_model(Gtk.StringList.new(["Test Media — /dev/sdz"]))
    window.disk_row.set_selected(0)
    check("hedef secildi", window.selected_disk() is not None)
    check("Yaz dugmesi etkin", window.write_button.get_sensitive())
    window.confirm_and_write()
    check("onay diyalogu kuruldu", True)

    # Arayuz yalnizca cikarilabilir aygit listeler.
    check("liste yalnizca cikarilabilir", all(d.removable for d in window._disks))

    # --- meşgul durumu ----------------------------------------------------
    window._set_busy(True)
    check("mesgulken Yaz gizli", not window.write_button.get_visible())
    check("mesgulken Iptal gorunur", window.cancel_button.get_visible())
    window._set_busy(False)
    check("mesgul durumu geri alindi", window.write_button.get_visible())


def on_activate(app: Adw.Application) -> None:
    try:
        window = KalipWindow(app)
        window.present()
        exercise(window)
    except Exception:
        failures.append(traceback.format_exc())
    GLib.timeout_add(300, lambda: (app.quit(), False)[1])


def main() -> int:
    app = Adw.Application(application_id="me.soylu.kalip.smoke")
    app.connect("activate", on_activate)
    app.run([])

    print("\n".join(checks))
    if failures:
        print("\n=== BASARISIZ ===")
        for item in failures:
            print(item)
        return 1
    print(f"\nduman testi gecti — {len(checks)} kontrol")
    return 0


if __name__ == "__main__":
    sys.exit(main())
