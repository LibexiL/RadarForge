"""Update check and installer.

Finds newer releases on GitHub (LibexiL/RadarForge) and installs them the way this copy was installed:

  windows-setup  the Windows installer: the new setup runs silently once RadarForge has closed, then starts it again
  appimage       the Linux AppImage: the file is replaced in place (shortcuts keep working); restart to use it
  linux-venv     Linux/install.sh: the new version's install.sh runs inside RadarForge, then restart
  windows-venv   Windows/install.bat: RadarForge closes, the new install.bat runs in a console, then starts it again
  portable       run.sh / run.bat from a download folder: the new ZIP is saved to Downloads
  git            a git checkout: release notes only (update with git pull)
  manual         anything else: release notes and the release page

Nothing here touches Qt, so it can be tested on its own; ui/updates.py is the dialog.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

REPO = "LibexiL/RadarForge"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
API_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
ENV_API = "RADARFORGE_UPDATE_API"            # testing: another releases list (same JSON as GitHub's)
ENV_OFF = "RADARFORGE_NO_UPDATE_CHECK"       # set to switch the automatic check off (tests, packagers)
UA = {"User-Agent": f"RadarForge/{__version__} (update check; github.com/{REPO})"}
CHECK_EVERY_S = 20 * 3600                    # automatic checks: about once a day
RETRY_S = 3600                               # ... or an hour later when the release's files are still being built


class UpdateError(Exception):
    """A failure to show the user as it is."""


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- versions
def parse_version(text: str):
    """(1, 10, 0) for "v1.10.0" or "1.10.0"; None for anything else (pre-releases like 1.11.0-beta included)."""
    m = re.fullmatch(r"[vV]?(\d+(?:\.\d+){0,3})", (text or "").strip())
    if not m:
        return None
    t = tuple(int(x) for x in m.group(1).split("."))
    return t + (0,) * (4 - len(t))


def is_newer(version: str, than: str = __version__) -> bool:
    a, b = parse_version(version), parse_version(than)
    return a is not None and b is not None and a > b


# --------------------------------------------------------------------------- releases
@dataclass
class Asset:
    name: str
    url: str
    size: int = 0
    digest: str = ""          # "sha256:<hex>" when GitHub lists one


@dataclass
class Release:
    version: str              # "1.11.0"
    tag: str                  # "v1.11.0"
    title: str
    notes: str                # markdown
    page: str                 # the release's web page
    published: str = ""
    assets: list = field(default_factory=list)
    source_zip: str = ""      # GitHub's own archive of the tag

    def asset(self, pattern: str):
        for a in self.assets:
            if re.fullmatch(pattern, a.name, flags=re.IGNORECASE):
                return a
        return None


def parse_releases(data) -> list:
    """Releases from GitHub's JSON, newest first: drafts, pre-releases and odd tags are left out."""
    out = []
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        tag = str(r.get("tag_name") or "")
        v = parse_version(tag)
        if v is None:
            continue
        assets = [Asset(str(a.get("name") or ""), str(a.get("browser_download_url") or ""), int(a.get("size") or 0),
                        str(a.get("digest") or "")) for a in r.get("assets") or [] if isinstance(a, dict)]
        out.append(Release(version=tag.lstrip("vV"), tag=tag, title=str(r.get("name") or tag),
                           notes=str(r.get("body") or ""), page=str(r.get("html_url") or RELEASES_PAGE),
                           published=str(r.get("published_at") or ""), assets=assets,
                           source_zip=str(r.get("zipball_url") or "")))
    out.sort(key=lambda rel: parse_version(rel.version), reverse=True)
    return out


def newer_than(releases: list, current: str = __version__) -> list:
    return [r for r in releases if is_newer(r.version, current)]


def fetch_releases(timeout: float = 15) -> list:
    """The published releases, newest first. Raises UpdateError with a message for the user."""
    import requests
    url = _test_api() or API_URL
    try:
        r = requests.get(url, timeout=timeout, headers={**UA, "Accept": "application/vnd.github+json",
                                                        "X-GitHub-Api-Version": "2022-11-28"})
    except requests.RequestException as exc:
        raise UpdateError(f"GitHub could not be reached ({type(exc).__name__}). Check the internet connection.")
    if r.status_code in (403, 429) and (r.headers.get("X-RateLimit-Remaining") == "0" or r.status_code == 429):
        raise UpdateError("GitHub's limit on update checks from this address was reached. Try again in an hour.")
    if r.status_code != 200:
        raise UpdateError(f"GitHub answered with an error (HTTP {r.status_code}).")
    try:
        return parse_releases(r.json())
    except ValueError:
        raise UpdateError("GitHub's answer could not be read.")


def combined_notes(releases: list, limit: int = 6) -> str:
    """Markdown with the notes of every newer release, newest first."""
    parts = []
    for r in releases[:limit]:
        body = (r.notes or "").strip().replace("\r\n", "\n")
        if not body.startswith("#"):
            body = f"## {r.title or r.version}\n\n{body or '(no notes)'}"
        parts.append(body)
    if len(releases) > limit:
        parts.append(f"…and {len(releases) - limit} older releases: see the [release page]({RELEASES_PAGE}).")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- how this copy was installed
@dataclass
class Install:
    kind: str
    path: Path | None = None      # the AppImage file, the program folder, the venv or the checkout


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return False


def _inside(path, folder) -> bool:
    try:
        return Path(folder).resolve() in Path(path).resolve().parents
    except OSError:
        return False


def detect_install(env=None, frozen=None, plat=None, executable=None, prefix=None, package_dir=None) -> Install:
    """Which of the install kinds (see the module notes) this copy is. Arguments are for tests."""
    env = os.environ if env is None else env
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    plat = sys.platform if plat is None else plat
    exe = Path(executable or sys.executable)
    pkg = Path(package_dir or Path(__file__).resolve().parent)
    prefix = Path(prefix or sys.prefix)
    # $APPIMAGE is inherited by everything started from inside any AppImage (a terminal, an editor): it only
    # means *this* program when this is the packaged program running from that AppImage's own mount
    appimage, appdir = env.get("APPIMAGE"), env.get("APPDIR")
    if (plat.startswith("linux") and frozen and appimage and appdir and Path(appimage).is_file()
            and _inside(exe, appdir)):
        return Install("appimage", Path(appimage))
    if frozen:
        if plat == "win32" and any(exe.parent.glob("unins*.exe")):
            return Install("windows-setup", exe.parent)
        return Install("manual", exe.parent)
    root = pkg.parent
    if (root / ".git").exists():
        return Install("git", root)
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if plat == "win32":
        local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        if _same(prefix, local / "RadarForge" / "venv"):
            return Install("windows-venv", prefix)
    elif plat.startswith("linux"):
        data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
        if _same(prefix, data / "radarforge" / "venv"):
            return Install("linux-venv", prefix)
    if (root / "Linux" / "run.sh").is_file() or (root / "Windows" / "run.bat").is_file():
        return Install("portable", root)
    return Install("manual", root)


SOURCE_KINDS = ("linux-venv", "windows-venv", "portable")


def pick_asset(release: Release, kind: str, machine: str | None = None):
    """The release file this install kind needs, or None (not built yet, or the kind can't install)."""
    machine = (machine or platform.machine() or "").lower()
    v = re.escape(release.version)
    if kind == "windows-setup":
        return release.asset(rf"RadarForge-Setup-{v}\.exe")
    if kind == "appimage":
        return release.asset(rf"RadarForge-{v}-x86_64\.AppImage") if machine in ("x86_64", "amd64") else None
    if kind in SOURCE_KINDS:
        a = release.asset(rf"RadarForge-{v}\.zip")
        if a is None and release.source_zip:
            a = Asset(f"RadarForge-{release.version}-source.zip", release.source_zip)
        return a
    return None


def _mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n >= 1e6 else (f"{n / 1e3:.0f} kB" if n else "")


@dataclass
class Plan:
    asset: Asset | None
    text: str                 # what pressing the button does
    button: str = ""          # empty: nothing to install from here (the release page instead)
    waiting: bool = False     # the file this needs isn't on the release yet


def plan(install: Install, release: Release, machine: str | None = None) -> Plan:
    kind = install.kind
    machine = (machine or platform.machine() or "").lower()
    if kind == "git":
        return Plan(None, f"This copy runs from a git checkout. Update it with <code>git pull</code> in "
                          f"{install.path}.")
    if kind == "manual" or (kind == "appimage" and machine not in ("x86_64", "amd64")):
        return Plan(None, "Download the new version from its release page.")
    a = pick_asset(release, kind, machine)
    if a is None:
        what = {"windows-setup": "Windows installer", "appimage": "AppImage"}.get(kind, "download")
        return Plan(None, f"The {what} for {release.version} isn't on the release page yet: GitHub builds it in the "
                          "first few minutes after a release. Try again shortly.", waiting=True)
    size = _mb(a.size)
    size = f" ({size})" if size else ""
    if kind == "windows-setup":
        return Plan(a, f"Downloads the installer{size}. RadarForge then closes, the update installs, and RadarForge "
                       "starts again. Settings are kept.", "Install and restart")
    if kind == "appimage":
        return Plan(a, f"Downloads the new AppImage{size} and puts it in place of <b>{install.path.name}</b>, so "
                       "shortcuts to it keep working. Restart RadarForge afterwards to use it.", "Download and install")
    if kind == "linux-venv":
        return Plan(a, f"Downloads the new version{size} and runs its installer (install.sh), showing its output "
                       "here. It updates the Python packages too, which can take a few minutes. Restart RadarForge "
                       "when it's done.", "Install update")
    if kind == "windows-venv":
        return Plan(a, f"Downloads the new version{size}. RadarForge then closes, a window shows the installer "
                       "(install.bat) updating it, which can take a few minutes, and RadarForge starts again.",
                    "Install and restart")
    return Plan(a, f"Saves the new version's ZIP{size} to your Downloads folder. Extract it and start RadarForge "
                   "from the new folder (run or install) as before.", "Download")


# --------------------------------------------------------------------------- downloading
def _test_api():
    """The RADARFORGE_UPDATE_API test hook (ignored by the packaged program)."""
    return None if getattr(sys, "frozen", False) else (os.environ.get(ENV_API) or None)


def _allowed(url: str) -> bool:
    """Only this repository's release files (or its source archive) may be downloaded."""
    from urllib.parse import unquote, urlsplit
    try:
        u = urlsplit(url or "")
        host, port = u.hostname, u.port
    except ValueError:
        return False
    path = unquote(u.path)
    if "\\" in path or any(seg in (".", "..") for seg in path.split("/")) or u.username or u.password:
        return False
    test = _test_api()
    if test:
        t = urlsplit(test)
        if (u.scheme, u.netloc) == (t.scheme, t.netloc):
            return True
    if u.scheme != "https" or port not in (None, 443):
        return False
    low = path.lower()
    if host == "github.com":
        return low.startswith(f"/{REPO}/releases/download/".lower())
    if host == "api.github.com":
        return low.startswith(f"/repos/{REPO}/zipball/".lower())
    return False


def download(asset: Asset, dest: Path, progress=None, cancelled=None, timeout: float = 30) -> Path:
    """Downloads [asset] to [dest] (through dest.part), checking its size and SHA-256 when GitHub gives them."""
    import requests
    if not _allowed(asset.url):
        raise UpdateError(f"unexpected download address: {asset.url}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if asset.size:
        free = shutil.disk_usage(dest.parent).free
        if free < asset.size + (20 << 20):
            raise UpdateError(f"not enough free disk space in {dest.parent} ({_mb(free)} free, "
                              f"{_mb(asset.size)} needed)")
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    done = 0
    try:
        with requests.get(asset.url, headers=UA, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                raise UpdateError(f"the download failed (HTTP {r.status_code})")
            total = asset.size or int(r.headers.get("Content-Length") or 0)
            with open(part, "wb") as fh:
                for chunk in r.iter_content(256 * 1024):
                    if cancelled is not None and cancelled():
                        raise Cancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
                fh.flush()
                os.fsync(fh.fileno())
        if asset.size and done != asset.size:
            raise UpdateError(f"the download stopped early ({_mb(done)} of {_mb(asset.size)})")
        if asset.digest.lower().startswith("sha256:") and h.hexdigest() != asset.digest[7:].strip().lower():
            raise UpdateError("the download is damaged (its checksum doesn't match GitHub's)")
        os.replace(part, dest)
    except requests.RequestException as exc:
        _unlink(part)
        raise UpdateError(f"the download failed ({type(exc).__name__})")
    except BaseException:
        _unlink(part)
        raise
    return dest


def _unlink(p: Path):
    try:
        Path(p).unlink()
    except OSError:
        pass


def updates_dir() -> Path:
    from .config import CACHE_DIR
    return CACHE_DIR / "updates"


def clear_old_downloads(min_age_s: float = 3600):
    """Removes downloads from earlier updates (at start-up; anything newer may still be in use)."""
    import time
    d = updates_dir()
    if not d.is_dir():
        return
    for p in d.iterdir():
        try:
            if time.time() - p.stat().st_mtime < min_age_s:
                continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- source installs
def extract_zip(zip_path: Path, dest: Path) -> Path:
    """Extracts a release ZIP into [dest] (emptied first), refusing paths that would land outside it."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    base = dest.resolve()
    try:
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                target = (base / info.filename).resolve()
                if target != base and base not in target.parents:
                    raise UpdateError(f"the download contains an unsafe path: {info.filename}")
            z.extractall(base)
    except zipfile.BadZipFile:
        raise UpdateError("the download is not a valid ZIP file")
    return base


def find_program(root: Path) -> Path | None:
    """The folder holding radarforge/, Linux/ and Windows/ inside an extracted ZIP (it may be one level down)."""
    root = Path(root)
    for cand in [root] + sorted(p for p in root.iterdir() if p.is_dir()):
        if (cand / "radarforge" / "__init__.py").is_file() and (cand / "pyproject.toml").is_file():
            return cand
    return None


def program_version(program: Path) -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (Path(program) / "radarforge" / "__init__.py").read_text("utf-8"))
    return m.group(1) if m else ""


def installer_env() -> dict:
    """Environment for the install scripts: none of this program's Python or Qt settings."""
    env = clean_env()
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "PYTHONNOUSERSITE"):
        env.pop(k, None)
    env["RF_UPDATE"] = "1"
    return env


# The script is plain ASCII and gets every path from the environment: cmd.exe reads .cmd files in the console's
# code page (a name like "José" in a path would break), and "call" would expand % in a path a second time.
WINDOWS_UPDATE_SCRIPT = "\r\n".join([
    "@echo off",
    "title Updating RadarForge",
    "echo Waiting for RadarForge to close...",
    'powershell -NoProfile -NonInteractive -Command "Wait-Process -Id $env:RF_PID -Timeout 300 '
    '-ErrorAction SilentlyContinue; if (Get-Process -Id $env:RF_PID -ErrorAction SilentlyContinue) { exit 3 }"',
    'set "RF_WAIT=%errorlevel%"',
    'if "%RF_WAIT%"=="3" goto busy',
    'if not "%RF_WAIT%"=="0" timeout /t 8 /nobreak >nul',
    'pushd "%RF_DIR%"',
    "call install.bat",
    "popd",
    'start "" "%RF_PYW%" -m radarforge',
    "exit /b 0",
    ":busy",
    "echo RadarForge did not close, so it was not updated. Close it and use Help - Check for updates again.",
    "pause",
    "exit /b 1",
    "",
])


def windows_update_env(install_bat: Path, pid: int, pythonw: Path) -> dict:
    """What WINDOWS_UPDATE_SCRIPT reads: the Windows folder holding install.bat, the process to wait for, pythonw."""
    return {"RF_DIR": str(Path(install_bat).parent), "RF_PID": str(int(pid)), "RF_PYW": str(pythonw),
            "RF_UPDATE": "1"}


def windows_script_command(script: Path) -> str:
    """cmd.exe running [script]: /d skips AutoRun scripts, /s keeps the quoted path intact (& ^ ( ) in names)."""
    return f'cmd.exe /d /s /c ""{script}""'


def venv_pythonw() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def start_install_sh(script: Path, log: Path, rc: Path) -> subprocess.Popen:
    """Runs install.sh in its own session (it finishes even if RadarForge closes), output to [log]; its exit
    code is written to [rc] when it's done."""
    for p in (log, rc):
        _unlink(p)
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(["bash", "-c", 'bash "$1" > "$2" 2>&1; echo $? > "$3.tmp"; mv -f "$3.tmp" "$3"',
                             "radarforge-update", str(script), str(log), str(rc)],
                            cwd=str(Path(script).parent), env=installer_env(), start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True)


# --------------------------------------------------------------------------- AppImage
def appimage_temp(target: Path) -> Path:
    """Where the new AppImage is downloaded: beside the old one, so it can be swapped in atomically."""
    return Path(target).with_name("." + Path(target).name + ".new")


def clear_appimage_temp(target: Path):
    """Removes a half-downloaded or not-swapped-in AppImage left beside [target] by an earlier run."""
    new = appimage_temp(target)
    for p in (new, new.with_name(new.name + ".part")):
        _unlink(p)


def appimage_writable(target: Path) -> bool:
    return os.access(Path(target).parent, os.W_OK | os.X_OK)


def replace_appimage(new: Path, target: Path):
    """Makes [new] executable and swaps it in for [target]. The running copy keeps working (it holds the old file)."""
    new, target = Path(new), Path(target)
    os.chmod(new, new.stat().st_mode | 0o755)
    os.replace(new, target)


# --------------------------------------------------------------------------- Windows installer
def setup_command(setup: Path, log: Path | None = None, pid: int | None = None) -> list:
    """Runs the Inno Setup installer with a progress window only. /WAITPID makes it wait until this process has
    exited (radarforge.iss, InitializeSetup); /RELAUNCH=1 makes it start RadarForge again when it's done."""
    cmd = [str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
           f"/WAITPID={int(pid if pid is not None else os.getpid())}", "/RELAUNCH=1"]
    if log is not None:
        cmd.append(f"/LOG={log}")
    return cmd


# --------------------------------------------------------------------------- starting things after RadarForge closes
_PENDING: list = []           # (args, Popen keyword arguments) started once the window has closed
_START_ENV: dict | None = None


def remember_environment():
    """Called first thing by app.main: the environment before the OpenGL set-up changes it."""
    global _START_ENV
    _START_ENV = dict(os.environ)


def clean_env() -> dict:
    """The environment RadarForge was started with, without what the AppImage runtime, PyInstaller and the
    OpenGL set-up added (variables the user set themselves, e.g. for the graphics card, are kept)."""
    env = dict(_START_ENV if _START_ENV is not None else os.environ)
    for k in ("APPIMAGE", "APPDIR", "ARGV0", "OWD", "RADARFORGE_GL_ATTEMPT"):
        env.pop(k, None)
    if _START_ENV is None:            # no snapshot: drop what the OpenGL set-up may have added
        try:
            from .gl_setup import PLATFORMS
            for _k, _l, penv in PLATFORMS:
                for var in penv:
                    env.pop(var, None)
        except Exception:
            pass
        env.pop("PYOPENGL_PLATFORM", None)
    # PyInstaller's bootloader marks its own child processes with these; a program started from here (the new
    # AppImage, or RadarForge.exe started again by the Windows setup) must start as a fresh program instead
    for k in [k for k in env if k.startswith("_PYI_") or k == "_MEIPASS2"]:
        env.pop(k)
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        own = [p for p in (getattr(sys, "_MEIPASS", ""), os.environ.get("APPDIR", "")) if p]
        for var in ("LD_LIBRARY_PATH",):
            orig = env.pop(var + "_ORIG", None)
            if orig is not None:
                env[var] = orig
            elif var in env:
                keep = [p for p in env[var].split(os.pathsep) if p and not any(p.startswith(o) for o in own)]
                if keep:
                    env[var] = os.pathsep.join(keep)
                else:
                    env.pop(var)
    return env


def relaunch_args(install: Install) -> list:
    """The command that starts this program again (keeping the graphics options it was started with)."""
    flags = [a for a in sys.argv[1:] if a in ("--x11", "--software", "--safe-graphics", "--no-live")]
    if install.kind == "appimage" and install.path is not None:
        return [str(install.path)] + flags
    if getattr(sys, "frozen", False):
        return [sys.executable] + flags
    return [sys.executable, "-m", "radarforge"] + flags


def run_after_exit(args, console: bool = False, extra_env: dict | None = None):
    """Starts [args] (a list, or on Windows a command line) once RadarForge's window has closed and its settings
    are saved (see app.main). [console]: a new console window (Windows), with the installer environment."""
    kw = {"env": clean_env() if not console else installer_env(), "close_fds": True}
    if extra_env:
        kw["env"].update(extra_env)
    if sys.platform == "win32":
        flags = 0x00000010 if console else (0x00000008 | 0x00000200)   # NEW_CONSOLE | DETACHED + NEW_GROUP
        kw["creationflags"] = flags
    else:
        kw["start_new_session"] = True
        kw["stdin"] = kw["stdout"] = kw["stderr"] = subprocess.DEVNULL
    _PENDING.append((args if isinstance(args, str) else list(args), kw))


def pending() -> list:
    return list(_PENDING)


def start_pending() -> list:
    """Called by app.main after the window has closed. Returns (command, error) for anything that didn't start."""
    failed = []
    if not _PENDING:
        return failed
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:                      # PyInstaller's DLL folder must not leak into the programs started here
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        except Exception:
            pass
    while _PENDING:
        args, kw = _PENDING.pop(0)
        try:
            subprocess.Popen(args, **kw)
            print("RadarForge: started", args if isinstance(args, str) else args[0], file=sys.stderr, flush=True)
        except OSError as exc:
            print("RadarForge: could not start", args, exc, file=sys.stderr, flush=True)
            failed.append((args, str(exc)))
    return failed
