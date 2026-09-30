#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  Remove RadarForge (installed with install.sh).
#  Usage:  bash uninstall.sh
# ---------------------------------------------------------------------------
set -uo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/radarforge"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/radarforge"

rm -rf "$DATA/radarforge" "$HOME/.local/bin/radarforge" "$DATA/applications/radarforge.desktop" \
       "$DATA/icons/hicolor/scalable/apps/radarforge.svg" "$DATA/icons/hicolor/256x256/apps/radarforge.png"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA/applications" >/dev/null 2>&1 || true
echo "RadarForge has been removed."

if [ -d "$CONF" ] || [ -d "$CACHE" ]; then
  read -r -p "Also delete your settings, themes and downloaded radar data? [y/N] " ans
  case "$ans" in
    [yY]*) rm -rf "$CONF" "$CACHE"; echo "Settings and downloads deleted." ;;
    *) echo "Kept $CONF and $CACHE." ;;
  esac
fi
