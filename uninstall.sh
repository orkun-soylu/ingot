#!/usr/bin/env bash
set -euo pipefail
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
sudo rm -rf /usr/local/libexec/kalip
sudo rm -f /usr/local/bin/kalip /usr/share/polkit-1/actions/me.soylu.kalip.policy
rm -f "$DESKTOP_DIR/me.soylu.kalip.desktop"
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/kalip"
echo "kalıp kaldırıldı. Kaynak dizini duruyor."
