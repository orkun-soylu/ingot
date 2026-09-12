"""Raspberry Pi OS first-boot configuration (bootfs/custom.toml).

The format applies to Raspberry Pi OS bookworm and later. Static addressing
and multiple wireless networks cannot be expressed in it, so they are not
offered here either.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import _

HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SSH_KEY_RE = re.compile(r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-\S+|sk-\S+)\s+\S+")
LOCALE_COUNTRY_RE = re.compile(r"^[a-z]{2,3}_([A-Z]{2})")

CONFIG_NAME = "custom.toml"


class ConfigError(ValueError):
    pass


def hash_password(plain: str) -> str:
    """Produce a SHA-512 crypt hash.

    Python 3.13 removed the `crypt` module, so openssl is used. The password
    goes to openssl on stdin, never in argv, where `ps` would show it.
    """
    if not plain:
        raise ConfigError(_("The password cannot be empty."))
    proc = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"],
        input=plain,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ConfigError(_("openssl passwd failed: {error}").format(error=proc.stderr.strip()))
    digest = proc.stdout.strip()
    if not digest.startswith("$6$"):
        raise ConfigError(_("openssl did not produce the expected SHA-512 hash."))
    return digest


def list_public_keys(ssh_dir: Path | None = None) -> list[tuple[Path, str]]:
    """Valid public keys in ~/.ssh as (path, key text)."""
    ssh_dir = ssh_dir or (Path.home() / ".ssh")
    found: list[tuple[Path, str]] = []
    if not ssh_dir.is_dir():
        return found
    for path in sorted(ssh_dir.glob("*.pub")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if SSH_KEY_RE.match(text):
            found.append((path, text))
    return found


# The Pi's locale defaults come from the machine doing the writing: the person
# preparing the card is usually in the same place the Pi will run.

def system_timezone(
    timezone_file: Path = Path("/etc/timezone"),
    localtime: Path = Path("/etc/localtime"),
) -> str:
    try:
        value = timezone_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    target = os.path.realpath(localtime)
    if "/zoneinfo/" in target:
        return target.split("/zoneinfo/", 1)[1]
    return "Etc/UTC"


def system_keymap(keyboard_file: Path = Path("/etc/default/keyboard")) -> str:
    try:
        for line in keyboard_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("XKBLAYOUT="):
                value = line.split("=", 1)[1].strip().strip("\"'")
                if value:
                    return value.split(",")[0]
    except OSError:
        pass
    return "us"


def system_country(environ: dict[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
        match = LOCALE_COUNTRY_RE.match(environ.get(variable, ""))
        if match:
            return match.group(1)
    return ""


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


@dataclass
class PiConfig:
    hostname: str = ""
    username: str = ""
    password_hash: str = ""
    ssh_enabled: bool = True
    ssh_password_auth: bool = True
    authorized_keys: list[str] = field(default_factory=list)
    keymap: str = "us"
    timezone: str = "Etc/UTC"
    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_country: str = ""
    wifi_hidden: bool = False

    def validate(self) -> None:
        if not self.hostname:
            raise ConfigError(_("A hostname is required."))
        if not HOSTNAME_RE.match(self.hostname):
            raise ConfigError(
                _(
                    "The hostname may contain only letters, digits and hyphens, "
                    "and cannot start or end with a hyphen."
                )
            )
        if not self.username:
            raise ConfigError(_("A username is required."))
        if not USERNAME_RE.match(self.username):
            raise ConfigError(
                _(
                    "The username must start with a lowercase letter or an underscore "
                    "and contain only lowercase letters, digits, underscores and hyphens."
                )
            )
        if not self.password_hash.startswith("$"):
            raise ConfigError(_("No password has been set."))
        for key in self.authorized_keys:
            if not SSH_KEY_RE.match(key.strip()):
                raise ConfigError(
                    _("Invalid SSH public key: {key}").format(key=key[:40] + "…")
                )
        if self.wifi_ssid and not self.wifi_country:
            raise ConfigError(_("A country code is required for Wi-Fi (for example GB)."))
        if not self.ssh_password_auth and not self.authorized_keys:
            raise ConfigError(
                _(
                    "Password login over SSH is off and no public key is set "
                    "— you would be locked out."
                )
            )

    def to_toml(self) -> str:
        self.validate()
        lines = [
            "# generated by ingot",
            "config_version = 1",
            "",
            "[system]",
            f"hostname = {_toml_string(self.hostname)}",
            "",
            "[user]",
            f"name = {_toml_string(self.username)}",
            f"password = {_toml_string(self.password_hash)}",
            "password_encrypted = true",
            "",
            "[ssh]",
            f"enabled = {str(self.ssh_enabled).lower()}",
            f"password_authentication = {str(self.ssh_password_auth).lower()}",
        ]
        if self.authorized_keys:
            lines.append("authorized_keys = [")
            for key in self.authorized_keys:
                lines.append(f"    {_toml_string(key.strip())},")
            lines.append("]")
        if self.wifi_ssid:
            lines += [
                "",
                "[wlan]",
                f"ssid = {_toml_string(self.wifi_ssid)}",
                f"password = {_toml_string(self.wifi_password)}",
                "password_encrypted = false",
                f"hidden = {str(self.wifi_hidden).lower()}",
                f"country = {_toml_string(self.wifi_country.upper())}",
            ]
        lines += [
            "",
            "[locale]",
            f"keymap = {_toml_string(self.keymap)}",
            f"timezone = {_toml_string(self.timezone)}",
            "",
        ]
        return "\n".join(lines)
