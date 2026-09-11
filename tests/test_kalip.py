"""kalıp çekirdek testleri. GUI gerektirmez: python3 -m unittest discover tests"""

from __future__ import annotations

import gzip
import lzma
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalip.devices import list_disks  # noqa: E402
from kalip.picfg import ConfigError, PiConfig, hash_password  # noqa: E402
from kalip.remote import _filename_from, _header, _split_header_blocks  # noqa: E402
from kalip.source import looks_like_pi_image, probe  # noqa: E402


def disk(name, mountpoints=(), children=(), rm=False, tran="usb", dtype="disk"):
    return {
        "name": name, "path": f"/dev/{name}", "size": 1 << 30, "type": dtype,
        "mountpoints": list(mountpoints), "rm": rm, "hotplug": rm, "tran": tran,
        "ro": False, "model": "Test", "vendor": None,
        "children": list(children),
    }


class DeviceSafety(unittest.TestCase):
    def test_kok_diski_asla_listelenmez(self):
        tree = [disk("sda", children=[disk("sda1", ["/"], dtype="part")], tran="sata")]
        self.assertEqual(list_disks(include_internal=True, tree=tree), [])

    def test_boot_bolumu_olan_disk_elenir(self):
        tree = [disk("nvme0n1", children=[
            disk("nvme0n1p1", ["/boot/firmware"], dtype="part"),
            disk("nvme0n1p2", ["/srv"], dtype="part"),
        ], tran="nvme")]
        self.assertEqual(list_disks(include_internal=True, tree=tree), [])

    def test_swap_diski_elenir(self):
        tree = [disk("sdb", children=[disk("sdb1", ["[SWAP]"], dtype="part")], tran="sata")]
        self.assertEqual(list_disks(include_internal=True, tree=tree), [])

    def test_cikarilabilir_usb_listelenir(self):
        tree = [disk("sdc", rm=True)]
        found = list_disks(tree=tree)
        self.assertEqual([d.path for d in found], ["/dev/sdc"])
        self.assertTrue(found[0].removable)

    def test_dahili_disk_varsayilan_olarak_gizli(self):
        tree = [disk("sdd", tran="sata")]
        self.assertEqual(list_disks(tree=tree), [])
        self.assertEqual([d.path for d in list_disks(include_internal=True, tree=tree)], ["/dev/sdd"])

    def test_sanal_aygitlar_atlanir(self):
        tree = [disk("zram0", ["[SWAP]"], tran=None), disk("loop0", tran=None, dtype="loop")]
        self.assertEqual(list_disks(include_internal=True, tree=tree), [])

    def test_bagli_veri_diski_listelenir_ama_isaretlenir(self):
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

    def test_toml_gecerli_ve_geri_okunabilir(self):
        parsed = tomllib.loads(self.base.to_toml())
        self.assertEqual(parsed["config_version"], 1)
        self.assertEqual(parsed["system"]["hostname"], "raspberrypi")
        self.assertTrue(parsed["user"]["password_encrypted"])

    def test_ozel_karakterler_kacirilir(self):
        self.base.wifi_ssid = 'Ev"Ağı\\Test'
        self.base.wifi_password = "p\"a's"
        parsed = tomllib.loads(self.base.to_toml())
        self.assertEqual(parsed["wlan"]["ssid"], 'Ev"Ağı\\Test')
        self.assertEqual(parsed["wlan"]["password"], "p\"a's")

    def test_ssh_anahtari_listeye_girer(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample orkun@host"
        self.base.authorized_keys = [key]
        self.assertEqual(tomllib.loads(self.base.to_toml())["ssh"]["authorized_keys"], [key])

    def test_gecersiz_hostname_reddedilir(self):
        for bad in ("-bas", "son-", "boşluk var", "a" * 64, ""):
            with self.subTest(bad=bad):
                self.base.hostname = bad
                self.assertRaises(ConfigError, self.base.validate)

    def test_gecersiz_kullanici_reddedilir(self):
        for bad in ("Orkun", "1orkun", "or kun", ""):
            with self.subTest(bad=bad):
                self.base.username = bad
                self.assertRaises(ConfigError, self.base.validate)

    def test_erisimsiz_kalma_engellenir(self):
        self.base.ssh_password_auth = False
        self.base.authorized_keys = []
        self.assertRaises(ConfigError, self.base.validate)

    def test_bozuk_ssh_anahtari_reddedilir(self):
        self.base.authorized_keys = ["bu bir anahtar degil"]
        self.assertRaises(ConfigError, self.base.validate)

    def test_parola_hash_sha512(self):
        digest = hash_password("deneme")
        self.assertTrue(digest.startswith("$6$"))
        self.assertNotIn("deneme", digest)

    def test_parola_argv_de_gorunmez(self):
        # openssl'e stdin ile veriliyor olmalı; aksi halde `ps` ile okunur.
        source = (Path(__file__).resolve().parent.parent / "kalip" / "picfg.py").read_text()
        self.assertIn("-stdin", source)
        self.assertIn("input=plain", source)


class SourceTest(unittest.TestCase):
    HAS_ZSTD = shutil.which("zstd") is not None

    @classmethod
    def setUpClass(cls):
        # Fixture'lar stdlib ile uretilir: derleme icin harici arac gerekmesin.
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
        import struct
        mbr = bytearray(512)
        def entry(ptype, lba, nsec):
            return bytes([0, 0, 0, 0, ptype, 0, 0, 0]) + struct.pack("<II", lba, nsec)
        mbr[446:462] = entry(0x0C, 8192, 131072)      # FAT32 bootfs
        mbr[462:478] = entry(0x83, 139264, 131072)    # linux rootfs
        mbr[510:512] = b"\x55\xaa"
        return bytes(mbr)

    def test_biçim_tespiti(self):
        beklenen = {
            "disk.img": None, "disk.img.xz": "xz", "disk.img.gz": "gzip",
            "disk.img.zst": "zstd", "disk.zip": "zip",
        }
        for name in self._names():
            with self.subTest(name=name):
                self.assertEqual(probe(Path(self.tmp.name) / name).compression, beklenen[name])

    def test_acilmis_boyut_dogru(self):
        for name in self._names():
            with self.subTest(name=name):
                self.assertEqual(probe(Path(self.tmp.name) / name).payload_size, 2 * 1024 * 1024)

    def test_pi_imzasi_sikistirmanin_icinden_okunur(self):
        for name in self._names():
            with self.subTest(name=name):
                self.assertTrue(probe(Path(self.tmp.name) / name).is_pi_image)

    def test_isohybrid_yanlis_pozitif_vermez(self):
        import struct
        mbr = bytearray(512)
        mbr[446:462] = bytes([0x80, 0, 0, 0, 0x00, 0, 0, 0]) + struct.pack("<II", 0, 2880000)
        mbr[510:512] = b"\x55\xaa"
        iso = Path(self.tmp.name) / "linux.iso"
        iso.write_bytes(bytes(mbr) + b"\0" * 4096)
        self.assertFalse(looks_like_pi_image(iso, None))

    def test_bos_dosya_cokmez(self):
        empty = Path(self.tmp.name) / "bos.img"
        empty.write_bytes(b"")
        self.assertFalse(looks_like_pi_image(empty, None))


class RemoteParsingTest(unittest.TestCase):
    RAW = (
        "HTTP/2 302\r\nlocation: https://x/final.img.xz\r\ncontent-type: text/html\r\n"
        "\r\n"
        "HTTP/2 200\r\naccept-ranges: bytes\r\ncontent-length: 524875608\r\n"
        "content-type: application/x-xz\r\n"
    )

    def test_son_blok_alinir(self):
        blocks = _split_header_blocks(self.RAW)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(_header(blocks[-1], "content-length"), "524875608")
        self.assertEqual(_header(blocks[-1], "Content-Type"), "application/x-xz")

    def test_dosya_adi_yonlendirme_sonrasi_urlden(self):
        blocks = _split_header_blocks(self.RAW)
        name = _filename_from(blocks[-1], "https://dl.example/img/2026-raspios.img.xz")
        self.assertEqual(name, "2026-raspios.img.xz")

    def test_content_disposition_oncelikli(self):
        block = ['content-disposition: attachment; filename="ubuntu-26.04.iso"']
        self.assertEqual(_filename_from(block, "https://x/download?id=7"), "ubuntu-26.04.iso")

    def test_dizin_yolu_bos_ada_dusmez(self):
        self.assertEqual(_filename_from([], "https://example.com/"), "image.bin")

    def test_disposition_yol_gezinmesi_soyulur(self):
        block = ['content-disposition: attachment; filename="../../etc/shadow"']
        self.assertEqual(_filename_from(block, "https://x/a"), "shadow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
