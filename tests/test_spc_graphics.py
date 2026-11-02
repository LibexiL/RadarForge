from datetime import datetime, timezone

from radarforge.features import feeds
from radarforge.features import warnings as W
from radarforge.ui.info_panel import graphic_source

MCD_HTML = """<html><img src="/misc/spc_logo.png"><a href="/products/md/">MDs</a>
<a href="mcd2430.png"><img src="mcd2430.png" width=800></a> <img src='mcd2430_prt.gif'>
<img src="/images/noaa.gif"></html>"""
WW_HTML = """<img src="/new/images/header.gif"><img src="ww0701_radar.gif">
<a href="ww0701_info.png">graphic</a><img SRC="ww0701_radar.gif"><img src="ww0702_radar.gif">"""


def test_graphic_urls_mcd():
    page = "https://www.spc.noaa.gov/products/md/2026/md2430.html"
    assert feeds.graphic_urls(MCD_HTML, page, "mcd2430") == \
        ["https://www.spc.noaa.gov/products/md/2026/mcd2430.png"]


def test_graphic_urls_watch():
    page = feeds.watch_page(701, 2026)
    got = feeds.graphic_urls(WW_HTML, page, "ww0701")
    assert got == ["https://www.spc.noaa.gov/products/watch/2026/ww0701_radar.gif",
                   "https://www.spc.noaa.gov/products/watch/2026/ww0701_info.png"]
    assert len(feeds.graphic_urls(WW_HTML, page, "ww0701", limit=1)) == 1


def test_graphic_source():
    now = datetime(2026, 11, 2, 20, tzinfo=timezone.utc)
    w = W.Alert("Tornado Watch", [], "", now, now, "KWNS", key="KWNS.TO.A.0701")
    g = graphic_source(dict(kind="alert", obj=w))
    assert g["page"].endswith("/products/watch/2026/ww0701.html") and g["token"] == "ww0701"
    iem = W.Alert("Severe Thunderstorm Watch", [], "", now, now, "OUN", key="OUN.SV.A.702")
    assert graphic_source(dict(kind="alert", obj=iem))["token"] == "ww0702"
    warn = W.Alert("Tornado Warning", [], "", now, now, "OUN", key="KOUN.TO.W.0042")
    assert graphic_source(dict(kind="alert", obj=warn)) is None
    g = graphic_source(dict(kind="mcd", obj=dict(number=2430, issue=now)))
    assert g["page"].endswith("/products/md/2026/md2430.html") and g["fallbacks"] == ("mcd2430.png",)
    g = graphic_source(dict(kind="outlook", obj=dict(title="x", lines=[], day=1)))
    assert g["page"].endswith("/products/outlook/day1otlk.html")


def test_fetch_graphics_falls_back(monkeypatch):
    class R:
        def __init__(self, url):
            self.url = url
            self.text = "<html>no pictures</html>"
            self.content = b"GIF89a"
            self.headers = {"Content-Type": "image/gif" if url.endswith(".gif") else "text/html"}

        def raise_for_status(self):
            pass

    class Sess:
        def get(self, url, **kw):
            return R(url)
    got = feeds.fetch_graphics(feeds.watch_page(5, 2026), "ww0005", ("ww0005_radar.gif",), session=Sess())
    assert got == [("https://www.spc.noaa.gov/products/watch/2026/ww0005_radar.gif", b"GIF89a")]
