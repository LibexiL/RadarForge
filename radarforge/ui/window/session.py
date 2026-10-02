"""Saving state and closing."""
from __future__ import annotations




class SessionMixin:
    """Saving state and closing."""

    def _save_state(self):
        s = self.settings
        s["workspace"] = self.ws.state()
        s["view"] = {"cx": self.view.cx, "cy": self.view.cy, "scale": self.view.scale}
        s["window_geometry"] = bytes(self.saveGeometry()).hex()
        s["panels"] = [p.product for p in self.view.panels] + list(s["panels"])[len(self.view.panels):]
        s.save()

    def prepare_restart(self):
        """Save everything and stop background work before the app re-executes itself."""
        try:
            self._save_state()
            self.data.stop_live()
            from ...data.level2 import shutdown_pool
            shutdown_pool()
        except Exception as exc:
            print("restart cleanup:", exc)

    def closeEvent(self, ev):
        self._save_state()
        for f in list(self.ws.floats):          # floating panels go away with the main window
            f._closing = True
            f.hide()
        self.data.stop_live()
        from ...data.level2 import shutdown_pool
        shutdown_pool()                          # don't wait for decoder processes to finish
        super().closeEvent(ev)
