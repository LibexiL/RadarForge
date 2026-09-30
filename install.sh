#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  RadarForge installer for Linux
#  Usage:  bash install.sh
#
#  * creates a private Python environment in ~/.local/share/radarforge/venv
#  * adds the 'radarforge' command (~/.local/bin) and an app-menu entry
#  * running it again updates an existing install (settings are kept)
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA/radarforge"
VENV="$PREFIX/venv"
BIN="$HOME/.local/bin"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- find Python 3.10+ -------------------------------------------------------
PY=""
for cand in "${PYTHON:-}" python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && "$cand" "$HERE/scripts/check_python.py" >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || die "Python 3.10 or newer was not found.
  Fedora / Nobara:  sudo dnf install python3
  Ubuntu / Debian:  sudo apt install python3 python3-venv
  Arch:             sudo pacman -S python"
say "Using $("$PY" "$HERE/scripts/check_python.py")"

# ---- virtual environment -----------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  say "Creating the Python environment in $VENV"
  mkdir -p "$PREFIX"
  "$PY" -m venv "$VENV" || die "could not create a virtual environment.
  Ubuntu / Debian need the venv module:  sudo apt install python3-venv"
else
  say "Updating the existing install in $PREFIX"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip wheel

say "Installing dependencies (PySide6, NumPy, SciPy, MetPy, scikit-image, PyOpenGL) – this can take a few minutes"
"$VENV/bin/python" -m pip install --upgrade -r "$HERE/requirements.txt"

say "Installing RadarForge"
"$VENV/bin/python" -m pip install --quiet --force-reinstall --no-deps "$HERE"

# ---- launcher, icon, app-menu entry ------------------------------------------
mkdir -p "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps" "$DATA/icons/hicolor/256x256/apps"
rm -f "$BIN/radarforge"
cat > "$BIN/radarforge" <<LAUNCH
#!/usr/bin/env bash
exec "$VENV/bin/python" -m radarforge "\$@"
LAUNCH
chmod +x "$BIN/radarforge"
cp "$HERE/radarforge/assets/radarforge.svg" "$DATA/icons/hicolor/scalable/apps/radarforge.svg"
cp "$HERE/radarforge/assets/radarforge.png" "$DATA/icons/hicolor/256x256/apps/radarforge.png"
cat > "$DATA/applications/radarforge.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=RadarForge
GenericName=Weather Radar Viewer
Comment=NEXRAD Level II / Level III radar viewer
Exec=$BIN/radarforge %F
Icon=radarforge
Terminal=false
Categories=Science;Geography;Education;
StartupWMClass=radarforge
DESK
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA/applications" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$DATA/icons/hicolor" >/dev/null 2>&1 || true

say "Done!"
echo "  Start RadarForge from your app menu, or run:  radarforge"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  (the 'radarforge' command needs $BIN on your PATH – log out and back in, or run $BIN/radarforge)";;
esac
