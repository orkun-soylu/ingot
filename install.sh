#!/usr/bin/env bash
# kalıp kurulumu.
#
# Ayrıcalıklı helper /usr/local/libexec altına root'a ait bir KOPYA olarak
# kurulur, repoya symlink DEĞİL: polkit o yola yetki verir ve dosyayı
# değiştirebilen herkes root olurdu. Helper'ı her değiştirdiğinde bu script'i
# yeniden çalıştır.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBEXEC=/usr/local/libexec/kalip
BINDIR=/usr/local/bin
POLKIT_DIR=/usr/share/polkit-1/actions
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mhata:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "Bunu root olarak çalıştırma; gerektiğinde sudo kendisi sorar."

say "Bağımlılıklar denetleniyor"
missing=()
for cmd in python3 lsblk curl openssl pkexec udevadm; do
  command -v "$cmd" >/dev/null || missing+=("$cmd")
done
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk, Adw' 2>/dev/null \
  || missing+=("python3-gi + gir1.2-gtk-4.0 + gir1.2-adw-1")

if ((${#missing[@]})); then
  printf '\nEksik: %s\n\n' "${missing[*]}"
  echo "Debian/Ubuntu için:"
  echo "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 policykit-1 \\"
  echo "                   util-linux curl openssl xz-utils gzip zstd unzip dosfstools"
  die "Önce bunları kur."
fi

for opt in xz zstd unzip; do
  command -v "$opt" >/dev/null || echo "  uyarı: $opt yok — ilgili biçim yazılamaz"
done

say "Ayrıcalıklı helper kuruluyor -> $LIBEXEC/kalip-helper"
sudo install -d -m 0755 "$LIBEXEC"
sudo install -m 0755 -o root -g root "$REPO/helper/kalip-helper" "$LIBEXEC/kalip-helper"

say "polkit kuralı kuruluyor -> $POLKIT_DIR/me.soylu.kalip.policy"
sudo install -m 0644 -o root -g root "$REPO/data/me.soylu.kalip.policy" \
  "$POLKIT_DIR/me.soylu.kalip.policy"

say "Başlatıcı kuruluyor -> $BINDIR/kalip"
sudo tee "$BINDIR/kalip" >/dev/null <<LAUNCHER
#!/usr/bin/env bash
# kalıp başlatıcı — install.sh tarafından üretildi
exec python3 -m kalip "\$@"
LAUNCHER
sudo sed -i "2i export PYTHONPATH=\"$REPO\${PYTHONPATH:+:\$PYTHONPATH}\"" "$BINDIR/kalip"
sudo chmod 0755 "$BINDIR/kalip"

say "Masaüstü girdisi kuruluyor -> $DESKTOP_DIR"
install -d -m 0755 "$DESKTOP_DIR"
install -m 0644 "$REPO/data/me.soylu.kalip.desktop" "$DESKTOP_DIR/me.soylu.kalip.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" || true

echo
say "Kuruldu. Çalıştır: kalip"
echo "    Helper: $LIBEXEC/kalip-helper (root'a ait kopya)"
echo "    Kaynak: $REPO"
