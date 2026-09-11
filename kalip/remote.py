"""URL'den imaj: önce sorgula (Proxmox'un 'Download from URL'ü gibi), sonra indir.

Her şey curl ile. İlerleme, curl'ün çıktısını ayrıştırarak değil hedef
dosyanın boyutunu yoklayarak ölçülür -- format değişikliğine dayanıklı.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from .devices import human_bytes
from .source import SUPPORTED_SUFFIXES

USER_AGENT = "kalip/0.1 (+https://github.com/orkun-soylu/ingot)"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kalip"

_DISPOSITION = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)
_CONTENT_RANGE = re.compile(r"bytes\s+\d+-\d+/(\d+)", re.IGNORECASE)


class RemoteError(RuntimeError):
    pass


@dataclass
class RemoteInfo:
    url: str
    effective_url: str
    filename: str
    size: int | None
    content_type: str
    resumable: bool
    status: int

    @property
    def supported(self) -> bool:
        return self.filename.lower().endswith(SUPPORTED_SUFFIXES)

    @property
    def size_human(self) -> str:
        return human_bytes(self.size)

    @property
    def summary(self) -> str:
        bits = [self.size_human]
        if self.content_type:
            bits.append(self.content_type)
        if self.resumable:
            bits.append("devam ettirilebilir")
        return " · ".join(bits)

    @property
    def warning(self) -> str:
        if not self.supported:
            exts = ", ".join(SUPPORTED_SUFFIXES)
            return f"Uzantı tanıdık değil (beklenen: {exts}). Yine de yazabilirsin."
        if self.size is None:
            return "Sunucu boyut bildirmedi — ilerleme çubuğu belirsiz olacak."
        return ""


def _split_header_blocks(raw: str) -> list[list[str]]:
    blocks, current = [], []
    for line in raw.splitlines():
        line = line.rstrip("\r")
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _header(block: list[str], name: str) -> str | None:
    prefix = name.lower() + ":"
    for line in block:
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def _filename_from(block: list[str], effective_url: str) -> str:
    disposition = _header(block, "content-disposition")
    if disposition:
        match = _DISPOSITION.search(disposition)
        if match:
            candidate = unquote(match.group(1)).strip()
            if candidate:
                return Path(candidate).name
    path = unquote(urlparse(effective_url).path)
    name = Path(path).name
    return name or "image.bin"


def _curl(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["curl", "-sS", "-A", USER_AGENT, "--max-time", str(timeout), *args],
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # curl yok
        raise RemoteError(f"curl çalıştırılamadı: {exc}") from exc


def query(url: str, timeout: int = 25) -> RemoteInfo:
    """HEAD ile sorgula; sunucu HEAD kabul etmezse 1 baytlık range GET'e düş."""
    url = url.strip()
    if not url:
        raise RemoteError("URL boş.")
    if urlparse(url).scheme not in ("http", "https"):
        raise RemoteError("Yalnızca http/https destekleniyor.")

    marker = "__KALIP__"
    fmt = f"\\n{marker}\\n%{{url_effective}}\\n%{{http_code}}\\n"
    proc = _curl(["-I", "-L", "-D", "-", "-o", os.devnull, "-w", fmt, "--", url], timeout)
    if proc.returncode != 0:
        raise RemoteError(proc.stderr.strip() or f"curl {proc.returncode} ile çıktı.")

    raw, _, tail = proc.stdout.rpartition(marker)
    tail_lines = [line for line in tail.splitlines() if line.strip()]
    effective_url = tail_lines[0] if tail_lines else url
    status = int(tail_lines[1]) if len(tail_lines) > 1 and tail_lines[1].isdigit() else 0

    blocks = _split_header_blocks(raw)
    last = blocks[-1] if blocks else []
    length = _header(last, "content-length")
    size = int(length) if length and length.isdigit() else None

    if status in (403, 405, 501) or (status == 200 and size is None):
        info = _query_via_range(url, timeout)
        if info is not None:
            return info

    if status >= 400:
        raise RemoteError(f"Sunucu HTTP {status} döndü.")

    return RemoteInfo(
        url=url,
        effective_url=effective_url,
        filename=_filename_from(last, effective_url),
        size=size,
        content_type=(_header(last, "content-type") or "").split(";")[0].strip(),
        resumable=(_header(last, "accept-ranges") or "").lower() == "bytes",
        status=status,
    )


def _query_via_range(url: str, timeout: int) -> RemoteInfo | None:
    """HEAD'i reddeden sunucular için: ilk baytı iste, Content-Range'den boyutu oku."""
    marker = "__KALIP__"
    fmt = f"\\n{marker}\\n%{{url_effective}}\\n%{{http_code}}\\n"
    proc = _curl(
        ["-L", "-r", "0-0", "-D", "-", "-o", os.devnull, "-w", fmt, "--", url], timeout
    )
    if proc.returncode != 0:
        return None

    raw, _, tail = proc.stdout.rpartition(marker)
    tail_lines = [line for line in tail.splitlines() if line.strip()]
    effective_url = tail_lines[0] if tail_lines else url
    status = int(tail_lines[1]) if len(tail_lines) > 1 and tail_lines[1].isdigit() else 0
    if status >= 400:
        return None

    blocks = _split_header_blocks(raw)
    last = blocks[-1] if blocks else []
    size = None
    content_range = _header(last, "content-range")
    if content_range:
        match = _CONTENT_RANGE.search(content_range)
        if match:
            size = int(match.group(1))

    return RemoteInfo(
        url=url,
        effective_url=effective_url,
        filename=_filename_from(last, effective_url),
        size=size,
        content_type=(_header(last, "content-type") or "").split(";")[0].strip(),
        resumable=status == 206,
        status=status,
    )


def cached_path(info: RemoteInfo) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / info.filename


def download(
    info: RemoteInfo,
    dest: Path | None = None,
    on_progress=None,
    cancel: threading.Event | None = None,
) -> Path:
    """curl ile indir. İlerleme hedef dosyanın boyutundan okunur.

    Dosya zaten tam boyuttaysa indirme atlanır (yeniden yazarken tekrar inmesin).
    """
    dest = dest or cached_path(info)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if info.size and dest.exists() and dest.stat().st_size == info.size:
        if on_progress:
            on_progress(info.size, info.size)
        return dest

    args = [
        "curl", "-fL", "-A", USER_AGENT, "--retry", "3", "--retry-delay", "2",
        "-o", str(dest), "--", info.url,
    ]
    if info.resumable:
        args.insert(2, "-C")
        args.insert(3, "-")

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RemoteError("İndirme iptal edildi.")
            if on_progress:
                try:
                    on_progress(dest.stat().st_size, info.size)
                except FileNotFoundError:
                    on_progress(0, info.size)
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            proc.kill()

    if proc.returncode != 0:
        stderr = (proc.stderr.read() if proc.stderr else "").strip()
        raise RemoteError(stderr or f"curl {proc.returncode} ile çıktı: {shlex.join(args)}")

    if on_progress:
        on_progress(dest.stat().st_size, info.size)
    return dest
