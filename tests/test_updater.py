"""Update check and installer pieces: versions, release parsing, install detection, downloads, ZIPs, AppImage swap."""
import hashlib
import http.server
import os
import stat
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from radarforge import updater
from radarforge.updater import Asset, Install


def _gh_release(tag, assets=(), draft=False, pre=False, body="notes"):
    return {"tag_name": tag, "name": f"RadarForge {tag.lstrip('v')}", "draft": draft, "prerelease": pre,
            "body": body, "html_url": f"https://github.com/LibexiL/RadarForge/releases/tag/{tag}",
            "published_at": "2026-10-05T00:00:00Z",
            "zipball_url": f"https://api.github.com/repos/LibexiL/RadarForge/zipball/{tag}",
            "assets": [{"name": n, "size": sz, "digest": "sha256:" + "0" * 64,
                        "browser_download_url": f"https://github.com/LibexiL/RadarForge/releases/download/{tag}/{n}"}
                       for n, sz in assets]}


FULL = [("RadarForge-{v}.zip", 9_000_000), ("RadarForge-Setup-{v}.exe", 122_000_000),
        ("RadarForge-{v}-x86_64.AppImage", 204_000_000)]


def _full(tag):
    v = tag.lstrip("v")
    return _gh_release(tag, [(n.format(v=v), s) for n, s in FULL])


# ----------------------------------------------------------------------------- versions and releases
def test_versions():
    assert updater.parse_version("v1.10.0") == (1, 10, 0, 0)
    assert updater.parse_version("1.10") == (1, 10, 0, 0)
    assert updater.parse_version("1.11.0-beta") is None
    assert updater.parse_version("latest") is None
    assert updater.is_newer("1.10.1", "1.10.0")
    assert updater.is_newer("v2.0", "1.99.99")
    assert updater.is_newer("1.10.0", "1.9.3")          # numeric, not text, comparison
    assert not updater.is_newer("1.10.0", "1.10.0")
    assert not updater.is_newer("1.9.9", "1.10.0")
    assert not updater.is_newer("junk", "1.0.0")


def test_parse_releases_skips_drafts_and_prereleases():
    data = [_full("v1.9.3"), _full("v1.11.0"), _gh_release("v1.12.0", draft=True),
            _gh_release("v1.12.0-rc1", pre=True), _gh_release("nightly"), _full("v1.10.0"), "junk"]
    rels = updater.parse_releases(data)
    assert [r.version for r in rels] == ["1.11.0", "1.10.0", "1.9.3"]
    r = rels[0]
    assert r.tag == "v1.11.0" and r.title == "RadarForge 1.11.0"
    assert r.asset(r"RadarForge-Setup-.*\.exe").size == 122_000_000
    assert [x.version for x in updater.newer_than(rels, "1.10.0")] == ["1.11.0"]
    assert updater.newer_than(rels, "1.11.0") == []
    assert updater.parse_releases({"message": "rate limited"}) == []


def test_combined_notes():
    rels = updater.parse_releases([_gh_release("v1.12.0", body="## 1.12.0 – 2026-11-01\n\n- new"),
                                   _gh_release("v1.11.0", body="- plain list")])
    md = updater.combined_notes(rels)
    assert md.index("## 1.12.0") < md.index("## RadarForge 1.11.0") < md.index("- plain list")
    many = updater.parse_releases([_gh_release(f"v1.{n}.0") for n in range(20, 30)])
    assert "4 older releases" in updater.combined_notes(many, limit=6)


# ----------------------------------------------------------------------------- what to install
def test_assets_must_match_the_release_version():
    """A setup built from an un-bumped version would reinstall the old version forever."""
    rel = updater.parse_releases([_gh_release("v1.11.0", [("RadarForge-Setup-1.10.0.exe", 1),
                                                          ("RadarForge-1.10.0-x86_64.AppImage", 1),
                                                          ("RadarForge-1.10.0.zip", 1)])])[0]
    assert updater.pick_asset(rel, "windows-setup") is None
    assert updater.pick_asset(rel, "appimage", machine="x86_64") is None
    assert updater.pick_asset(rel, "linux-venv").url.endswith("/zipball/v1.11.0")


def test_pick_asset_and_plan():
    rel = updater.parse_releases([_full("v1.11.0")])[0]
    assert updater.pick_asset(rel, "windows-setup").name == "RadarForge-Setup-1.11.0.exe"
    assert updater.pick_asset(rel, "appimage", machine="x86_64").name == "RadarForge-1.11.0-x86_64.AppImage"
    assert updater.pick_asset(rel, "appimage", machine="aarch64") is None
    for kind in updater.SOURCE_KINDS:
        assert updater.pick_asset(rel, kind).name == "RadarForge-1.11.0.zip"
    assert updater.pick_asset(rel, "git") is None
    win = updater.plan(Install("windows-setup"), rel)
    assert win.button == "Install and restart" and "122 MB" in win.text
    app = updater.plan(Install("appimage", Path("/home/u/Apps/radarforge.appimage")), rel, machine="x86_64")
    assert app.button and "radarforge.appimage" in app.text
    assert updater.plan(Install("linux-venv"), rel).button == "Install update"
    assert updater.plan(Install("windows-venv"), rel).button == "Install and restart"
    assert updater.plan(Install("portable"), rel).button == "Download"
    git = updater.plan(Install("git", Path("/src/RadarForge")), rel)
    assert not git.button and "git pull" in git.text
    assert not updater.plan(Install("manual"), rel).button
    assert not updater.plan(Install("appimage", Path("/x.AppImage")), rel, machine="aarch64").button


def test_plan_waits_for_ci_builds():
    """Right after a release only the ZIP is there; the installer and AppImage come a few minutes later."""
    rel = updater.parse_releases([_gh_release("v1.11.0", [("RadarForge-1.11.0.zip", 9_000_000)])])[0]
    p = updater.plan(Install("windows-setup"), rel)
    assert p.waiting and not p.button and "Windows installer" in p.text
    assert updater.plan(Install("appimage", Path("/a.AppImage")), rel, machine="x86_64").waiting
    assert updater.plan(Install("linux-venv"), rel).button          # source installs only need the ZIP
    # no ZIP attached at all: GitHub's own archive of the tag works for source installs
    bare = updater.parse_releases([_gh_release("v1.11.0")])[0]
    a = updater.pick_asset(bare, "linux-venv")
    assert a.url.endswith("/zipball/v1.11.0") and a.name == "RadarForge-1.11.0-source.zip"


# ----------------------------------------------------------------------------- install detection
def test_detect_install(tmp_path):
    home = tmp_path / "home"
    pkg = tmp_path / "site" / "radarforge"
    pkg.mkdir(parents=True)
    env = {"HOME": str(home)}
    # AppImage: the packaged program running from the AppImage's mount ($APPDIR)
    ai = tmp_path / "RadarForge-1.10.0-x86_64.AppImage"
    ai.write_bytes(b"x")
    mount = tmp_path / ".mount_RadarAbc"
    (mount / "usr" / "lib" / "radarforge").mkdir(parents=True)
    exe = mount / "usr" / "lib" / "radarforge" / "RadarForge"
    exe.write_bytes(b"")
    aienv = {**env, "APPIMAGE": str(ai), "APPDIR": str(mount)}
    got = updater.detect_install(env=aienv, frozen=True, plat="linux", executable=exe, package_dir=pkg)
    assert got.kind == "appimage" and got.path == ai
    # $APPIMAGE pointing nowhere is ignored
    assert updater.detect_install(env={**aienv, "APPIMAGE": str(tmp_path / "gone")}, frozen=True, plat="linux",
                                  executable=exe, package_dir=pkg).kind == "manual"
    # ... and so is one inherited from another AppImage (a terminal or editor started from an AppImage)
    other = tmp_path / "Kate-24.08-x86_64.AppImage"
    other.write_bytes(b"x")
    kate = {**env, "APPIMAGE": str(other), "APPDIR": str(tmp_path / ".mount_Kate")}
    assert updater.detect_install(env=kate, frozen=True, plat="linux", executable=tmp_path / "dist" / "RadarForge",
                                  package_dir=pkg).kind == "manual"
    venv0 = home / ".local" / "share" / "radarforge" / "venv"
    venv0.mkdir(parents=True)
    assert updater.detect_install(env=kate, frozen=False, plat="linux", prefix=venv0,
                                  package_dir=pkg).kind == "linux-venv"
    # the Windows installer leaves its uninstaller beside RadarForge.exe
    app = tmp_path / "Programs" / "RadarForge"
    app.mkdir(parents=True)
    (app / "RadarForge.exe").write_bytes(b"")
    assert updater.detect_install(env=env, frozen=True, plat="win32", executable=app / "RadarForge.exe",
                                  package_dir=pkg).kind == "manual"
    (app / "unins000.exe").write_bytes(b"")
    assert updater.detect_install(env=env, frozen=True, plat="win32", executable=app / "RadarForge.exe",
                                  package_dir=pkg).kind == "windows-setup"
    # install.sh: ~/.local/share/radarforge/venv (or $XDG_DATA_HOME)
    venv = home / ".local" / "share" / "radarforge" / "venv"
    assert updater.detect_install(env=env, frozen=False, plat="linux", prefix=venv, package_dir=pkg).kind == "linux-venv"
    xdg = tmp_path / "xdg"
    (xdg / "radarforge" / "venv").mkdir(parents=True)
    assert updater.detect_install(env={**env, "XDG_DATA_HOME": str(xdg)}, frozen=False, plat="linux",
                                  prefix=xdg / "radarforge" / "venv", package_dir=pkg).kind == "linux-venv"
    # install.bat: %LOCALAPPDATA%\RadarForge\venv
    local = tmp_path / "Local"
    (local / "RadarForge" / "venv").mkdir(parents=True)
    assert updater.detect_install(env={**env, "LOCALAPPDATA": str(local)}, frozen=False, plat="win32",
                                  prefix=local / "RadarForge" / "venv", package_dir=pkg).kind == "windows-venv"
    # run.sh / run.bat from the extracted download
    dl = tmp_path / "Downloads" / "RadarForge"
    (dl / "radarforge").mkdir(parents=True)
    (dl / "Linux").mkdir()
    (dl / "Linux" / "run.sh").write_text("")
    other = tmp_path / "other-venv"
    assert updater.detect_install(env=env, frozen=False, plat="linux", prefix=other,
                                  package_dir=dl / "radarforge").kind == "portable"
    # a git checkout wins over the folder look
    (dl / ".git").mkdir()
    assert updater.detect_install(env=env, frozen=False, plat="linux", prefix=other,
                                  package_dir=dl / "radarforge").kind == "git"
    # pip install somewhere else
    assert updater.detect_install(env=env, frozen=False, plat="linux", prefix=other, package_dir=pkg).kind == "manual"


# ----------------------------------------------------------------------------- downloads
class _Files(http.server.BaseHTTPRequestHandler):
    files = {}

    def do_GET(self):
        body = self.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for i in range(0, len(body), 65536):
            self.wfile.write(body[i:i + 65536])

    def log_message(self, *a):
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):      # a cancelled download drops the connection
        pass


@pytest.fixture
def server(monkeypatch):
    srv = _QuietServer(("127.0.0.1", 0), _Files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv(updater.ENV_API, base + "/releases")
    yield base
    srv.shutdown()


def test_download_checks_size_and_digest(server, tmp_path):
    data = os.urandom(700_000)
    _Files.files = {"/a.bin": data}
    good = Asset("a.bin", server + "/a.bin", len(data), "sha256:" + hashlib.sha256(data).hexdigest())
    seen = []
    out = updater.download(good, tmp_path / "a.bin", progress=lambda d, t: seen.append((d, t)))
    assert out.read_bytes() == data and seen[-1] == (len(data), len(data))
    bad = Asset("a.bin", server + "/a.bin", len(data), "sha256:" + "1" * 64)
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.download(bad, tmp_path / "b.bin")
    assert not (tmp_path / "b.bin").exists() and not (tmp_path / "b.bin.part").exists()
    short = Asset("a.bin", server + "/a.bin", len(data) + 5)
    with pytest.raises(updater.UpdateError, match="stopped early"):
        updater.download(short, tmp_path / "c.bin")
    with pytest.raises(updater.UpdateError, match="HTTP 404"):
        updater.download(Asset("x", server + "/missing"), tmp_path / "d.bin")
    with pytest.raises(updater.Cancelled):
        updater.download(good, tmp_path / "e.bin", cancelled=lambda: True)
    assert not (tmp_path / "e.bin.part").exists()


def test_download_refuses_other_hosts(tmp_path, monkeypatch):
    monkeypatch.delenv(updater.ENV_API, raising=False)
    for url in ("https://evil.example/RadarForge-Setup-9.exe",
                "http://github.com/LibexiL/RadarForge/releases/download/v9/x.exe",
                "https://github.com/someone/RadarForge/releases/download/v9/x.exe",
                "https://github.com/LibexiL/RadarForge/../../evil/payload/releases/download/v9/x.exe",
                "https://github.com/LibexiL/RadarForge/releases/download/%2e%2e/%2e%2e/%2e%2e/evil/x.exe",
                "https://github.com/LibexiL/RadarForge/archive/x.exe",
                "https://github.com@evil.example/LibexiL/RadarForge/releases/download/v9/x.exe",
                "https://github.com:8443/LibexiL/RadarForge/releases/download/v9/x.exe",
                "https://api.github.com/repos/LibexiL/RadarForge/releases"):
        with pytest.raises(updater.UpdateError, match="unexpected"):
            updater.download(Asset("x", url), tmp_path / "x")
    assert updater._allowed("https://github.com/LibexiL/RadarForge/releases/download/v1.11.0/RadarForge-1.11.0.zip")
    assert updater._allowed("https://github.com/libexil/radarforge/releases/download/v1.11.0/RadarForge-1.11.0.zip")
    assert updater._allowed("https://api.github.com/repos/LibexiL/RadarForge/zipball/v1.11.0")


def test_test_hook_ignored_by_packaged_program(monkeypatch):
    monkeypatch.setenv(updater.ENV_API, "http://127.0.0.1:9/releases")
    assert updater._allowed("http://127.0.0.1:9/dl/x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert updater._test_api() is None and not updater._allowed("http://127.0.0.1:9/dl/x")


def test_fetch_releases_from_test_server(server):
    import json
    _Files.files = {"/releases": json.dumps([_full("v1.11.0"), _full("v1.10.0")]).encode()}
    rels = updater.fetch_releases()
    assert [r.version for r in rels] == ["1.11.0", "1.10.0"]


# ----------------------------------------------------------------------------- source installs
def _zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)


def test_extract_zip_and_find_program(tmp_path):
    z = tmp_path / "r.zip"
    _zip(z, {"RadarForge/radarforge/__init__.py": '__version__ = "1.11.0"\n', "RadarForge/pyproject.toml": "",
             "RadarForge/Linux/install.sh": "echo hi"})
    root = updater.extract_zip(z, tmp_path / "out")
    prog = updater.find_program(root)
    assert prog == root / "RadarForge" and updater.program_version(prog) == "1.11.0"
    # GitHub's own archive puts everything in "LibexiL-RadarForge-<sha>/"
    z2 = tmp_path / "gh.zip"
    _zip(z2, {"LibexiL-RadarForge-abc123/radarforge/__init__.py": '__version__ = "1.11.0"',
              "LibexiL-RadarForge-abc123/pyproject.toml": ""})
    assert updater.find_program(updater.extract_zip(z2, tmp_path / "out2")).name == "LibexiL-RadarForge-abc123"
    evil = tmp_path / "evil.zip"
    _zip(evil, {"../../outside.txt": "x"})
    with pytest.raises(updater.UpdateError, match="unsafe"):
        updater.extract_zip(evil, tmp_path / "out3")
    assert not (tmp_path / "outside.txt").exists()
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(updater.UpdateError, match="valid ZIP"):
        updater.extract_zip(junk, tmp_path / "out4")


def test_windows_update_script():
    s = updater.WINDOWS_UPDATE_SCRIPT
    s.encode("ascii")                                   # cmd.exe reads it in the console code page
    assert "\r\n" in s and "$env:RF_PID" in s
    assert s.index('pushd "%RF_DIR%"') < s.index("call install.bat") < s.index('start "" "%RF_PYW%" -m radarforge')
    assert 'if "%RF_WAIT%"=="3" goto busy' in s and ":busy" in s
    bat = Path("C:/Users/José & 100%/AppData/Local/RadarForge/cache/updates/R/RadarForge/Windows/install.bat")
    env = updater.windows_update_env(bat, 4321, Path("C:/Users/José & 100%/venv/Scripts/pythonw.exe"))
    assert env["RF_DIR"] == str(bat.parent) and env["RF_PID"] == "4321" and env["RF_UPDATE"] == "1"
    cmd = updater.windows_script_command(Path("C:/Users/A & B/update.cmd"))
    assert cmd.startswith("cmd.exe /d /s /c \"\"") and cmd.endswith("update.cmd\"\"")


def test_setup_command():
    cmd = updater.setup_command(Path("C:/t/RadarForge-Setup-1.11.0.exe"), Path("C:/t/setup.log"), pid=4321)
    assert cmd[0].endswith("RadarForge-Setup-1.11.0.exe")
    for flag in ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/WAITPID=4321", "/RELAUNCH=1"):
        assert flag in cmd
    assert any(a.startswith("/LOG=") for a in cmd)


def test_install_scripts_support_unattended_update():
    root = Path(__file__).resolve().parents[1]
    bat = (root / "Windows" / "install.bat").read_text()
    assert "if not defined RF_UPDATE pause" in bat
    iss = (root / "packaging" / "radarforge.iss").read_text()
    assert "RelaunchAfterUpdate" in iss and "{param:relaunch|0}" in iss and "[InstallDelete]" in iss
    assert "function InitializeSetup" in iss and "{param:waitpid|0}" in iss and "WaitForSingleObject" in iss


# ----------------------------------------------------------------------------- AppImage
@pytest.mark.skipif(sys.platform == "win32", reason="AppImages are Linux only")
def test_replace_appimage_while_running(tmp_path):
    target = tmp_path / "RadarForge.AppImage"
    target.write_bytes(b"old")
    os.chmod(target, 0o755)
    held = open(target, "rb")                       # the running copy keeps its file open
    new = updater.appimage_temp(target)
    assert new.parent == target.parent and new.name.startswith(".")
    new.write_bytes(b"new version")
    os.chmod(new, 0o644)
    assert updater.appimage_writable(target)
    updater.replace_appimage(new, target)
    assert target.read_bytes() == b"new version" and not new.exists()
    assert target.stat().st_mode & stat.S_IXUSR
    assert held.read() == b"old"                    # still readable by the old process
    held.close()


# ----------------------------------------------------------------------------- after the window closes
def test_clean_env_keeps_the_users_own_graphics_settings(monkeypatch):
    monkeypatch.setenv("__GLX_VENDOR_LIBRARY_NAME", "nvidia")      # e.g. KDE's "run on the dedicated GPU"
    monkeypatch.setattr(updater, "_START_ENV", None)
    updater.remember_environment()
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")                    # added later by the OpenGL set-up
    monkeypatch.setenv("RADARFORGE_GL_ATTEMPT", "1")
    env = updater.clean_env()
    assert env["__GLX_VENDOR_LIBRARY_NAME"] == "nvidia"
    assert env.get("QT_QPA_PLATFORM") == updater._START_ENV.get("QT_QPA_PLATFORM")   # as started, not "xcb"
    assert "RADARFORGE_GL_ATTEMPT" not in env
    monkeypatch.setattr(updater, "_START_ENV", None)


def test_clean_env_and_relaunch(monkeypatch, tmp_path):
    monkeypatch.setenv("APPIMAGE", "/x/RadarForge.AppImage")
    monkeypatch.setenv("APPDIR", "/tmp/.mount_x")
    monkeypatch.setenv("RADARFORGE_GL_ATTEMPT", "2")
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    env = updater.clean_env()
    for k in ("APPIMAGE", "APPDIR", "RADARFORGE_GL_ATTEMPT", "QT_QPA_PLATFORM"):
        assert k not in env
    monkeypatch.setattr(sys, "argv", ["radarforge", "--x11", "file.ar2v", "--site", "KTLX"])
    args = updater.relaunch_args(Install("appimage", Path("/x/RadarForge.AppImage")))
    assert args == [str(Path("/x/RadarForge.AppImage")), "--x11"]
    assert updater.relaunch_args(Install("linux-venv"))[:3] == [sys.executable, "-m", "radarforge"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX spawn check")
def test_start_pending_runs_after_exit(tmp_path):
    flag = tmp_path / "started"
    updater.run_after_exit([sys.executable, "-c", f"open({str(flag)!r}, 'w').write('ok')"])
    assert updater.pending() and not flag.exists()
    updater.start_pending()
    assert updater.pending() == []
    for _ in range(100):
        if flag.exists():
            break
        time.sleep(0.05)
    assert flag.read_text() == "ok"


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh is Linux only")
def test_install_sh_runs_detached_with_log_and_code(tmp_path):
    script = tmp_path / "Linux" / "install.sh"
    script.parent.mkdir()
    script.write_text('echo "in $(basename $PWD) RF_UPDATE=$RF_UPDATE"\necho oops >&2\nexit 4\n')
    log, rc = tmp_path / "u" / "install.log", tmp_path / "u" / "install.rc"
    p = updater.start_install_sh(script, log, rc)
    p.wait(20)
    for _ in range(100):
        if rc.exists():
            break
        time.sleep(0.05)
    assert rc.read_text().strip() == "4"
    text = log.read_text()
    assert "in Linux RF_UPDATE=1" in text and "oops" in text


@pytest.mark.skipif(sys.platform == "win32", reason="AppImages are Linux only")
def test_clear_appimage_temp(tmp_path):
    target = tmp_path / "R.AppImage"
    new = updater.appimage_temp(target)
    new.write_bytes(b"x")
    new.with_name(new.name + ".part").write_bytes(b"x")
    updater.clear_appimage_temp(target)
    assert not new.exists() and not new.with_name(new.name + ".part").exists()


def test_clear_old_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path)
    old = tmp_path / "RadarForge-1.9.0.zip"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    (tmp_path / "RadarForge-1.9.0").mkdir()
    os.utime(tmp_path / "RadarForge-1.9.0", (time.time() - 7200, time.time() - 7200))
    fresh = tmp_path / "RadarForge-Setup-1.11.0.exe"
    fresh.write_bytes(b"x")
    updater.clear_old_downloads()
    assert not old.exists() and not (tmp_path / "RadarForge-1.9.0").exists() and fresh.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="LD_LIBRARY_PATH is Linux only")
def test_clean_env_for_frozen_builds(monkeypatch):
    """Programs started from the packaged app must not look like PyInstaller children of it."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/.mount_abc/usr/lib/radarforge/_internal", raising=False)
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/tmp/.mount_abc/usr/lib/radarforge/RadarForge")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "-1")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_abc/usr/lib/radarforge/_internal:/opt/mine")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/mine")
    env = updater.clean_env()
    assert not [k for k in env if k.startswith("_PYI_")]
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert env["LD_LIBRARY_PATH"] == "/opt/mine" and "LD_LIBRARY_PATH_ORIG" not in env
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    env = updater.clean_env()                       # no saved value: drop only the bundle's own folder
    assert env["LD_LIBRARY_PATH"] == "/opt/mine"
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_abc/usr/lib/radarforge/_internal")
    assert "LD_LIBRARY_PATH" not in updater.clean_env()
