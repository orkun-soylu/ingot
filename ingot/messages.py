"""Translations for the helper's status and error codes.

The helper runs as root through pkexec, which does not carry the session's
language into it, so it never produces interface text. It emits stable codes
and this module turns them into translated messages in the user's process.
"""

from __future__ import annotations

from .devices import human_bytes
from .i18n import N_, _, ngettext

STATUS = {
    "validating_target": N_("Checking the target device"),
    "writing": N_("Writing the image"),
    "flushing": N_("Flushing data to the device"),
    "verifying": N_("Verifying"),
    "verified": N_("Verification passed"),
    "rereading_partitions": N_("Rereading the partition table"),
    "seeding": N_("Writing custom.toml to the boot partition"),
    "done": N_("Done"),
}

ERRORS = {
    "not_root": N_("The helper must run as root; it is started through pkexec."),
    "source_missing": N_("Cannot read the image: {path}"),
    "toml_missing": N_("Cannot read custom.toml: {path}"),
    "device_missing": N_("{device} does not exist."),
    "not_whole_device": N_("{device} is not a whole block device."),
    "virtual_device": N_("{device} is a virtual device and cannot be written to."),
    "read_only": N_("{device} is read-only."),
    "system_disk": N_("{device} is a system disk ({mounts}) — refusing to write."),
    "swap_on_target": N_("The target has active swap — refusing to write."),
    "still_mounted": N_("Some partitions are still mounted: {mounts}"),
    "image_too_large": N_("The image is larger than the device: {image} > {capacity}."),
    "capacity_exceeded": N_("The image outgrew the device's capacity; writing stopped."),
    "cancelled": N_("Cancelled — the data on the media is incomplete."),
    "zip_not_single": N_("The zip archive must contain exactly one image file."),
    "zstdcat_missing": N_("zstdcat was not found. Is the zstd package installed?"),
    "unknown_compression": N_("Unknown compression: {name}"),
    "device_ended_early": N_("The device ended sooner than expected."),
    "verify_failed": N_(
        "Verification FAILED — the data on the device does not match the image. "
        "The media may be faulty."
    ),
    "device_busy": N_("{device} is busy — another program may be using it."),
    "os_error": N_("{error} ({device})"),
    "interrupted": N_("Interrupted."),
}

SEED_REASONS = {
    "no-fat-partition": N_("no FAT partition was found"),
    "mount-failed": N_("the boot partition could not be mounted"),
    "not-pi-bootfs": N_("the boot partition does not look like Raspberry Pi OS"),
}

STAGES = {
    "write": N_("Writing"),
    "verify": N_("Verifying"),
    "download": N_("Downloading"),
}

# Parameters the helper sends as raw byte counts.
_BYTE_PARAMS = ("image", "capacity")


def describe_status(code: str, argument: str = "") -> str:
    if code == "unmounting":
        count = int(argument) if argument.isdigit() else 0
        return ngettext(
            "Unmounting {count} mounted partition",
            "Unmounting {count} mounted partitions",
            count,
        ).format(count=count)
    template = STATUS.get(code)
    return _(template) if template else code


def describe_error(code: str, params: dict | None = None) -> str:
    params = dict(params or {})
    for key in _BYTE_PARAMS:
        if isinstance(params.get(key), int):
            params[key] = human_bytes(params[key])

    template = ERRORS.get(code)
    if template is None:
        detail = ", ".join(f"{key}={value}" for key, value in params.items())
        return f"{code}: {detail}" if detail else code
    try:
        return _(template).format(**params)
    except (KeyError, IndexError, ValueError):
        return _(template)  # a translation with broken placeholders still says something


def describe_seed_reason(reason: str) -> str:
    template = SEED_REASONS.get(reason)
    return _(template) if template else reason


def stage_label(stage: str) -> str:
    template = STAGES.get(stage)
    return _(template) if template else stage
