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


class Cancelled(Exception):
    """A download stopped because it was no longer wanted (another radar was chosen)."""


def fetch_file(bucket: str, key: str, timeout: float = 60, cancelled=None) -> Path:
    """Downloads an object into the cache (streamed straight to disk, never held whole in memory) and returns
    its path; a file already cached isn't read at all. `cancelled()` (optional) is checked as the data comes
    in, so a download nobody wants any more stops part-way and frees the connection."""
    path = cached_path(bucket, key)
    try:
        if path.stat().st_size > 0:
            return path
    except OSError:
        pass
    if cancelled is not None and cancelled():
        raise Cancelled(key)
    r = session().get(f"{bucket_url(bucket)}/{key}", timeout=timeout, stream=True)
    try:
        r.raise_for_status()
        tmp = None
        for _attempt in range(2):          # the folder may vanish under us (cache tidy-up): make it again once
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
                fh = open(tmp, "wb")
                break
            except OSError:
                tmp = None
        if tmp is None:                    # the cache can't be written (disk full?): a private temporary copy
            import tempfile
            fd, name = tempfile.mkstemp(prefix="rf_", suffix="_" + key.rsplit("/", 1)[-1])
            tmp, path, fh = Path(name), None, os.fdopen(fd, "wb")
        try:
            with fh:
                for block in r.iter_content(1 << 18):
                    if cancelled is not None and cancelled():
                        raise Cancelled(key)
                    fh.write(block)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        if path is None:
            return tmp
        os.replace(tmp, path)
        return path
    finally:
        r.close()


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


def _list_level2_after(site: str, after: datetime) -> list:
    """Archive files from [after] on (S3 lists only the keys after it: one small response, not the whole day)."""
    out = []
    d = after.date()
    end = datetime.now(timezone.utc).date()
    while d <= end:
        prefix = f"{d:%Y/%m/%d}/{site.upper()}/"
        start = prefix + f"{site.upper()}{after:%Y%m%d_%H%M%S}" if d == after.date() else None
        objs, _ = list_objects(L2_BUCKET, prefix, start_after=start)
        for o in objs:
            name = o.key.rsplit("/", 1)[-1]
            m = _L2_RE.search(name)
            if not m or name.endswith(("_MDM", ".tar")):
                continue
            t = datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            out.append(L2File(o.key, t, o.size))
        d += timedelta(days=1)
    out.sort(key=lambda f: f.time)
    return out


_latest_memo: dict = {}                # site -> (when, files): the newest volume and the loop share one listing
_latest_lock = threading.Lock()


def latest_level2(site: str, count: int = 1) -> list:
    site = site.upper()
    now = datetime.now(timezone.utc)
    with _latest_lock:
        m = _latest_memo.get(site)
        if m is not None and time.time() - m[0] < 20 and len(m[1]) >= count:
            return m[1][-count:]
    # just the last stretch of the day (volumes come every 4-10 minutes): far less to download than the whole day
    files = []
    try:
        files = _list_level2_after(site, now - timedelta(minutes=(count + 4) * 12))
    except Exception:
        files = []
    if len(files) < count:
        files = list_level2(site, now.date())
        if len(files) < count:
            files = list_level2(site, (now - timedelta(days=1)).date()) + files
    with _latest_lock:
        _latest_memo[site] = (time.time(), files)
    return files[-count:]


def forget_listings():
    with _latest_lock:
        _latest_memo.clear()


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


def level3_after(site3: str, product: str, after_key: str) -> list:
    """Files newer than [after_key] (a cheap listing: S3 returns only the keys after it)."""
    now = datetime.now(timezone.utc)
    days = [now.date()]
    if now.hour == 0 and now.minute < 20:          # just after midnight: yesterday's last ones can still arrive
        days.insert(0, (now - timedelta(days=1)).date())
    out = []
    for day in days:
        prefix = f"{site3.upper()}_{product.upper()}_{day:%Y_%m_%d}_"
        objs, _ = list_objects(L3_BUCKET, prefix, start_after=max(after_key, prefix))
        for o in objs:
            m = _L3_RE.match(o.key)
            if m:
                t = datetime(*map(int, m.groups()[2:]), tzinfo=timezone.utc)
                out.append(L3File(o.key, m.group(1), m.group(2), t))
    out.sort(key=lambda f: f.time)
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


_list_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rf-list")    # listings side by side
_last_volume: dict = {}                # site -> newest volume number seen (kept between runs)
_VOLS_FILE = CACHE_DIR / "live_volumes.json"


def _load_volumes():
    import json
    try:
        _last_volume.update({k: int(v) for k, v in json.loads(_VOLS_FILE.read_text()).items()})
    except Exception:
        pass


def _remember_volume(site, n):
    import json
    if _last_volume.get(site) == n:
        return
    _last_volume[site] = n
    try:
        _VOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _VOLS_FILE.with_name(f"{_VOLS_FILE.name}.{os.getpid()}.{threading.get_ident()}.part")
        tmp.write_text(json.dumps(_last_volume))
        os.replace(tmp, _VOLS_FILE)
    except OSError:
        pass


_load_volumes()


def _volume_stamp(site, n):
    ch = list_chunks(site, n)
    return ch[-1].stamp if ch else ""


def _near_volume(site, hint, now=None):
    """The newest volume among hint .. hint+5 (the numbers it has moved on by since), checked side by side; None
    when that can't be told from them (nothing there, the newest is the last checked, or it is old)."""
    cands = [(hint - 1 + k) % 999 + 1 for k in range(6)]
    stamps = list(_list_pool.map(lambda n: _volume_stamp(site, n), cands))
    best = max(range(len(cands)), key=lambda i: stamps[i])
    if not stamps[best] or best == len(cands) - 1:
        return None
    t = datetime.strptime(stamps[best], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    if (now or datetime.now(timezone.utc)) - t > timedelta(minutes=20):
        return None
    return cands[best]


def find_latest_volume(site: str, hint: int | None = None) -> int | None:
    """The newest volume number. Starts next to the last one seen (one round of side-by-side listings), and
    otherwise searches the circular numbering, 16 listings at a time."""
    site = site.upper()
    hint = hint or _last_volume.get(site)
    if hint:
        try:
            n = _near_volume(site, hint)
        except Exception:
            n = None
        if n is not None:
            _remember_volume(site, n)
            return n
    nums = chunk_volumes(site)
    if not nums:
        return None
    n = search_newest(nums, lambda v: _volume_stamp(site, v))
    if n is not None:
        _remember_volume(site, n)
    return n


def search_newest(nums: list, stamp_of, width: int = 16):
    """The number with the newest stamp in a list sorted by number whose stamps rise and then drop once (the
    numbering wraps). Each round looks at [width] numbers side by side and narrows to the stretch holding the
    drop; a short stretch is read whole."""
    cache: dict = {}

    def get(idxs):
        todo = [i for i in idxs if nums[i] not in cache]
        for i, st in zip(todo, _list_pool.map(lambda i: stamp_of(nums[i]), todo)):
            cache[nums[i]] = st
        return [cache[nums[i]] for i in idxs]
    lo, hi = 0, len(nums) - 1
    while True:
        if hi - lo + 1 <= width:
            idxs = list(range(lo, hi + 1))
            st = get(idxs)
            k = max(range(len(idxs)), key=lambda j: st[j])
            best = idxs[k]
            break
        idxs = sorted({lo + round((hi - lo) * j / (width - 1)) for j in range(width)})
        st = get(idxs)
        k = max(range(len(idxs)), key=lambda j: st[j])
        if not st[k]:
            return None
        # the newest lies between the newest sample and the one after it
        lo, hi = idxs[k], (idxs[k + 1] if k + 1 < len(idxs) else idxs[k])
    # neighbours (gaps and leftovers)
    st_best = get([best])[0]
    for j in ((best - 1) % len(nums), (best + 1) % len(nums)):
        if get([j])[0] > st_best:
            best, st_best = j, get([j])[0]
    return nums[best]


def next_volume_number(n: int) -> int:
    return 1 if n >= 999 else n + 1
