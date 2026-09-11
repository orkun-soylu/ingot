"""Raspberry Pi OS ilk açılış ayarı (bootfs/custom.toml).

Biçim Pi OS bookworm ve sonrasında geçerlidir. Statik IP ve çoklu WiFi
custom.toml'un desteklemediği şeylerdir; buraya da konmadı.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SSH_KEY_RE = re.compile(r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-\S+|sk-\S+)\s+\S+")

CONFIG_NAME = "custom.toml"


class ConfigError(ValueError):
    pass


def hash_password(plain: str) -> str:
    """SHA-512 crypt üret.

    Python 3.13'te `crypt` modülü kaldırıldı; openssl kullanılıyor.
    Parola argv'ye değil stdin'e verilir -- aksi halde `ps` ile okunur.
    """
    if not plain:
        raise ConfigError("Parola boş olamaz.")
    proc = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"],
        input=plain,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ConfigError(f"openssl passwd başarısız: {proc.stderr.strip()}")
    digest = proc.stdout.strip()
    if not digest.startswith("$6$"):
        raise ConfigError("openssl beklenen SHA-512 hash'ini üretmedi.")
    return digest


def list_public_keys(ssh_dir: Path | None = None) -> list[tuple[Path, str]]:
    """~/.ssh içindeki geçerli açık anahtarlar: (yol, anahtar metni)."""
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
    keymap: str = "tr"
    timezone: str = "Europe/Istanbul"
    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_country: str = "TR"
    wifi_hidden: bool = False

    def validate(self) -> None:
        if not self.hostname:
            raise ConfigError("Hostname gerekli.")
        if not HOSTNAME_RE.match(self.hostname):
            raise ConfigError(
                "Hostname yalnızca harf, rakam ve tire içerebilir; tire ile başlayıp bitemez."
            )
        if not self.username:
            raise ConfigError("Kullanıcı adı gerekli.")
        if not USERNAME_RE.match(self.username):
            raise ConfigError(
                "Kullanıcı adı küçük harfle veya _ ile başlamalı; küçük harf, rakam, _ ve - içerebilir."
            )
        if not self.password_hash.startswith("$"):
            raise ConfigError("Parola ayarlanmamış.")
        for key in self.authorized_keys:
            if not SSH_KEY_RE.match(key.strip()):
                raise ConfigError(f"Geçersiz SSH açık anahtarı: {key[:40]}…")
        if self.wifi_ssid and not self.wifi_country:
            raise ConfigError("WiFi için ülke kodu gerekli (ör. TR).")
        if not self.ssh_password_auth and not self.authorized_keys:
            raise ConfigError(
                "Parola ile SSH kapalı ve hiç açık anahtar yok — makineye giremezsin."
            )

    def to_toml(self) -> str:
        self.validate()
        lines = [
            "# kalip tarafından üretildi",
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
