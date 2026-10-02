"""Build a packaged RadarForge (no Python needed to run it).

    python packaging/build.py                # PyInstaller folder in dist/RadarForge, tested with --self-test
    python packaging/build.py --appimage     # Linux: also dist/RadarForge-<version>-x86_64.AppImage
    python packaging/build.py --zip          # also a zip (Windows) or tar.gz (Linux) of the folder

Needs `pip install pyinstaller` and the program's own requirements. The AppImage step needs `appimagetool` on the PATH
(https://github.com/AppImage/appimagetool/releases).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST = ROOT / "dist"


def version() -> str:
    ns: dict = {}
    exec((ROOT / "radarforge" / "__init__.py").read_text(encoding="utf-8"), ns)
    return ns["__version__"]


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def pyinstaller() -> Path:
    work = Path(tempfile.mkdtemp(prefix="rf_pyi_"))
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--workpath", work, "--distpath", DIST,
         HERE / "radarforge.spec"], cwd=ROOT)
    shutil.rmtree(work, ignore_errors=True)
    return DIST / "RadarForge"


def self_test(folder: Path):
    exe = folder / ("RadarForge.exe" if os.name == "nt" else "RadarForge")
    report = folder.parent / "selftest.txt"
    run([exe, "--self-test", report])                     # a windowed build has no terminal: it writes a report
    text = report.read_text(encoding="utf-8")
    print(text)
    if "FAILED" in text:
        sys.exit("the packaged build is missing something (see above)")


def appimage(folder: Path) -> Path:
    tool = shutil.which("appimagetool")
    if tool is None:
        sys.exit("appimagetool not found: download it from https://github.com/AppImage/appimagetool/releases")
    appdir = DIST / "RadarForge.AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    lib = appdir / "usr" / "lib" / "radarforge"
    shutil.copytree(folder, lib, symlinks=True)
    shutil.copy2(HERE / "linux" / "AppRun", appdir / "AppRun")
    shutil.copy2(HERE / "linux" / "radarforge.desktop", appdir / "radarforge.desktop")
    shutil.copy2(ROOT / "radarforge" / "assets" / "radarforge.png", appdir / "radarforge.png")
    out = DIST / f"RadarForge-{version()}-x86_64.AppImage"
    run([tool, appdir, out], env={**os.environ, "ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1"})
    shutil.rmtree(appdir, ignore_errors=True)
    return out


def archive(folder: Path) -> Path:
    name = f"RadarForge-{version()}-{'windows-x64' if os.name == 'nt' else 'linux-x86_64'}"
    fmt = "zip" if os.name == "nt" else "gztar"
    return Path(shutil.make_archive(str(DIST / name), fmt, root_dir=folder.parent, base_dir=folder.name))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--appimage", action="store_true", help="Linux: wrap the folder as an AppImage")
    ap.add_argument("--zip", action="store_true", help="also make a zip / tar.gz of the folder")
    args = ap.parse_args()
    folder = pyinstaller()
    self_test(folder)
    if args.appimage:
        print("built", appimage(folder))
    if args.zip:
        print("built", archive(folder))
    print("built", folder)


if __name__ == "__main__":
    main()
