"""pkexec köprüsü.

Arayüz hiçbir zaman root olmaz; yalnızca bu modülün başlattığı helper olur.
Parola kutusunu kullanıcının kendi polkit ajanı gösterir -- bu yüzden Wayland'de
sorun çıkmaz (rpi-imager'ın tüm GUI'yi root'a taşıyıp takıldığı yer burasıydı).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

HELPER_CANDIDATES = (
    Path("/usr/libexec/kalip/kalip-helper"),          # .deb ile kurulan
    Path(__file__).resolve().parent.parent / "helper" / "kalip-helper",  # depodan calistirirken
)

# pkexec'in iptal ettiği / yetkilendirmenin reddedildiği çıkış kodu
PKEXEC_NOT_AUTHORIZED = 126
PKEXEC_FAILED = 127


class PrivilegeError(RuntimeError):
    pass


def helper_path() -> Path:
    for candidate in HELPER_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise PrivilegeError(
        "kalip-helper bulunamadı — paket eksik kurulmuş olabilir. "
        "Yeniden kur: sudo apt install --reinstall kalip"
    )


def ensure_pkexec() -> str:
    path = shutil.which("pkexec")
    if path is None:
        raise PrivilegeError(
            "pkexec yok. Kur: sudo apt install pkexec polkitd"
        )
    return path


@dataclass
class WriteRequest:
    device: str
    source: Path
    compression: str | None
    payload_size: int = 0
    verify: bool = True
    toml_path: Path | None = None

    def helper_args(self) -> list[str]:
        args = [
            "--device", self.device,
            "--source", str(self.source),
            "--compression", self.compression or "none",
            "--payload-size", str(self.payload_size or 0),
        ]
        if self.verify:
            args.append("--verify")
        if self.toml_path:
            args += ["--toml", str(self.toml_path)]
        return args


@dataclass
class WriteOutcome:
    ok: bool
    error: str = ""
    facts: dict[str, str] = field(default_factory=dict)


class WriteJob:
    """Helper'ı ayrı bir iş parçacığında çalıştırır, olayları geri çağırır.

    on_event(kind, *payload):
        "status",   mesaj
        "progress", asama, yapilan, toplam      (toplam 0 = bilinmiyor)
        "fact",     anahtar, deger
        "finished", WriteOutcome
    """

    def __init__(self, request: WriteRequest, on_event) -> None:
        self.request = request
        self._on_event = on_event
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cancelled = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write("CANCEL\n")
                proc.stdin.flush()
                proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def _run(self) -> None:
        facts: dict[str, str] = {}
        try:
            command = [ensure_pkexec(), str(helper_path()), *self.request.helper_args()]
        except PrivilegeError as exc:
            self._on_event("finished", WriteOutcome(ok=False, error=str(exc)))
            return

        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._on_event("finished", WriteOutcome(ok=False, error=f"pkexec başlatılamadı: {exc}"))
            return

        with self._lock:
            self._proc = proc
            if self._cancelled:  # start ile cancel yarıştıysa
                try:
                    proc.stdin.write("CANCEL\n")
                    proc.stdin.flush()
                except OSError:
                    pass

        error = ""
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            kind, _, rest = line.partition(" ")
            if kind == "S":
                self._on_event("status", rest)
            elif kind == "P":
                parts = rest.split()
                if len(parts) == 3:
                    stage, done, total = parts
                    self._on_event("progress", stage, int(done), int(total))
            elif kind == "R":
                key, _, value = rest.partition("=")
                facts[key] = value
                self._on_event("fact", key, value)
            elif kind == "E":
                error = rest
            elif kind == "D":
                pass

        proc.wait()
        stderr = (proc.stderr.read() if proc.stderr else "").strip()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except OSError:
                pass

        if proc.returncode == 0 and not error:
            self._on_event("finished", WriteOutcome(ok=True, facts=facts))
            return

        if not error:
            if proc.returncode == PKEXEC_NOT_AUTHORIZED:
                error = "Yetkilendirme iptal edildi veya reddedildi."
            elif proc.returncode == PKEXEC_FAILED:
                error = stderr or "pkexec helper'ı çalıştıramadı."
            else:
                error = stderr or f"Helper {proc.returncode} ile çıktı."
        self._on_event("finished", WriteOutcome(ok=False, error=error, facts=facts))
