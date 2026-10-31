"""The drawer: one page at a time, folding away, switch pages, and its remembered state (no network)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_drawer_pages_fold_and_state():
    from PySide6.QtWidgets import QApplication, QLabel
    QApplication.instance() or QApplication([])
    from radarforge.ui.drawer import Drawer
    d = Drawer([("radar", "Radar", "radar", "", [("products", "Radar", QLabel("p"))]),
                ("weather", "Weather", "cloud", "", [("severe", "Severe", QLabel("s")),
                                                     ("winter", "Winter", QLabel("w"))])])
    assert d.is_open() and d.current_key() == "products"
    assert d.show_key("winter") and d.current_key() == "winter" and d.is_open("winter")
    assert not d.is_open("products") and not d.show_key("nope")
    d._nav_clicked("weather")                       # the page already showing: fold away
    assert not d.is_open() and not d.body.isVisibleTo(d)
    d._nav_clicked("radar")
    assert d.is_open("products")
    a = d.action("severe")
    a.setChecked(True)
    assert d.is_open("severe")
    st = d.state()
    d.set_open(False)
    d2 = Drawer([("radar", "Radar", "radar", "", [("products", "Radar", QLabel("p"))]),
                 ("weather", "Weather", "cloud", "", [("severe", "Severe", QLabel("s")),
                                                      ("winter", "Winter", QLabel("w"))])])
    d2.restore(st)
    assert d2.current_key() == "severe" and d2.is_open()
    d2.restore({"part": "winter", "open": False})
    assert d2.current_key() == "winter" and not d2.is_open()


def test_flow_layout_wraps():
    from PySide6.QtWidgets import QApplication, QToolButton
    QApplication.instance() or QApplication([])
    from radarforge.ui.flow import flow_box
    box, lay = flow_box(6, 6)
    for t in ("One", "Two", "Three", "Four", "Five"):
        b = QToolButton()
        b.setText(t)
        lay.addWidget(b)
    wide, narrow = lay.heightForWidth(2000), lay.heightForWidth(80)
    assert narrow > wide                             # wraps onto more lines when narrow
