# PyInstaller recipe for the packaged RadarForge (Windows installer and Linux AppImage).
# Build from the repository root:   pyinstaller --noconfirm packaging/radarforge.spec
# The result is dist/RadarForge/ (a folder with RadarForge(.exe) and its libraries).
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
IS_WIN = sys.platform == "win32"

datas = [
    (os.path.join(ROOT, "radarforge", "assets"), os.path.join("radarforge", "assets")),
    (os.path.join(ROOT, "radarforge", "data", "sites.json"), os.path.join("radarforge", "data")),
    (os.path.join(ROOT, "radarforge", "products", "palettes"), os.path.join("radarforge", "products", "palettes")),
]
datas += collect_data_files("metpy")              # Level III tables
datas += collect_data_files("pint")               # unit definitions used by MetPy
datas += collect_data_files("imageio_ffmpeg", include_py_files=False)   # the bundled ffmpeg (MP4 export)
binaries = []

hidden = (collect_submodules("radarforge") + collect_submodules("OpenGL.platform") +
          collect_submodules("OpenGL.arrays") + collect_submodules("metpy.io") +
          ["OpenGL.GL", "OpenGL.GL.shaders", "PIL.GifImagePlugin", "imageio.plugins.ffmpeg",
           "imageio.plugins.pillow", "scipy.ndimage", "skimage.measure", "h5py", "PySide6.QtMultimedia"])

a = Analysis(
    [os.path.join(SPECPATH, "radarforge_launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "cv2", "sklearn", "IPython", "jupyter_client", "notebook", "sphinx", "pytest",
              "shapely", "geonamescache", "matplotlib.tests", "numpy.tests", "scipy.tests", "PySide6.Qt3DAnimation",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick3D",
              "PySide6.QtCharts", "PySide6.QtDataVisualization"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RadarForge",
    console=False,
    icon=os.path.join(ROOT, "radarforge", "assets", "radarforge.ico") if IS_WIN else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="RadarForge")
