"""GR2Analyst-style polling servers: dir.list parsing and incremental (byte-range) downloads."""
import http.server
import threading

import pytest

from radarforge.data import polling


def test_parse_dir_list_formats():
    text = """
    4733853 KTLX_20261007_195701
    5049123 KTLX_20261007_200210.bz2
    KTLX20261007_200715_V06
    812 KTLX_20261007_2012
    junk line without a time
    100 dir.list
    """
    files = polling.parse_dir_list(text, "KTLX")
    assert [f.name for f in files] == ["KTLX_20261007_195701", "KTLX_20261007_200210.bz2",
                                       "KTLX20261007_200715_V06", "KTLX_20261007_2012"]
    assert files[0].size == 4733853 and files[2].size is None
    assert files[3].time.minute == 12 and files[3].time.second == 0
    assert files[1].time.tzinfo is not None


def test_safe_url_and_label():
    u = "https://bob:secret@level2.example.com/raw/"
    assert "secret" not in polling.safe_url(u) and "bob" in polling.safe_url(u)
    assert polling.server_label("aws") == "NOAA on AWS"
    assert polling.server_label(polling.IEM_URL, polling.DEFAULT_SERVERS) == "Iowa State (IEM)"


class _Server(http.server.BaseHTTPRequestHandler):
    files = {}
    log = []

    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        if name == "dir.list":
            body = "".join(f"{len(v)} {k}\n" for k, v in self.files.items()).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
            return
        data = self.files.get(name)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            self.log.append(("206", start))
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
            self.end_headers()
            self.wfile.write(data[start:])
            return
        self.log.append(("200", 0))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(polling, "CACHE_DIR", tmp_path)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/raw"
    srv.shutdown()


def test_growing_file_downloads_only_new_bytes(server):
    _Server.files = {"KDDC_20261007_201643": b"A" * 1000}
    _Server.log = []
    pc = polling.PollingClient(server, "KDDC")
    files = pc.list()
    assert len(files) == 1
    p1 = pc.fetch(files[0])
    assert open(p1, "rb").read() == b"A" * 1000
    assert pc.fetch(files[0]) is None                       # same size in dir.list: nothing to do
    _Server.files["KDDC_20261007_201643"] += b"B" * 500       # the radar scanned some more
    p2 = pc.fetch(pc.list()[0])
    assert open(p2, "rb").read() == b"A" * 1000 + b"B" * 500
    assert _Server.log == [("200", 0), ("206", 1000)]       # the second time only the new part
    assert p1 != p2                                          # a new path for each revision

