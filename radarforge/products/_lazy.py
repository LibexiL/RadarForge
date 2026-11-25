"""scipy.ndimage, imported the first time it's used rather than at start-up (it takes ~0.4 s to import)."""
from __future__ import annotations

import importlib


class _LazyModule:
    def __init__(self, name: str):
        self._name = name
        self._mod = None

    def __getattr__(self, attr):
        mod = self._mod
        if mod is None:
            mod = self._mod = importlib.import_module(self._name)
        return getattr(mod, attr)


ndimage = _LazyModule("scipy.ndimage")


def warm() -> None:
    """Import scipy.ndimage now (called from a background thread once the window is up)."""
    ndimage.label  # noqa: B018
