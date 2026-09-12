"""Ingot — a disk image writer for Linux."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ingot")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0+source"
