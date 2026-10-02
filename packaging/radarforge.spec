# PyInstaller recipe for RadarForge. Run through packaging/build.py (it knows where the files are).
# One folder, no console on Windows. The map data, the radar site list and the colour tables are package data.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
IS_WINDOWS = os.name == "nt"

datas = [(os.path.join(ROOT, "radarforge", "assets"), "radarforge/assets"),
         (os.path.join(ROOT, "radarforge", "data", "sites.json"), "radarforge/data"),
         (os.path.join(ROOT, "radarforge", "products", "palettes"), "radarforge/products/palettes")]
for pkg in ("metpy", "pint", "imageio_ffmpeg", "matplotlib"):
    datas += collect_data_files(pkg)
for pkg in ("metpy", "imageio_ffmpeg"):
    datas += copy_metadata(pkg)

hidden = collect_submodules("OpenGL.platform") + collect_submodules("OpenGL.GL") + [
    "OpenGL.arrays.numpymodule", "OpenGL.arrays.ctypesarrays", "OpenGL.arrays.lists", "OpenGL.arrays.numbers",
    "matplotlib.backends.backend_qtagg", "scipy.special._cdflib", "h5py.defs", "h5py.utils", "h5py._proxy"]

a = Analysis([os.path.join(ROOT, "packaging", "launcher.py")], pathex=[ROOT], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
                       "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts",
                       "PySide6.QtDataVisualization", "pytest", "IPython"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="RadarForge", console=False,
          icon=os.path.join(ROOT, "radarforge", "assets", "radarforge.ico") if IS_WINDOWS else None)
coll = COLLECT(exe, a.binaries, a.datas, name="RadarForge")
