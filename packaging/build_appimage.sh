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
# Libraries every desktop Linux provides and that must come from the host, not the bundle: the host's
# graphics driver, GTK theme bits and gvfs modules get loaded into the process and need the host's own
# glib / libstdc++ / GL (the AppImage project's exclude list; a bundled older glib made Mint abort at start).
EXCLUDE="ld-linux.so.2 ld-linux-x86-64.so.2 libanl.so.1 libBrokenLocale.so.1 libcidn.so.1 libc.so.6 libdl.so.2
  libm.so.6 libmvec.so.1 libnss_compat.so.2 libnss_dns.so.2 libnss_files.so.2 libnss_hesiod.so.2 libnss_nis.so.2
  libnss_nisplus.so.2 libpthread.so.0 libresolv.so.2 librt.so.1 libthread_db.so.1 libutil.so.1 libstdc++.so.6
  libgcc_s.so.1 libGL.so.1 libEGL.so.1 libGLdispatch.so.0 libGLX.so.0 libOpenGL.so.0 libdrm.so.2 libglapi.so.0
  libgbm.so.1 libxcb.so.1 libX11.so.6 libX11-xcb.so.1 libgio-2.0.so.0 libglib-2.0.so.0 libgobject-2.0.so.0
  libgmodule-2.0.so.0 libgthread-2.0.so.0 libgdk_pixbuf-2.0.so.0 libfontconfig.so.1 libthai.so.0 libfreetype.so.6
  libharfbuzz.so.0 libcom_err.so.2 libexpat.so.1 libgpg-error.so.0 libICE.so.6 libSM.so.6 libusb-1.0.so.0
  libuuid.so.1 libz.so.1 libgtk-3.so.0 libgdk-3.so.0 libpango-1.0.so.0 libpangocairo-1.0.so.0 libpangoft2-1.0.so.0
  libcairo.so.2 libcairo-gobject.so.2 libselinux.so.1 libmount.so.1 libblkid.so.1 libpcre2-8.so.0 libdbus-1.so.3
  libasound.so.2 libpulse.so.0 libpulse-simple.so.0 libjack.so.0 libwayland-client.so.0 libwayland-cursor.so.0
  libwayland-egl.so.1 libwayland-server.so.0 libxkbcommon.so.0 libxkbcommon-x11.so.0 libgcrypt.so.20
  libsystemd.so.0 libudev.so.1 libatk-1.0.so.0 libatk-bridge-2.0.so.0 libatspi.so.0 libxcb-render.so.0
  libxcb-shm.so.0 libXext.so.6 libXrender.so.1 libXi.so.6 libXfixes.so.3 libXcursor.so.1 libXrandr.so.2
  libXinerama.so.1 libXss.so.1 libXxf86vm.so.1 libXdamage.so.1 libXcomposite.so.1 libxshmfence.so.1"
for lib in $EXCLUDE; do
  find "$APPDIR/usr/lib/radarforge" -name "$lib" -o -name "$lib.*" | while read -r f; do echo "  not bundled: ${f##*/}"; rm -f "$f"; done
done
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
