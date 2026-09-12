"""Block device discovery.

The only source is `lsblk -J`. Disks holding a system filesystem (root, /boot,
/home, swap and so on) are never produced -- not hidden, never built. This
check serves the interface; the real gate is inside the helper
(see helper/ingot-helper).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from .i18n import _

COLUMNS = "NAME,PATH,SIZE,MODEL,VENDOR,TRAN,RM,HOTPLUG,TYPE,MOUNTPOINTS,RO"

# A disk with any of these mount points under it is untouchable.
PROTECTED_MOUNTS = frozenset(
    {"/", "/boot", "/boot/efi", "/boot/firmware", "/home", "/usr", "/var", "/nix", "[SWAP]"}
)

# Virtual devices that are never offered as a target.
VIRTUAL_PREFIXES = ("loop", "zram", "ram", "md", "dm-", "sr", "fd")


@dataclass(frozen=True)
class Disk:
    path: str
    name: str
    size: int
    model: str
    tran: str
    removable: bool
    readonly: bool
    system: bool
    mountpoints: tuple[str, ...] = field(default=())

    @property
    def size_human(self) -> str:
        return human_bytes(self.size)

    @property
    def title(self) -> str:
        model = self.model or _("Unknown device")
        return f"{model} — {self.size_human}"

    @property
    def subtitle(self) -> str:
        bits = [self.path]
        if self.tran:
            bits.append(self.tran.upper())
        bits.append(_("removable") if self.removable else _("internal"))
        if self.mountpoints:
            bits.append(_("mounted: {mounts}").format(mounts=", ".join(self.mountpoints)))
        if self.readonly:
            bits.append(_("read-only"))
        return " · ".join(bits)


def human_bytes(n: int | None) -> str:
    if n is None:
        return _("unknown")
    step = 1024.0
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < step or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TiB"


def _collect_mountpoints(node: dict) -> list[str]:
    found = [m for m in (node.get("mountpoints") or []) if m]
    for child in node.get("children") or []:
        found.extend(_collect_mountpoints(child))
    return found


def _run_lsblk() -> list[dict]:
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", COLUMNS],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out).get("blockdevices", [])


def list_disks(include_internal: bool = False, tree: list[dict] | None = None) -> list[Disk]:
    """Candidate target disks. System disks are excluded unconditionally.

    `tree` exists for tests; without it lsblk is run.
    """
    disks: list[Disk] = []
    for node in (_run_lsblk() if tree is None else tree):
        if node.get("type") != "disk":
            continue
        name = node.get("name", "")
        if name.startswith(VIRTUAL_PREFIXES):
            continue

        mounts = _collect_mountpoints(node)
        if any(m in PROTECTED_MOUNTS for m in mounts):
            continue  # never offered

        removable = bool(node.get("rm") or node.get("hotplug") or node.get("tran") == "usb")
        if not removable and not include_internal:
            continue

        model = " ".join(filter(None, [node.get("vendor"), node.get("model")])).strip()
        disks.append(
            Disk(
                path=node.get("path", f"/dev/{name}"),
                name=name,
                size=int(node.get("size") or 0),
                model=model,
                tran=node.get("tran") or "",
                removable=removable,
                readonly=bool(node.get("ro")),
                system=False,
                mountpoints=tuple(mounts),
            )
        )

    disks.sort(key=lambda d: (not d.removable, d.path))
    return disks
