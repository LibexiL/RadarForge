#!/usr/bin/env bash
# Builds RadarForge-<version>-x86_64.AppImage from the PyInstaller folder (dist/RadarForge).
#   pyinstaller --noconfirm packaging/radarforge.spec && bash packaging/build_appimage.sh
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(python3 -c 'import re;print(re.search(r"__version__ = \"(.+?)\"", open("radarforge/__init__.py").read()).group(1))')"
DIST="${DIST:-dist/RadarForge}"
[ -x "$DIST/RadarForge" ] || { echo "run pyinstaller first (no $DIST/RadarForge)"; exit 1; }
APPDIR="build/RadarForge.AppDir"
rm -rf "$APPDIR" && mkdir -p "$APPDIR/usr/lib"
cp -a "$DIST" "$APPDIR/usr/lib/radarforge"
cp radarforge/assets/radarforge.png "$APPDIR/radarforge.png"
cat > "$APPDIR/radarforge.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=RadarForge
Comment=NEXRAD weather radar viewer
Exec=RadarForge %F
Icon=radarforge
Categories=Science;Education;
Terminal=false
DESK
cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/radarforge/RadarForge" "$@"
RUN
chmod +x "$APPDIR/AppRun"
TOOL="build/appimagetool-x86_64.AppImage"
if [ ! -x "$TOOL" ]; then
  curl -fL --retry 3 -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi
OUT="RadarForge-$VERSION-x86_64.AppImage"
ARCH=x86_64 "$TOOL" --appimage-extract-and-run --comp zstd "$APPDIR" "dist/$OUT"
echo "Built dist/$OUT"
