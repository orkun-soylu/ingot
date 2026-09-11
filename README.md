# kalıp

Linux için imaj yazıcı. Linux ISO'larını USB'ye, Raspberry Pi OS'i SD/NVMe/SSD'ye yazar;
Pi imajı algılarsa ilk açılış ayarlarını (`bootfs/custom.toml`) da tohumlar.

## Neden

`rpi-imager` 2.x Wayland'de kullanılamıyor: tüm GUI'yi root olarak yeniden başlatmaya
çalışıyor, Wayland de normal bir oturumda root GUI'sinin compositor'a bağlanmasına izin
vermiyor. Sonuç: parola sorulur, sonra sessizce ölür
([#1336](https://github.com/raspberrypi/rpi-imager/issues/1336),
[#1376](https://github.com/raspberrypi/rpi-imager/issues/1376) — hâlâ açık).
`rpi-imager` Debian deposunda yok, Flatpak sürümü Flathub'dan kaldırıldı, `etcher-cli`
ise deprecated.

kalıp bunu yapısal olarak çözer: **arayüz hiçbir zaman root olmaz.** Yalnızca ekranla
işi olmayan küçük bir helper `pkexec` ile yükselir. Parola kutusunu kullanıcının kendi
polkit ajanı gösterir, yani o hata sınıfı hiç doğmaz.

## Kurulum

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 policykit-1 \
                 util-linux curl openssl xz-utils gzip zstd unzip dosfstools
git clone https://github.com/orkun-soylu/ingot.git kalip && cd kalip
./install.sh
```

`install.sh` üç şey kurar:

| Ne | Nereye |
|---|---|
| Ayrıcalıklı helper | `/usr/local/libexec/kalip/kalip-helper` (root'a ait **kopya**) |
| polkit kuralı | `/usr/share/polkit-1/actions/me.soylu.kalip.policy` |
| Başlatıcı + menü girdisi | `/usr/local/bin/kalip`, `~/.local/share/applications/` |

> Helper repoya symlink **değildir**, bilerek. polkit o yola root yetkisi verir; symlink
> olsaydı dosyayı değiştirebilen herkes root olurdu. Helper'ı düzenlersen `./install.sh`
> tekrar çalıştır.

Kaldırmak için `./uninstall.sh`.

## Kullanım

```
kalip                      # veya menüden "kalıp"
kalip ~/indirilenler/x.iso # dosyayla aç
```

**Yerel dosya:** İmaj dosyası satırına tıkla, seç.

**URL:** Adresi yapıştır → `Sorgula`. Proxmox'un "Download from URL"ü gibi çalışır:
yönlendirmeleri takip eder, dosya adını (`Content-Disposition`, yoksa son URL'den),
boyutu ve içerik tipini gösterir, devam ettirilebilir olup olmadığını söyler. `İndir`
dosyayı `~/.cache/kalip/` altına alır — aynı imajı tekrar yazarken yeniden inmez.

Desteklenen biçimler: `.iso` `.img` `.raw` `.img.xz` `.img.gz` `.img.zst` `.zip`
(biçim uzantıdan değil sihirli baytlardan anlaşılır).

**Pi OS paneli** yalnızca imaj gerçekten Pi imajıysa açılır. Tespit MBR'den yapılır
(1. bölüm FAT + 2. bölüm Linux) ve sıkıştırmanın içinden okunur, dosya adına bakmaz.
ISO'lar isohybrid olduğu için yanlış pozitif vermez.

## Güvenlik

Yanlış cihaza yazmak tek gerçek risk. Kapılar:

- **Sistem diski hiç listelenmez.** `/`, `/boot`, `/boot/firmware`, `/home`, `/usr`,
  `/var`, `/nix` ya da swap barındıran disk üretilmez — filtrelenmez, hiç oluşturulmaz.
- **Dahili diskler varsayılan olarak gizli.** Açtığında onay kutusu aygıt adını
  (`nvme0n1`) elle yazmanı ister.
- **Helper arayüze güvenmez.** Aynı kontrolleri root tarafında `lsblk` ile yeniden yapar.
  Asıl kapı orasıdır; arayüzdeki filtreler yalnızca kullanıcı deneyimidir.
- **`O_EXCL`** ile açılır: cihaz kullanımdaysa çekirdek yazmayı reddeder.
- Kapasite aşımı yazma **başlamadan** yakalanır.
- Parola `openssl passwd -6`'ya **stdin'den** verilir — argv'de olsaydı `ps` ile okunurdu.
  (Python 3.13'te `crypt` modülü kaldırıldığı için openssl kullanılıyor.)

**Doğrulama** açıkken yazılan bayt sayısı kadar geri okunup SHA-256 karşılaştırılır.
Öncesinde `posix_fadvise(DONTNEED)` çağrılır; yoksa diski değil sayfa önbelleğini
doğrulamış olurdun.

**İptal** stdin üzerinden yapılır (`CANCEL`), sinyalle değil: helper root, arayüz değil,
sinyal gönderemez. Arayüz çökerse boru kapanır ve helper yazmayı durdurur.

## Sınırlar

- `custom.toml` **Pi OS bookworm ve sonrası**. Daha eskisi için `ssh` + `userconf.txt`
  gerekir; kalıp bunu yapmaz.
- `custom.toml` **statik IP ve çoklu WiFi desteklemiyor** — biçimin kendi sınırı.
  Statik adres için DHCP rezervasyonu kullan.
- `.gz` imajlarda açılmış boyut 4 GiB üstünde sarar (gzip biçiminin sınırı); ilerleme
  çubuğu o durumda belirsize düşer, yazma etkilenmez.
- `.bz2` uzantısı tanınır ama yazılamaz.

## Geliştirme

```bash
python3 -m unittest discover -s tests -v     # 26 test, GUI gerektirmez
PYTHONPATH=. python3 -m kalip                # kurulmadan çalıştır (helper yine pkexec ister)
```

Helper'ı arayüzsüz sürebilirsin — hata ayıklarken en hızlı yol:

```bash
sudo ./helper/kalip-helper --device /dev/sdb --source pios.img.xz \
     --compression xz --payload-size 5368709120 --verify --toml custom.toml
```

Loop cihazıyla gerçek donanıma dokunmadan test:

```bash
truncate -s 200M /tmp/hedef.raw
T=$(sudo losetup --show -fP /tmp/hedef.raw)
sudo ./helper/kalip-helper --device "$T" --source ... && sudo losetup -d "$T"
```

### Yapı

```
kalip/devices.py      lsblk -> aygıt listesi, sistem diski elemesi
kalip/source.py       biçim tespiti, açılmış boyut, akış, Pi MBR imzası
kalip/remote.py       curl ile URL sorgu + indirme
kalip/picfg.py        custom.toml üretimi ve doğrulaması
kalip/privileged.py   pkexec köprüsü, satır tabanlı olay protokolü
kalip/ui/window.py    GTK4 + libadwaita arayüz
helper/kalip-helper   root tarafı: doğrula, yaz, doğrula, tohumla
```
