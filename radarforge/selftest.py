"""`radarforge --self-test`: check that everything the program needs is present, then exit.

It is what the packaged builds are tested with (the libraries, the data files and the video encoder travel inside
them), and a quick way to see what is missing on a broken source install."""
from __future__ import annotations

import sys


def _check_data():
    from .data import sites
    from .products import catalog, colortable
    from .render import maps
    assert len(sites.all_sites()) > 100, "radar site list"
    assert maps.ASSET.exists(), "basemap"
    assert colortable.PALETTE_DIR.is_dir() and any(colortable.PALETTE_DIR.glob("*.pal")), "colour tables"
    assert catalog.PRODUCTS


def _check_sounding():
    import numpy as np
    from .services.sounding import Sounding, parameters
    p = np.array([1000, 925, 850, 700, 500, 300, 250], float)
    z = np.array([100, 780, 1500, 3100, 5800, 9600, 10900], float)
    t = np.array([28, 22, 17, 6, -12, -41, -52], float)
    td = np.array([22, 18, 12, -2, -25, -50, -60], float)
    u = np.array([2, 8, 12, 15, 22, 35, 40], float)               # m/s
    v = np.array([5, 9, 10, 8, 5, 5, 5], float)
    snd = Sounding.build(p, z, t, td, u, v)
    par = parameters(snd)
    assert par.sbcape is not None and par.sbcape > 0, "MetPy parameters"


def _check_chart():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    import io
    fig = Figure()
    fig.subplots().plot([0, 1], [0, 1])
    fig.savefig(io.BytesIO(), format="png")


def _check_video():
    import imageio_ffmpeg
    import os
    assert os.path.exists(imageio_ffmpeg.get_ffmpeg_exe()), "video encoder (ffmpeg)"


def _check_netcdf():
    import h5py
    import tempfile
    import os
    path = os.path.join(tempfile.mkdtemp(), "t.h5")
    with h5py.File(path, "w") as f:
        f["x"] = [1, 2, 3]


def _check_gl():
    from OpenGL import GL
    assert GL.GL_TRIANGLES == 4      # imports the platform plugin the packaged build needs


CHECKS = (("data files", _check_data), ("sounding maths (MetPy)", _check_sounding), ("charts (matplotlib)", _check_chart),
          ("video encoder", _check_video), ("satellite files (h5py)", _check_netcdf), ("OpenGL bindings", _check_gl))


def run(out=None) -> int:
    out = out or sys.stdout
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"ok      {name}", file=out)
        except Exception as exc:
            failures += 1
            print(f"FAILED  {name}: {exc!r}", file=out)
    return 1 if failures else 0
