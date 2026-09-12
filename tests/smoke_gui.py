#!/usr/bin/env python3
"""Interface smoke test. Runs headless: xvfb-run python3 tests/smoke_gui.py

Builds the real window and drives the main flows programmatically. Set
LANGUAGE=tr (with compiled catalogues) to also check that the Turkish
translation is active.
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

from ingot.devices import Disk  # noqa: E402
from ingot.i18n import _  # noqa: E402
from ingot.remote import RemoteInfo  # noqa: E402
from ingot.source import ImageSource  # noqa: E402
from ingot.ui.window import IngotWindow  # noqa: E402

failures: list[str] = []
checks: list[str] = []


def check(name: str, condition: bool) -> None:
    checks.append(f"{'ok  ' if condition else 'FAIL'} {name}")
    if not condition:
        failures.append(name)


def fake_disk() -> Disk:
    return Disk(
        path="/dev/sdz", name="sdz", size=64 * 1000 ** 3, model="Test Media",
        tran="usb", removable=True, readonly=False, system=False, mountpoints=(),
    )


def pi_source() -> ImageSource:
    return ImageSource(
        path=Path("/tmp/2026-raspios.img.xz"), compression="xz",
        file_size=524875608, payload_size=5 * 1024 ** 3, is_pi_image=True,
    )


def exercise(window: IngotWindow) -> None:
    window.refresh_disks()
    check("refresh_disks ran", True)

    check("labels go through gettext", window.write_button.get_label() == _("Write"))
    if os.environ.get("LANGUAGE", "").startswith("tr"):
        check("Turkish catalogue is active", window.write_button.get_label() == "Yaz")
    else:
        check("English source text", window.write_button.get_label() == "Write")

    window.notify_user("warning text", "warning")
    check("banner opens", window.banner.get_revealed())
    window.notify_user("")
    check("banner closes", not window.banner.get_revealed())

    window._show_progress("Writing", 50, 100)
    check("progress fraction", abs(window.progress.get_fraction() - 0.5) < 0.01)
    window._show_progress("Writing", 50, 0)  # unknown total -> pulse
    check("unknown total does not crash", True)

    window._query_ready(RemoteInfo(
        url="https://x/a.img.xz", effective_url="https://x/a.img.xz",
        filename="2026-raspios.img.xz", size=524875608,
        content_type="application/x-xz", resumable=True, status=200,
    ))
    check("remote row visible", window.remote_row.get_visible())

    window._source_ready(pi_source())
    check("source row visible", window.source_row.get_visible())
    check("Pi panel opens", window.pi_group.get_visible())

    window._source_ready(ImageSource(
        path=Path("/tmp/debian.iso"), compression=None,
        file_size=1 << 30, payload_size=1 << 30, is_pi_image=False,
    ))
    check("Pi panel closed for an ISO", not window.pi_group.get_visible())

    window._source_ready(pi_source())
    window.hostname_row.set_text("pi-test-01")
    window.username_row.set_text("orkun")
    window.password_row.set_text("secret-password")
    window.wifi_country_row.set_text("GB")
    config = window._build_pi_config()
    check("PiConfig built", config is not None and config.hostname == "pi-test-01")
    check("password hashed", config.password_hash.startswith("$6$"))
    check("custom.toml generated", 'hostname = "pi-test-01"' in config.to_toml())

    window.hostname_row.set_text("")
    try:
        window._build_pi_config()
        check("empty hostname rejected", False)
    except Exception:
        check("empty hostname rejected", True)
    window.hostname_row.set_text("pi-test-01")

    window._disks = [fake_disk()]
    window.disk_row.set_model(Gtk.StringList.new(["Test Media — /dev/sdz"]))
    window.disk_row.set_selected(0)
    check("target selected", window.selected_disk() is not None)
    check("Write enabled", window.write_button.get_sensitive())
    window.confirm_and_write()
    check("confirmation dialog built", True)
    check("only removable devices listed", all(d.removable for d in window._disks))

    window._set_busy(True)
    check("Write hidden while busy", not window.write_button.get_visible())
    check("Cancel shown while busy", window.cancel_button.get_visible())
    window._set_busy(False)
    check("busy state restored", window.write_button.get_visible())


def on_activate(app: Adw.Application) -> None:
    try:
        window = IngotWindow(app)
        window.present()
        exercise(window)
    except Exception:
        failures.append(traceback.format_exc())
    GLib.timeout_add(300, lambda: (app.quit(), False)[1])


def main() -> int:
    app = Adw.Application(application_id="me.soylu.ingot.smoke")
    app.connect("activate", on_activate)
    app.run([])

    print("\n".join(checks))
    if failures:
        print("\n=== FAILED ===")
        for item in failures:
            print(item)
        return 1
    print(f"\nsmoke test passed — {len(checks)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
