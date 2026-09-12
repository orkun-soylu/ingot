"""pkexec bridge.

The interface never becomes root; only the helper this module starts does.
The password prompt comes from the user's own polkit agent, which is why this
works on Wayland (rpi-imager moves its whole interface to root and fails
exactly there).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import _
from .messages import describe_error, describe_status

HELPER_CANDIDATES = (
    Path("/usr/libexec/ingot/ingot-helper"),  # installed by the package
    Path(__file__).resolve().parent.parent / "helper" / "ingot-helper",  # source checkout
)

# pkexec exit codes: authorization dismissed or denied / could not run the program
PKEXEC_NOT_AUTHORIZED = 126
PKEXEC_FAILED = 127


class PrivilegeError(RuntimeError):
    pass


def helper_path() -> Path:
    for candidate in HELPER_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise PrivilegeError(
        _(
            "The privileged helper was not found — the package may be incompletely "
            "installed. Reinstall it with: sudo apt install --reinstall ingot"
        )
    )


def ensure_pkexec() -> str:
    path = shutil.which("pkexec")
    if path is None:
        raise PrivilegeError(
            _("pkexec is not installed. Install it with: sudo apt install pkexec polkitd")
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
    """Run the helper on a worker thread and report events back.

    on_event(kind, *payload):
        "status",   translated message
        "progress", stage, done, total      (total 0 = unknown)
        "fact",     key, value
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
            self._on_event(
                "finished",
                WriteOutcome(ok=False, error=_("Could not start pkexec: {error}").format(error=exc)),
            )
            return

        with self._lock:
            self._proc = proc
            if self._cancelled:  # cancel raced with start
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
            kind, _sep, rest = line.partition(" ")
            if kind == "S":
                code, _sep, argument = rest.partition(" ")
                self._on_event("status", describe_status(code, argument))
            elif kind == "P":
                parts = rest.split()
                if len(parts) == 3:
                    stage, done, total = parts
                    self._on_event("progress", stage, int(done), int(total))
            elif kind == "R":
                key, _sep, value = rest.partition("=")
                facts[key] = value
                self._on_event("fact", key, value)
            elif kind == "E":
                code, _sep, payload = rest.partition(" ")
                try:
                    params = json.loads(payload) if payload else {}
                except ValueError:
                    params = {}
                error = describe_error(code, params)

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
                error = _("Authorization was cancelled or denied.")
            elif proc.returncode == PKEXEC_FAILED:
                error = stderr or _("pkexec could not run the helper.")
            else:
                error = stderr or _("The helper exited with status {code}.").format(
                    code=proc.returncode
                )
        self._on_event("finished", WriteOutcome(ok=False, error=error, facts=facts))
