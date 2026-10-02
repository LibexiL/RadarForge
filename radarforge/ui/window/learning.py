"""Learn mode: historic storms to step through (Help → Learn)."""
from __future__ import annotations

from ...services import learn
from ...services.commands import Command
from ..dialogs.learn import LearnDialog
from .views import PRODUCT_IDS


class LearnMixin:
    """Learn mode: each step of a historic event is a view that the main window goes to."""

    def open_learn(self, event_id=None):
        dlg = getattr(self, "_learn_dlg", None)
        if dlg is None:
            dlg = self._learn_dlg = LearnDialog(self)
        if isinstance(event_id, str):
            dlg.select(event_id)
        dlg.show()
        dlg.raise_()

    def learn_step(self, event: dict, index: int):
        """Go to one step of an event: the radar, the time (loads the archive), the panels and the zoom."""
        self.apply_view(learn.step_view(event, index, PRODUCT_IDS))

    def _extra_commands(self, order) -> list:
        return [Command(f"Learn: {e['title']} ({e['date']})", "Help › Learn",
                        lambda eid=e["id"]: self.open_learn(eid), detail=e["about"], keywords="historic event tutorial",
                        order=order) for e in learn.EVENTS]
