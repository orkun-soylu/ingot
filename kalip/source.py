"""İmaj kaynağı: biçim tespiti, açılmış boyut, akış açma, Pi OS imzası."""

from __future__ import annotations

import gzip
import lzma
import os
import struct
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAGICS = {
    b"\xfd7zXZ\x00": "xz",
    b"\x1f\x8b": "gzip",
    b"\x28\xb5\x2f\xfd": "zstd",
    b"PK\x03\x04": "zip",
}

SUPPORTED_SUFFIXES = (".iso", ".img", ".raw", ".xz", ".gz", ".zst", ".zip", ".bz2")

# MBR bölüm tipi kodları
FAT_TYPES = {0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E}
LINUX_TYPE = 0x83

MBR_PEEK = 1 << 20  # Pi imzası için açılması yeterli olan ön kısım


@dataclass
class ImageSource:
    path: Path
    compression: str | None
    file_size: int
    payload_size: int | None  # açılmış boyut; bilinmiyorsa None
    is_pi_image: bool = False

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def format_label(self) -> str:
        return {"xz": "xz", "gzip": "gzip", "zstd": "zstd", "zip": "zip"}.get(
            self.compression, "ham imaj"
        )


def detect_compression(path: Path) -> str | None:
    with path.open("rb") as fh:
        head = fh.read(8)
    for magic, kind in MAGICS.items():
        if head.startswith(magic):
            return kind
    return None


def _xz_payload_size(path: Path) -> int | None:
    try:
        out = subprocess.run(
            ["xz", "--robot", "-l", str(path)], capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if parts and parts[0] == "totals" and len(parts) > 4:
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def _zstd_payload_size(path: Path) -> int | None:
    try:
        out = subprocess.run(
            ["zstd", "-lv", str(path)], capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    for line in out.splitlines():
        if "Decompressed Size:" in line and "(" in line:
            chunk = line.split("(", 1)[1].split(")", 1)[0]  # "7340032 B"
            digits = chunk.split()[0]
            if digits.isdigit():
                return int(digits)
    return None


def _gzip_payload_size(path: Path) -> int | None:
    """gzip ISIZE alanı 2^32 modülüdür -> 4 GiB üstü açılmış boyut sarar.

    Pi imajları 5 GiB civarı olduğu için .gz'de bu değer *tahmindir*; writer
    toplamı aşan yazmayı belirsiz ilerlemeye çevirerek tolere eder.
    """
    if path.stat().st_size < 4:
        return None
    with path.open("rb") as fh:
        fh.seek(-4, os.SEEK_END)
        isize = struct.unpack("<I", fh.read(4))[0]
    return isize or None


def _zip_payload_size(path: Path) -> int | None:
    try:
        with zipfile.ZipFile(path) as zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            if len(members) != 1:
                return None
            return members[0].file_size
    except (zipfile.BadZipFile, OSError):
        return None


def payload_size(path: Path, compression: str | None) -> int | None:
    if compression is None:
        return path.stat().st_size
    return {
        "xz": _xz_payload_size,
        "gzip": _gzip_payload_size,
        "zstd": _zstd_payload_size,
        "zip": _zip_payload_size,
    }[compression](path)


def open_stream(path: Path, compression: str | None):
    """Açılmış baytları veren okunabilir akış döndürür.

    zstd için stdlib yok (3.14'e kadar) -> zstdcat alt süreci.
    """
    if compression is None:
        return path.open("rb"), None
    if compression == "xz":
        return lzma.open(path, "rb"), None
    if compression == "gzip":
        return gzip.open(path, "rb"), None
    if compression == "zip":
        zf = zipfile.ZipFile(path)
        member = [i for i in zf.infolist() if not i.is_dir()][0]
        return zf.open(member, "r"), zf
    if compression == "zstd":
        proc = subprocess.Popen(
            ["zstdcat", "--", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        return proc.stdout, proc
    raise ValueError(f"bilinmeyen sıkıştırma: {compression}")


def close_stream(stream, holder) -> None:
    """Akışı ve varsa onu tutan nesneyi kapat.

    zstd alt süreci kill'den sonra wait edilmezse zombi kalır.
    """
    if stream is not None:
        try:
            stream.close()
        except Exception:
            pass
    if holder is None:
        return
    if isinstance(holder, subprocess.Popen):
        holder.kill()
        holder.wait()
    else:
        holder.close()


def looks_like_pi_image(path: Path, compression: str | None) -> bool:
    """İmajın ilk MiB'ini açıp MBR'ye bakar.

    Pi OS imzası: 1. bölüm FAT (bootfs), 2. bölüm Linux (rootfs).
    ISO'lar isohybrid olduğu için bu kalıba uymaz -> yanlış pozitif vermez.
    """
    stream = holder = None
    try:
        stream, holder = open_stream(path, compression)
        head = stream.read(MBR_PEEK)
    except Exception:
        return False
    finally:
        close_stream(stream, holder)

    if len(head) < 512 or head[510:512] != b"\x55\xaa":
        return False

    types = []
    for i in range(4):
        entry = head[446 + i * 16 : 446 + (i + 1) * 16]
        ptype = entry[4]
        nsectors = struct.unpack("<I", entry[12:16])[0]
        if nsectors:
            types.append(ptype)

    return len(types) >= 2 and types[0] in FAT_TYPES and LINUX_TYPE in types[1:]


def probe(path: str | Path) -> ImageSource:
    path = Path(path).expanduser().resolve()
    compression = detect_compression(path)
    return ImageSource(
        path=path,
        compression=compression,
        file_size=path.stat().st_size,
        payload_size=payload_size(path, compression),
        is_pi_image=looks_like_pi_image(path, compression),
    )
