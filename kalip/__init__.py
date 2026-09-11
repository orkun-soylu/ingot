"""kalıp — Linux için imaj yazıcı."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kalip")
except PackageNotFoundError:  # depodan calistirilirken paket kurulu olmayabilir
    __version__ = "0.0.0+kaynak"
