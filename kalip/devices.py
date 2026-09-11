"""Blok cihaz listesi.

Tek kaynak `lsblk -J`. Sistem diskleri (kök, /boot, /home, swap barındıran)
listeden *elenir* -- gizlenmez, hiç üretilmez. Bu tarafta yapılan kontrol
kullanıcı deneyimi içindir; gerçek kapı helper'ın içindedir (bkz. helper/kalip-helper).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

COLUMNS = "NAME,PATH,SIZE,MODEL,VENDOR,TRAN,RM,HOTPLUG,TYPE,MOUNTPOINTS,RO"

# Bu bağlama noktalarından biri diskin altındaysa disk dokunulmazdır.
PROTECTED_MOUNTS = frozenset(
    {"/", "/boot", "/boot/efi", "/boot/firmware", "/home", "/usr", "/var", "/nix", "[SWAP]"}
)

# Hedef olamayacak sanal aygıtlar.
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
        return f"{self.model or 'Bilinmeyen aygıt'} — {self.size_human}"

    @property
    def subtitle(self) -> str:
        bits = [self.path]
        if self.tran:
            bits.append(self.tran.upper())
        bits.append("çıkarılabilir" if self.removable else "dahili")
        if self.mountpoints:
            bits.append("bağlı: " + ", ".join(self.mountpoints))
        if self.readonly:
            bits.append("salt-okunur")
        return " · ".join(bits)


def human_bytes(n: int | None) -> str:
    if n is None:
        return "bilinmiyor"
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
    """Yazılabilir disk adayları. Sistem diskleri her koşulda dışarıda.

    `tree` yalnızca test için: verilmezse lsblk çalıştırılır.
    """
    disks: list[Disk] = []
    for node in (_run_lsblk() if tree is None else tree):
        if node.get("type") != "disk":
            continue
        name = node.get("name", "")
        if name.startswith(VIRTUAL_PREFIXES):
            continue

        mounts = _collect_mountpoints(node)
        is_system = any(m in PROTECTED_MOUNTS for m in mounts)
        if is_system:
            continue  # asla listeleme

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
