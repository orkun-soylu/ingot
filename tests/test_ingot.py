"""Ingot core tests. No GUI needed: python3 -m unittest discover -s tests"""

from __future__ import annotations

import ast
import gzip
import lzma
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingot import messages, remote  # noqa: E402
from ingot.devices import list_disks  # noqa: E402
from ingot.picfg import (  # noqa: E402
    ConfigError,
    PiConfig,
    hash_password,
    system_country,
    system_keymap,
    system_timezone,
)
from ingot.remote import _filename_from, _header, _split_header_blocks  # noqa: E402
from ingot.source import looks_like_pi_image, probe  # noqa: E402

HELPER = ROOT / "helper" / "ingot-helper"
PO_DIR = ROOT / "po"
PLACEHOLDER = re.compile(r"\{(\w*)\}")


def disk(name, mountpoints=(), children=(), rm=False, tran="usb", dtype="disk"):
    return {
        "name": name, "path": f"/dev/{name}", "size": 1 << 30, "type": dtype,
        "mountpoints": list(mountpoints), "rm": rm, "hotplug": rm, "tran": tran,
        "ro": False, "model": "Test", "vendor": None,
        "children": list(children),
    }


class DeviceSafetyTest(unittest.TestCase):
    def test_root_disk_is_never_listed(self):
        tree = [disk("sda", children=[disk("sda1", ["/"], dtype="part")], tran="sata")]
        self.assertEqual(list_disks(include_internal=True, tree=tree), [])

    def test_disk_with_boot_partition_is_excluded(self):
        tree = [disk("nvme0n1", children=[
            disk("nvme0n1p1", ["/boot/firmware"], dtype="part"),
            disk("nvme0n1p2", ["/srv"], dtype="part"),
        ], tran="nvme")]
        self.assertEqual(list_disks(include_internal=True, tree=tree), [])

    def test_swap_disk_is_excluded(self):
        tree = [disk("sdb", children=[disk("sdb1", ["[SWAP]"], dtype="part")], tran="sata")]
        self.assertEqual(list_disks(include_internal=True, tree=tree), [])

    def test_removable_usb_is_listed(self):
        found = list_disks(tree=[disk("sdc", rm=True)])
        self.assertEqual([d.path for d in found], ["/dev/sdc"])
        self.assertTrue(found[0].removable)

    def test_internal_disk_hidden_by_default(self):
        tree = [disk("sdd", tran="sata")]
        self.assertEqual(list_disks(tree=tree), [])
        self.assertEqual([d.path for d in list_disks(include_internal=True, tree=tree)], ["/dev/sdd"])

    def test_virtual_devices_are_skipped(self):
        tree = [disk("zram0", ["[SWAP]"], tran=None), disk("loop0", tran=None, dtype="loop")]
        self.assertEqual(list_disks(include_internal=True, tree=tree), [])

    def test_mounted_data_disk_is_listed_and_marked(self):
        tree = [disk("sde", children=[disk("sde1", ["/media/orkun/usb"], dtype="part")], rm=True)]
        found = list_disks(tree=tree)
        self.assertEqual(len(found), 1)
        self.assertIn("/media/orkun/usb", found[0].mountpoints)


class PiConfigTest(unittest.TestCase):
    def setUp(self):
        self.base = PiConfig(
            hostname="raspberrypi", username="orkun",
            password_hash="$6$abc$def", authorized_keys=[],
        )

    def test_toml_is_valid_and_round_trips(self):
        parsed = tomllib.loads(self.base.to_toml())
        self.assertEqual(parsed["config_version"], 1)
        self.assertEqual(parsed["system"]["hostname"], "raspberrypi")
        self.assertTrue(parsed["user"]["password_encrypted"])

    def test_special_characters_are_escaped(self):
        self.base.wifi_ssid = 'Home"Net\\Test'
        self.base.wifi_password = "p\"a's"
        self.base.wifi_country = "gb"
        parsed = tomllib.loads(self.base.to_toml())
        self.assertEqual(parsed["wlan"]["ssid"], 'Home"Net\\Test')
        self.assertEqual(parsed["wlan"]["password"], "p\"a's")
        self.assertEqual(parsed["wlan"]["country"], "GB")

    def test_ssh_key_is_listed(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample orkun@host"
        self.base.authorized_keys = [key]
        self.assertEqual(tomllib.loads(self.base.to_toml())["ssh"]["authorized_keys"], [key])

    def test_invalid_hostname_is_rejected(self):
        for bad in ("-start", "end-", "has space", "a" * 64, ""):
            with self.subTest(bad=bad):
                self.base.hostname = bad
                self.assertRaises(ConfigError, self.base.validate)

    def test_invalid_username_is_rejected(self):
        for bad in ("Orkun", "1orkun", "or kun", ""):
            with self.subTest(bad=bad):
                self.base.username = bad
                self.assertRaises(ConfigError, self.base.validate)

    def test_wifi_needs_a_country(self):
        self.base.wifi_ssid = "HomeNet"
        self.base.wifi_country = ""
        self.assertRaises(ConfigError, self.base.validate)

    def test_lockout_is_prevented(self):
        self.base.ssh_password_auth = False
        self.base.authorized_keys = []
        self.assertRaises(ConfigError, self.base.validate)

    def test_malformed_ssh_key_is_rejected(self):
        self.base.authorized_keys = ["this is not a key"]
        self.assertRaises(ConfigError, self.base.validate)

    def test_password_hash_is_sha512(self):
        digest = hash_password("example")
        self.assertTrue(digest.startswith("$6$"))
        self.assertNotIn("example", digest)

    def test_password_never_goes_to_argv(self):
        # It must reach openssl on stdin; in argv `ps` would show it.
        source = (ROOT / "ingot" / "picfg.py").read_text()
        self.assertIn("-stdin", source)
        self.assertIn("input=plain", source)


class SystemDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_timezone_from_etc_timezone(self):
        tz = self.root / "timezone"
        tz.write_text("Europe/Istanbul\n")
        self.assertEqual(system_timezone(tz, self.root / "none"), "Europe/Istanbul")

    def test_timezone_from_localtime_symlink(self):
        zone = self.root / "usr/share/zoneinfo/Europe/London"
        zone.parent.mkdir(parents=True)
        zone.write_bytes(b"TZif")
        link = self.root / "localtime"
        link.symlink_to(zone)
        self.assertEqual(system_timezone(self.root / "missing", link), "Europe/London")

    def test_timezone_fallback(self):
        self.assertEqual(system_timezone(self.root / "a", self.root / "b"), "Etc/UTC")

    def test_keymap_takes_first_layout(self):
        keyboard = self.root / "keyboard"
        keyboard.write_text('XKBMODEL="pc105"\nXKBLAYOUT="tr,us"\n')
        self.assertEqual(system_keymap(keyboard), "tr")

    def test_keymap_fallback(self):
        self.assertEqual(system_keymap(self.root / "missing"), "us")

    def test_country_from_locale(self):
        self.assertEqual(system_country({"LANG": "tr_TR.UTF-8"}), "TR")
        self.assertEqual(system_country({"LC_ALL": "C.UTF-8", "LANG": "en_GB.UTF-8"}), "GB")
        self.assertEqual(system_country({"LANG": "C"}), "")


class SourceTest(unittest.TestCase):
    HAS_ZSTD = shutil.which("zstd") is not None

    @classmethod
    def setUpClass(cls):
        # Fixtures are built with the standard library so the package build
        # needs no extra tools.
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        payload = cls._pi_mbr() + b"\0" * (2 * 1024 * 1024 - 512)
        cls.raw = root / "disk.img"
        cls.raw.write_bytes(payload)

        with lzma.open(root / "disk.img.xz", "wb") as fh:
            fh.write(payload)
        with gzip.GzipFile(root / "disk.img.gz", "wb", mtime=0) as fh:
            fh.write(payload)
        with zipfile.ZipFile(root / "disk.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("disk.img", payload)
        if cls.HAS_ZSTD:
            subprocess.run(["zstd", "-q", "-k", str(cls.raw)], check=True, cwd=root)

    @classmethod
    def _names(cls) -> list[str]:
        names = ["disk.img", "disk.img.xz", "disk.img.gz", "disk.zip"]
        if cls.HAS_ZSTD:
            names.append("disk.img.zst")
        return names

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def _pi_mbr() -> bytes:
        mbr = bytearray(512)

        def entry(ptype, lba, nsec):
            return bytes([0, 0, 0, 0, ptype, 0, 0, 0]) + struct.pack("<II", lba, nsec)

        mbr[446:462] = entry(0x0C, 8192, 131072)      # FAT32 bootfs
        mbr[462:478] = entry(0x83, 139264, 131072)    # Linux rootfs
        mbr[510:512] = b"\x55\xaa"
        return bytes(mbr)

    def test_format_detection(self):
        expected = {
            "disk.img": None, "disk.img.xz": "xz", "disk.img.gz": "gzip",
            "disk.img.zst": "zstd", "disk.zip": "zip",
        }
        for name in self._names():
            with self.subTest(name=name):
                self.assertEqual(probe(Path(self.tmp.name) / name).compression, expected[name])

    def test_decompressed_size(self):
        for name in self._names():
            with self.subTest(name=name):
                self.assertEqual(probe(Path(self.tmp.name) / name).payload_size, 2 * 1024 * 1024)

    def test_pi_signature_read_through_compression(self):
        for name in self._names():
            with self.subTest(name=name):
                self.assertTrue(probe(Path(self.tmp.name) / name).is_pi_image)

    def test_isohybrid_is_not_a_false_positive(self):
        mbr = bytearray(512)
        mbr[446:462] = bytes([0x80, 0, 0, 0, 0x00, 0, 0, 0]) + struct.pack("<II", 0, 2880000)
        mbr[510:512] = b"\x55\xaa"
        iso = Path(self.tmp.name) / "linux.iso"
        iso.write_bytes(bytes(mbr) + b"\0" * 4096)
        self.assertFalse(looks_like_pi_image(iso, None))

    def test_empty_file_does_not_crash(self):
        empty = Path(self.tmp.name) / "empty.img"
        empty.write_bytes(b"")
        self.assertFalse(looks_like_pi_image(empty, None))


class RemoteTest(unittest.TestCase):
    RAW = (
        "HTTP/2 302\r\nlocation: https://x/final.img.xz\r\ncontent-type: text/html\r\n"
        "\r\n"
        "HTTP/2 200\r\naccept-ranges: bytes\r\ncontent-length: 524875608\r\n"
        "content-type: application/x-xz\r\n"
    )

    def test_last_header_block_wins(self):
        blocks = _split_header_blocks(self.RAW)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(_header(blocks[-1], "content-length"), "524875608")
        self.assertEqual(_header(blocks[-1], "Content-Type"), "application/x-xz")

    def test_filename_from_url_after_redirect(self):
        blocks = _split_header_blocks(self.RAW)
        name = _filename_from(blocks[-1], "https://dl.example/img/2026-raspios.img.xz")
        self.assertEqual(name, "2026-raspios.img.xz")

    def test_content_disposition_takes_precedence(self):
        block = ['content-disposition: attachment; filename="ubuntu-26.04.iso"']
        self.assertEqual(_filename_from(block, "https://x/download?id=7"), "ubuntu-26.04.iso")

    def test_directory_url_gets_a_fallback_name(self):
        self.assertEqual(_filename_from([], "https://example.com/"), "image.bin")

    def test_path_traversal_in_disposition_is_stripped(self):
        block = ['content-disposition: attachment; filename="../../etc/shadow"']
        self.assertEqual(_filename_from(block, "https://x/a"), "shadow")

    def test_cache_from_before_the_rename_is_carried_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "kalip"
            legacy.mkdir()
            (legacy / "pi.img.xz").write_bytes(b"x")
            new = Path(tmp) / "ingot"
            info = remote.RemoteInfo(
                url="u", effective_url="u", filename="pi.img.xz", size=1,
                content_type="", resumable=False, status=200,
            )
            with mock.patch.object(remote, "CACHE_DIR", new), \
                    mock.patch.object(remote, "LEGACY_CACHE_DIR", legacy):
                path = remote.cached_path(info)
            self.assertEqual(path, new / "pi.img.xz")
            self.assertTrue(path.exists())
            self.assertFalse(legacy.exists())


class HelperContractTest(unittest.TestCase):
    """The helper emits codes; the interface has to know every one of them."""

    @classmethod
    def setUpClass(cls):
        cls.source = HELPER.read_text(encoding="utf-8")

    def test_every_error_code_is_translated(self):
        codes = set(re.findall(r'HelperError\(\s*"([a-z_]+)"', self.source))
        codes |= set(re.findall(r'report_error\(\s*"([a-z_]+)"', self.source))
        self.assertTrue(codes)
        self.assertEqual(sorted(codes - messages.ERRORS.keys()), [])

    def test_every_status_code_is_translated(self):
        codes = set(re.findall(r'\bstatus\(\s*"([a-z_]+)"', self.source))
        self.assertTrue(codes)
        self.assertEqual(sorted(codes - (messages.STATUS.keys() | {"unmounting"})), [])

    def test_every_seed_reason_is_translated(self):
        reasons = set(re.findall(r'"no:([a-z-]+)"', self.source))
        self.assertTrue(reasons)
        self.assertEqual(sorted(reasons - messages.SEED_REASONS.keys()), [])

    def test_error_templates_accept_the_helper_parameters(self):
        samples = {
            "system_disk": {"device": "/dev/sda", "mounts": "/, /boot"},
            "image_too_large": {"image": 8 << 30, "capacity": 4 << 30},
            "os_error": {"error": "Input/output error", "device": "/dev/sdb"},
            "unknown_compression": {"name": "bz2"},
        }
        for code, params in samples.items():
            with self.subTest(code=code):
                self.assertNotIn("{", messages.describe_error(code, params))
        self.assertIn("GiB", messages.describe_error("image_too_large", samples["image_too_large"]))

    def test_unknown_code_does_not_crash(self):
        self.assertIn("mystery", messages.describe_error("mystery", {"a": 1}))

    def test_unmounting_count_is_formatted(self):
        self.assertIn("2", messages.describe_status("unmounting", "2"))


def translatable_strings() -> dict[str, str | None]:
    """msgid -> msgid_plural (or None) from every _(), N_() and ngettext() call."""
    found: dict[str, str | None] = {}
    for path in sorted((ROOT / "ingot").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            literals = [
                a.value for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            if node.func.id in ("_", "N_") and literals:
                found.setdefault(literals[0], None)
            elif node.func.id == "ngettext" and len(literals) >= 2:
                found[literals[0]] = literals[1]
    return found


def parse_po(path: Path) -> list[dict]:
    entries: list[dict] = []
    entry: dict = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines() + [""]:
        line = raw.strip()
        if not line:
            if entry:
                entries.append(entry)
            entry, current = {}, None
        elif line.startswith("#,"):
            entry.setdefault("flags", set()).update(f.strip() for f in line[2:].split(","))
        elif line.startswith("#"):
            continue
        elif line.startswith('"'):
            entry[current] += ast.literal_eval(line)
        else:
            current, _sep, value = line.partition(" ")
            entry[current] = ast.literal_eval(value)
    return [e for e in entries if e.get("msgid")]


class CatalogueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.languages = (PO_DIR / "LINGUAS").read_text().split()
        cls.wanted = translatable_strings()

    def test_turkish_is_shipped(self):
        self.assertIn("tr", self.languages)

    def test_every_string_is_translated(self):
        for lang in self.languages:
            entries = {e["msgid"]: e for e in parse_po(PO_DIR / f"{lang}.po")}
            with self.subTest(lang=lang):
                self.assertEqual(sorted(m for m in self.wanted if m not in entries), [])
                for msgid, entry in entries.items():
                    self.assertNotIn("fuzzy", entry.get("flags", set()), msgid)
                    translations = [v for k, v in entry.items() if k.startswith("msgstr")]
                    self.assertTrue(translations and all(translations), msgid)

    def test_plural_entries_match_the_source(self):
        for lang in self.languages:
            entries = {e["msgid"]: e for e in parse_po(PO_DIR / f"{lang}.po")}
            for msgid, plural in self.wanted.items():
                if plural is not None:
                    with self.subTest(lang=lang, msgid=msgid):
                        self.assertEqual(entries[msgid].get("msgid_plural"), plural)

    def test_placeholders_survive_translation(self):
        for lang in self.languages:
            for entry in parse_po(PO_DIR / f"{lang}.po"):
                expected = set(PLACEHOLDER.findall(entry["msgid"]))
                for key, value in entry.items():
                    if key.startswith("msgstr"):
                        with self.subTest(lang=lang, msgid=entry["msgid"], key=key):
                            self.assertEqual(set(PLACEHOLDER.findall(value)), expected)

    def test_no_stale_entries(self):
        for lang in self.languages:
            stale = sorted(
                e["msgid"] for e in parse_po(PO_DIR / f"{lang}.po") if e["msgid"] not in self.wanted
            )
            with self.subTest(lang=lang):
                self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
