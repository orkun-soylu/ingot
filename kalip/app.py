"""Uygulama girişi."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio  # noqa: E402

from . import __version__
from .ui.window import KalipWindow

APP_ID = "me.soylu.kalip"


class KalipApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window: KalipWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = KalipWindow(self)
        self.window.present()

    def do_open(self, files, n_files, hint) -> None:
        self.do_activate()
        assert self.window is not None
        for gfile in files:
            path = gfile.get_path()
            if path:
                from pathlib import Path

                self.window.load_source(Path(path))
                break


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    if "--version" in argv:
        print(f"kalip {__version__}")
        return 0
    return KalipApplication().run(argv)
