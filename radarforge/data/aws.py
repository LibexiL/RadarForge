"""Anonymous access to the NOAA/Unidata NEXRAD buckets on AWS S3.

Buckets (verified 2026):
  unidata-nexrad-level2         YYYY/MM/DD/SITE/SITEYYYYMMDD_HHMMSS_V06
  unidata-nexrad-level2-chunks  SITE/<volume 1-999>/YYYYMMDD-HHMMSS-<nnn>-<S|I|E>
  unidata-nexrad-level3         SSS_PPP_YYYY_MM_DD_HH_MM_SS  (flat)
"""
from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from ..config import CACHE_DIR

L2_BUCKET = "unidata-nexrad-level2"
CHUNK_BUCKET = "unidata-nexrad-level2-chunks"
L3_BUCKET = "unidata-nexrad-level3"
USER_AGENT = "RadarForge/1.0 (NEXRAD viewer; +https://github.com/)"

_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_local = threading.local()


def session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        # retry dropped connections and S3's occasional 500/503 ("slow down") with a short back-off
        from urllib3.util.retry import Retry
        retry = Retry(total=4, connect=3, read=2, status=3, backoff_factor=0.4,
                      status_forcelist=(500, 502, 503, 504), allowed_methods=frozenset(["GET", "HEAD"]),
                      raise_on_status=False)
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _local.session = s
    return s


def friendly_error(exc) -> str:
    """A short, readable reason for a failed request (for the status bar), instead of urllib3's essay."""
    host = ""
    url = (getattr(getattr(exc, "request", None), "url", None) or
           getattr(getattr(exc, "response", None), "url", None) or "")
    m = re.search(r"host='([^']+)'", str(exc)) or re.match(r"https?://([^/:]+)", url)
    if m:
        host = m.group(1)
    where = host or "the server"
    if isinstance(exc, requests.Timeout):
        return f"{where} didn't answer in time"
    if isinstance(exc, requests.ConnectionError):
        text = str(exc)
        if "NameResolution" in text or "getaddrinfo" in text or "Name or service not known" in text:
            return f"can't look up {where} – check the internet connection"
        if "SSL" in text or "CERTIFICATE" in text.upper():
            return f"secure connection to {where} failed"
        return f"no connection to {where}"
    if isinstance(exc, requests.HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        return f"{where} answered {code}" if code else f"{where} refused the request"
    if isinstance(exc, ValueError) and "JSON" in str(exc):
        return f"{where} sent something unreadable"
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 140 else text[:137] + "…"


def bucket_url(bucket: str) -> str:
    return f"https://{bucket}.s3.amazonaws.com"


@dataclass
class S3Object:
    key: str
    size: int
    modified: str


def list_objects(bucket: str, prefix: str = "", delimiter: str | None = None,
                 max_keys: int = 1000, start_after: str | None = None,
                 max_pages: int = 20) -> tuple[list, list]:
    """ListObjectsV2. Returns (objects, common_prefixes)."""
    objs, prefixes = [], []
    token = None
    for _ in range(max_pages):
        params = {"list-type": "2", "prefix": prefix, "max-keys": str(max_keys)}
        if delimiter:
            params["delimiter"] = delimiter
        if start_after and not token:
            params["start-after"] = start_after
        if token:
            params["continuation-token"] = token
        r = session().get(bucket_url(bucket) + "/", params=params, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for c in root.findall(f"{_NS}Contents"):
            objs.append(S3Object(c.findtext(f"{_NS}Key"), int(c.findtext(f"{_NS}Size") or 0),
                                 c.findtext(f"{_NS}LastModified") or ""))
        for p in root.findall(f"{_NS}CommonPrefixes"):
            prefixes.append(p.findtext(f"{_NS}Prefix"))
        if root.findtext(f"{_NS}IsTruncated") == "true":
            token = root.findtext(f"{_NS}NextContinuationToken")
            if not token:
                break
        else:
            break
    return objs, prefixes


def fetch(bucket: str, key: str, cache: bool = True, timeout: float = 60) -> bytes:
    path = CACHE_DIR / "s3" / bucket / key
    if cache and path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    r = session().get(f"{bucket_url(bucket)}/{key}", timeout=timeout)
    r.raise_for_status()
    data = r.content
    if cache:
        for _attempt in range(2):          # the folder may vanish under us (cache tidy-up): make it again once
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
                tmp.write_bytes(data)
                os.replace(tmp, path)
                break
            except OSError:
                continue
    return data


def cached_path(bucket: str, key: str) -> Path:
    return CACHE_DIR / "s3" / bucket / key


_chunk_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="rf-chunks")   # live chunks only


def fetch_many(bucket: str, keys: list, cache: bool = False, timeout: float = 30, cancelled=None) -> list:
    """Download several objects in parallel. Returns [(key, bytes or Exception)] in the order given.
    `cancelled()` (optional) is checked before each download starts."""
    def one(k):
        if cancelled is not None and cancelled():
            return k, RuntimeError("cancelled")
        try:
            return k, fetch(bucket, k, cache=cache, timeout=timeout)
        except Exception as exc:          # noqa: BLE001 - handed back to the caller
            return k, exc
    return list(_chunk_pool.map(one, keys))


def prune_cache(max_bytes: float = 3e9, max_age_days: float = 10.0) -> int:
    """Delete downloaded radar files older than max_age_days, then the oldest until the cache is under
    max_bytes. Returns the number of files removed. Safe to run in the background."""
    root = CACHE_DIR / "s3"
    if not root.exists():
        return 0
    files = []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            p = Path(dirpath) / n
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
    files.sort()
    total = sum(f[1] for f in files)
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for mtime, size, p in files:
        if mtime >= cutoff and total <= max_bytes:
            break
        try:
            p.unlink()
            total -= size
            removed += 1
        except OSError:
            pass
    day_ago = time.time() - 86400
    for dirpath, dirs, names in os.walk(root, topdown=False):      # tidy empty folders (not fresh ones)
        if not dirs and not names and Path(dirpath) != root:
            try:
                if os.stat(dirpath).st_mtime < day_ago:
                    os.rmdir(dirpath)
            except OSError:
                pass
    return removed


# --------------------------------------------------------------------------- #
# Level II archive
# --------------------------------------------------------------------------- #
_L2_RE = re.compile(r"([A-Z0-9]{4})(\d{8})_(\d{6})(_V\d\d)?(\.gz|\.bz2)?$")


@dataclass
class L2File:
    key: str
    time: datetime
    size: int

    @property
    def name(self):
        return self.key.rsplit("/", 1)[-1]


def list_level2(site: str, day: date) -> list:
    prefix = f"{day:%Y/%m/%d}/{site.upper()}/"
    objs, _ = list_objects(L2_BUCKET, prefix)
    out = []
    for o in objs:
        name = o.key.rsplit("/", 1)[-1]
        if name.endswith("_MDM") or name.endswith(".tar"):
            continue
        m = _L2_RE.search(name)
        if not m:
            continue
        t = datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        out.append(L2File(o.key, t, o.size))
    out.sort(key=lambda f: f.time)
    return out


def list_level2_range(site: str, start: datetime, end: datetime) -> list:
    out = []
    d = start.date()
    while d <= end.date():
        out.extend(f for f in list_level2(site, d) if start <= f.time <= end)
        d += timedelta(days=1)
    return out


def latest_level2(site: str, count: int = 1) -> list:
    now = datetime.now(timezone.utc)
    files = list_level2(site, now.date())
    if len(files) < count:
        files = list_level2(site, (now - timedelta(days=1)).date()) + files
    return files[-count:]


# --------------------------------------------------------------------------- #
# Level III
# --------------------------------------------------------------------------- #
_L3_RE = re.compile(r"^([A-Z0-9]{3})_([A-Z0-9]{3})_(\d{4})_(\d\d)_(\d\d)_(\d\d)_(\d\d)_(\d\d)$")


@dataclass
class L3File:
    key: str
    site: str
    product: str
    time: datetime


def l3_products(site3: str) -> list:
    """Product codes the bucket holds for a site (one request)."""
    _, prefixes = list_objects(L3_BUCKET, f"{site3.upper()}_", delimiter="_")
    return sorted({p.split("_")[1] for p in prefixes if p.count("_") >= 2})


def list_level3(site3: str, product: str, day: date) -> list:
    prefix = f"{site3.upper()}_{product.upper()}_{day:%Y_%m_%d}_"
    objs, _ = list_objects(L3_BUCKET, prefix)
    out = []
    for o in objs:
        m = _L3_RE.match(o.key)
        if not m:
            continue
        t = datetime(*map(int, m.groups()[2:]), tzinfo=timezone.utc)
        out.append(L3File(o.key, m.group(1), m.group(2), t))
    out.sort(key=lambda f: f.time)
    return out


def list_level3_range(site3: str, product: str, start: datetime, end: datetime) -> list:
    out = []
    d = start.date()
    while d <= end.date():
        out.extend(f for f in list_level3(site3, product, d) if start <= f.time <= end)
        d += timedelta(days=1)
    return out


def latest_level3(site3: str, product: str, count: int = 1) -> list:
    now = datetime.now(timezone.utc)
    files = list_level3(site3, product, now.date())
    if len(files) < count:
        files = list_level3(site3, product, (now - timedelta(days=1)).date()) + files
    return files[-count:]


# --------------------------------------------------------------------------- #
# Real-time Level II chunks
# --------------------------------------------------------------------------- #
_CHUNK_RE = re.compile(r"/(\d+)/(\d{8}-\d{6})-(\d{3})-([SIE])$")


@dataclass
class Chunk:
    key: str
    volume: int
    stamp: str        # YYYYMMDD-HHMMSS of volume start
    number: int
    kind: str         # S, I or E

    @property
    def time(self) -> datetime:
        return datetime.strptime(self.stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


def list_chunks(site: str, volume: int) -> list:
    objs, _ = list_objects(CHUNK_BUCKET, f"{site.upper()}/{volume}/")
    chunks = []
    for o in objs:
        m = _CHUNK_RE.search(o.key)
        if m:
            chunks.append(Chunk(o.key, int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)))
    if not chunks:
        return []
    newest = max(c.stamp for c in chunks)       # volume numbers are reused every ~999 volumes
    chunks = [c for c in chunks if c.stamp == newest]
    chunks.sort(key=lambda c: c.number)
    return chunks


def chunk_volumes(site: str) -> list:
    _, prefixes = list_objects(CHUNK_BUCKET, f"{site.upper()}/", delimiter="/")
    nums = []
    for p in prefixes:
        try:
            nums.append(int(p.strip("/").split("/")[-1]))
        except ValueError:
            pass
    return sorted(nums)


def find_latest_volume(site: str) -> int | None:
    """Binary search the circular volume numbering for the newest volume."""
    nums = chunk_volumes(site)
    if not nums:
        return None
    cache: dict = {}

    def stamp(i):
        n = nums[i]
        if n not in cache:
            ch = list_chunks(site, n)
            cache[n] = ch[-1].stamp if ch else ""
        return cache[n]

    lo, hi = 0, len(nums) - 1
    # rotated sorted array: find index of minimum stamp, newest is the one before it
    while lo < hi:
        mid = (lo + hi) // 2
        if stamp(mid) > stamp(hi):
            lo = mid + 1
        else:
            hi = mid
    idx = (lo - 1) % len(nums)
    best = nums[idx]
    # sanity: check neighbours in case of gaps/leftovers
    for j in (idx - 1, idx + 1):
        n2 = nums[j % len(nums)]
        if stamp(j % len(nums)) > cache.get(best, ""):
            best = n2
    return best


def next_volume_number(n: int) -> int:
    return 1 if n >= 999 else n + 1
