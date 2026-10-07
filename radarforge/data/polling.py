"""Live Level II from a GR2Analyst-style polling server (instead of NOAA's buckets on AWS).

A polling server has one folder per radar with a dir.list file naming the newest volumes, one per line as
"<size> <file name>" (some servers list names only):

    https://mesonet-nexrad.agron.iastate.edu/level2/raw/KTLX/dir.list
    https://mesonet-nexrad.agron.iastate.edu/level2/raw/KTLX/KTLX_20261007_201643  (or ..._201643.bz2)

Each file is a whole Archive II volume; the newest one grows while the radar scans, so it is fetched again
with a byte-range request for just the new part (what the servers ask clients to do). Subscription servers
take a user name and password in the address: https://user:password@server/path/.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..config import CACHE_DIR

IEM_URL = "https://mesonet-nexrad.agron.iastate.edu/level2/raw/"
DEFAULT_SERVERS = [{"name": "Iowa State (IEM)", "url": IEM_URL}]

_TIME = re.compile(r"(\d{8})[_-]?(\d{4})(\d{2})?(?!\d)")


@dataclass
class PollFile:
    name: str
    size: int | None
    time: datetime


def parse_dir_list(text: str, site: str = "") -> list:
    """Volumes listed in a dir.list, oldest first. Lines are "<size> <name>" or just "<name>"."""
    out = {}
    for line in (text or "").splitlines():
        tok = line.split()
        if not tok:
            continue
        name = tok[-1]
        if name.lower().endswith((".list", ".cfg", ".txt")) or name.endswith("/"):
            continue
        size = None
        if len(tok) >= 2 and tok[0].isdigit():
            size = int(tok[0])
        m = _TIME.search(name)
        if not m:
            continue
        d, hm, ss = m.group(1), m.group(2), m.group(3) or "00"
        try:
            t = datetime.strptime(d + hm + ss, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out[name] = PollFile(name, size, t)
    return sorted(out.values(), key=lambda f: (f.time, f.name))


def server_label(url: str, servers=None) -> str:
    """A short name for a source: the saved server's name, or its host."""
    if not url or url == "aws":
        return "NOAA on AWS"
    for s in servers or []:
        if s.get("url") == url and s.get("name"):
            return s["name"]
    return urlsplit(url).hostname or url


def safe_url(url: str) -> str:
    """The address without a password, for messages."""
    p = urlsplit(url)
    if p.password:
        host = p.hostname or ""
        if p.port:
            host += f":{p.port}"
        return p._replace(netloc=f"{p.username}:***@{host}").geturl()
    return url


class PollingClient:
    """dir.list polling and incremental downloads for one radar on one server."""

    def __init__(self, base_url: str, site: str, timeout: float = 20):
        self.base = base_url.strip()
        if not self.base.endswith("/"):
            self.base += "/"
        self.site = site.upper()
        self.timeout = timeout
        self.busy = False
        self.cancelled = False
        self.failures = 0
        self._have: dict = {}            # file name -> (bytes we have, path)
        host = urlsplit(self.base).hostname or "server"
        self.dir = CACHE_DIR / "poll" / re.sub(r"[^A-Za-z0-9.-]", "_", host) / self.site

    def _get(self, url, headers=None):
        from . import aws
        h = {"Cache-Control": "no-cache"}
        h.update(headers or {})
        return aws.session().get(url, timeout=self.timeout, headers=h)

    def list(self) -> list:
        r = self._get(f"{self.base}{self.site}/dir.list")
        if r.status_code == 404:
            raise FileNotFoundError(f"{safe_url(self.base)} has no {self.site} folder")
        r.raise_for_status()
        return parse_dir_list(r.content.decode("latin-1", "replace"), self.site)

    def known_size(self, name: str) -> int:
        return self._have.get(name, (0, None))[0]

    def fetch(self, pf: PollFile):
        """Downloads [pf] (only the new bytes when part of it is already here). Returns the local path of the
        whole file, or None when nothing changed."""
        have, old_path = self._have.get(pf.name, (0, None))
        if pf.size is not None and have and pf.size <= have:
            return None
        url = f"{self.base}{self.site}/{pf.name}"
        head = b""
        if have and old_path and os.path.exists(old_path):
            r = self._get(url, {"Range": f"bytes={have}-"})
            if r.status_code == 416:            # nothing new
                return None
            r.raise_for_status()
            if r.status_code == 206:
                with open(old_path, "rb") as fh:
                    head = fh.read()
            data = head + r.content
        else:
            r = self._get(url)
            r.raise_for_status()
            data = r.content
        if not data or len(data) <= have:
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        # a new name for every revision: decoded volumes are cached by path
        path = self.dir / f"{pf.name}.{len(data)}"
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        if old_path and old_path != str(path):
            try:
                Path(old_path).unlink()
            except OSError:
                pass
        self._have[pf.name] = (len(data), str(path))
        return str(path)

    def forget_older(self, keep: set):
        """Deletes downloads of volumes that have left the loop."""
        for name in [n for n in self._have if n not in keep]:
            _n, path = self._have.pop(name)
            try:
                Path(path).unlink()
            except (OSError, TypeError):
                pass
