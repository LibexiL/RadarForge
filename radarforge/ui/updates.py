"""Update check in the main window (status-bar button, Help → Check for updates…) and the update dialog."""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QTextBrowser, QToolButton, QVBoxLayout)

from .. import __version__, updater

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class _Relay(QObject):
    checked = Signal(bool, object, str)        # manual, releases (or None), error
    progress = Signal(object, object)          # bytes done, total
    downloaded = Signal(object, str)           # path (or None), error ("cancelled" when stopped)


def _downloads_dir() -> Path:
    d = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
    p = Path(d) if d else Path.home()
    return p if p.is_dir() else Path.home()


def _mb(n) -> str:
    return f"{(n or 0) / 1e6:.1f} MB"


class UpdatesMixin:
    """Main-window part: automatic checks, the status-bar button, quitting so an update can install."""

    def _init_updates(self):
        self.install_info = updater.detect_install()
        self._upd = _Relay(self)
        self._upd.checked.connect(self._update_checked)
        self._upd_checking = False
        self._upd_releases = None            # releases newer than this one, from the last check
        self._upd_installed = ""             # a version installed in this session (it runs after a restart)
        self._upd_dialog = None
        b = QToolButton()
        b.setProperty("role", "update")
        b.setCursor(Qt.PointingHandCursor)
        b.setVisible(False)
        b.clicked.connect(lambda: self.show_update_dialog())
        self.statusBar().addPermanentWidget(b)
        self.update_btn = b
        updater.clear_old_downloads()
        if self.install_info.kind == "appimage":
            updater.clear_appimage_temp(self.install_info.path)
        latest = self.settings["update_latest"] or ""
        if self._update_wanted(latest):
            self._show_update_btn(latest)
        if not os.environ.get(updater.ENV_OFF):
            QTimer.singleShot(20_000, self._auto_update_check)
            self._upd_timer = QTimer(self)
            self._upd_timer.timeout.connect(self._auto_update_check)
            self._upd_timer.start(15 * 60_000)       # cheap when not due; keeps the hour-later retry on time

    def _update_wanted(self, version) -> bool:
        return bool(version) and updater.is_newer(version) and version != (self.settings["update_skip"] or "")

    def _show_update_btn(self, version):
        self.update_btn.setText(f"Update to {version}")
        self.update_btn.setToolTip(f"RadarForge {version} is available (you have {__version__}). "
                                   "Click to see what's new and install it.")
        self.update_btn.setVisible(True)

    def _auto_update_check(self):
        if not self.settings["update_check"] or os.environ.get(updater.ENV_OFF):
            return
        if time.time() - float(self.settings["update_last_check"] or 0) < updater.CHECK_EVERY_S:
            return
        self.check_for_updates(manual=False)

    def check_for_updates(self, manual=True):
        if self._upd_checking:
            return
        self._upd_checking = True
        relay = self._upd

        def work():
            try:
                rel, err = updater.fetch_releases(), ""
            except updater.UpdateError as exc:
                rel, err = None, str(exc)
            except Exception as exc:              # noqa: BLE001 - shown to the user
                rel, err = None, f"{type(exc).__name__}: {exc}"
            try:
                relay.checked.emit(manual, rel, err)
            except RuntimeError:                  # window already gone
                pass
        threading.Thread(target=work, name="rf-update-check", daemon=True).start()

    def _update_checked(self, manual, releases, err):
        self._upd_checking = False
        d = self._upd_dialog
        waiting_dialog = d is not None and d.busy == "check"      # only a dialog that asked takes the answer
        if err:
            print("update check:", err)
            if waiting_dialog:
                d.check_failed(err)
            return
        newer = updater.newer_than(releases)
        self._upd_releases = newer
        latest = newer[0].version if newer else ""
        waiting = bool(newer) and updater.plan(self.install_info, newer[0]).waiting
        s = self.settings
        s["update_latest"] = "" if waiting else latest
        # the release's installer is still being built: look again in an hour instead of tomorrow
        s["update_last_check"] = time.time() - ((updater.CHECK_EVERY_S - updater.RETRY_S) if waiting else 0)
        s.save()
        if newer and not waiting and self._update_wanted(latest) and not self._upd_installed:
            if not self.update_btn.isVisible() and not manual:
                self._status_msg(f"RadarForge {latest} is available: click “Update to {latest}” in the status bar.")
            self._show_update_btn(latest)
        else:
            self.update_btn.setVisible(False)
        if waiting_dialog:
            d.set_releases(newer)

    def show_update_dialog(self, check=False):
        d = self._upd_dialog
        if d is None:
            d = self._upd_dialog = UpdateDialog(self)
        d.auto.blockSignals(True)
        d.auto.setChecked(bool(self.settings["update_check"]))
        d.auto.blockSignals(False)
        if not d.busy:
            if self._upd_installed:
                d.show_restart(self._upd_installed)
            elif check or self._upd_releases is None:
                d.start_check()
            else:
                d.set_releases(self._upd_releases)
        d.show()
        d.raise_()
        d.activateWindow()

    def skip_update(self, version):
        self.settings["update_skip"] = version
        self.settings.save()
        self.update_btn.setVisible(False)

    def _update_blocks_close(self) -> bool:
        """True if the user chose not to quit because install.sh is still running (asked from closeEvent)."""
        d = self._upd_dialog
        if d is None or not d.installer_running():
            return False
        ans = QMessageBox.question(
            self, "RadarForge update", "The update is still installing. It carries on if RadarForge closes, but "
            "wait until it has finished (a few minutes) before starting RadarForge again.\n\nQuit anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return ans != QMessageBox.Yes

    def quit_for_update(self):
        """Closes RadarForge (saving everything) so what run_after_exit queued can start."""
        self._status_msg("Closing RadarForge for the update…")
        self.close()
        QApplication.instance().quit()


class UpdateDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("RadarForge updates")
        self.resize(660, 580)
        self.release = None
        self.newer = []
        self.plan = None
        self.busy = None                 # "check" | "download" | "install"
        self.proc = None
        self._cancel = False
        self._primary_fn = None
        self._done_path = None
        self.relay = _Relay(self)
        self.relay.progress.connect(self._progress)
        self.relay.downloaded.connect(self._downloaded)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        self.title = QLabel()
        self.title.setProperty("role", "title")
        self.sub = QLabel()
        self.sub.setProperty("role", "hint")
        self.sub.setWordWrap(True)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.plan_lbl = QLabel()
        self.plan_lbl.setProperty("role", "card")
        self.plan_lbl.setWordWrap(True)
        self.plan_lbl.setTextFormat(Qt.RichText)
        self.plan_lbl.setOpenExternalLinks(True)
        self.plan_lbl.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.log.setVisible(False)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.auto = QCheckBox("Check for updates automatically (about once a day)")
        self.auto.setChecked(bool(main.settings["update_check"]))
        self.auto.toggled.connect(self._auto_toggled)
        for wdg in (self.title, self.sub):
            lay.addWidget(wdg)
        lay.addWidget(self.notes, 1)
        lay.addWidget(self.log, 1)
        lay.addWidget(self.plan_lbl)
        lay.addWidget(self.bar)
        lay.addWidget(self.auto)
        row = QHBoxLayout()
        self.page_btn = QPushButton("Release page")
        self.page_btn.clicked.connect(self._open_page)
        row.addWidget(self.page_btn)
        row.addStretch(1)
        self.skip_btn = QPushButton("Skip this version")
        self.skip_btn.clicked.connect(self._skip)
        self.close_btn = QPushButton("Later")
        self.close_btn.clicked.connect(self.close)
        self.primary = QPushButton()
        self.primary.clicked.connect(lambda: self._primary_fn and self._primary_fn())
        for b in (self.skip_btn, self.close_btn, self.primary):
            row.addWidget(b)
        lay.addLayout(row)

    # ------------------------------------------------------------------ states
    def _buttons(self, primary=None, fn=None, skip=False, close="Close"):
        self._primary_fn = fn
        self.primary.setVisible(bool(primary))
        if primary:
            self.primary.setText(primary)
            self.primary.setEnabled(True)
            self.primary.setDefault(True)
            self.primary.setAutoDefault(True)
        self.skip_btn.setVisible(skip)
        self.close_btn.setText(close)
        self.close_btn.setVisible(bool(close))

    def start_check(self):
        self.busy = "check"
        self.title.setText("Checking for updates…")
        self.sub.setText(f"You have RadarForge {__version__}.")
        self.notes.setVisible(False)
        self.log.setVisible(False)
        self.plan_lbl.setVisible(False)
        self.bar.setRange(0, 0)
        self.bar.setVisible(True)
        self._buttons(close="Close")
        self.main.check_for_updates(manual=True)

    def check_failed(self, err):
        if self.busy not in (None, "check"):
            return
        self.busy = None
        self.bar.setVisible(False)
        self.title.setText("Couldn't check for updates")
        self.sub.setText(err)
        self.notes.setVisible(False)
        self.plan_lbl.setVisible(False)
        self._buttons("Try again", self.start_check, close="Close")

    def set_releases(self, newer):
        if self.busy not in (None, "check"):
            return
        self.busy = None
        self.newer = list(newer or [])
        self.bar.setVisible(False)
        self.log.setVisible(False)
        if not self.newer:
            self.release = None
            self.title.setText("RadarForge is up to date")
            self.sub.setText(f"You have {__version__}, the newest version.")
            self.notes.setVisible(False)
            self.plan_lbl.setVisible(False)
            self._buttons(close="Close")
            return
        r = self.release = self.newer[0]
        self.title.setText(f"RadarForge {r.version} is available")
        sub = f"You have {__version__}."
        if len(self.newer) > 1:
            sub += f" There are {len(self.newer)} newer releases; what changed in each is below."
        if (self.main.settings["update_skip"] or "") == r.version:
            sub += " You chose to skip this version."
        self.sub.setText(sub)
        self.notes.setMarkdown(updater.combined_notes(self.newer))
        self.notes.setVisible(True)
        self.plan = updater.plan(self.main.install_info, r)
        self._plan_text(self.plan.text)
        if self.plan.button:
            self._buttons(self.plan.button, self._install, skip=True, close="Later")
        elif self.plan.waiting:
            self._buttons("Check again", self.start_check, skip=True, close="Later")
        else:
            self._buttons("Open the release page", self._open_page, skip=True, close="Later")

    def _plan_text(self, html, error=False):
        self.plan_lbl.setText(f"<span style='color:#ff6b6b'>{html}</span>" if error else html)
        self.plan_lbl.setVisible(True)

    # ------------------------------------------------------------------ actions
    def _auto_toggled(self, on):
        self.main.settings["update_check"] = bool(on)
        self.main.settings.save()

    def _open_page(self):
        QDesktopServices.openUrl(QUrl(self.release.page if self.release else updater.RELEASES_PAGE))

    def _skip(self):
        if self.release is not None:
            self.main.skip_update(self.release.version)
        self.close()

    def _install(self):
        inst, a = self.main.install_info, self.plan.asset
        self._fallback = False
        if inst.kind == "appimage":
            if updater.appimage_writable(inst.path):
                dest = updater.appimage_temp(inst.path)
            else:                          # e.g. /opt: save it where the user can move it themselves
                dest = _downloads_dir() / a.name
                self._fallback = True
        elif inst.kind == "portable":
            dest = _downloads_dir() / a.name
        else:
            dest = updater.updates_dir() / a.name
        self.busy = "download"
        self._cancel = False
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFormat("Starting the download…")
        self.bar.setVisible(True)
        self._plan_text(f"Downloading {a.name}…")
        self._buttons("Cancel", self._cancel_download, close="")
        relay = self.relay

        def emit(sig, *args):
            try:
                sig.emit(*args)
            except RuntimeError:
                pass

        def work():
            try:
                updater.download(a, dest, progress=lambda d, t: emit(relay.progress, d, t),
                                 cancelled=lambda: self._cancel)
                emit(relay.downloaded, str(dest), "")
            except updater.Cancelled:
                emit(relay.downloaded, None, "cancelled")
            except updater.UpdateError as exc:
                emit(relay.downloaded, None, str(exc))
            except Exception as exc:              # noqa: BLE001 - shown to the user
                emit(relay.downloaded, None, f"{type(exc).__name__}: {exc}")
        threading.Thread(target=work, name="rf-update-download", daemon=True).start()

    def _cancel_download(self):
        self._cancel = True
        self.primary.setEnabled(False)
        self.bar.setFormat("Stopping…")

    def _progress(self, done, total):
        if total:
            self.bar.setValue(int(1000 * min(done, total) / total))
            self.bar.setFormat(f"{_mb(done)} of {_mb(total)}")
        else:
            self.bar.setFormat(_mb(done))

    def _downloaded(self, path, err):
        self.busy = None
        self.bar.setVisible(False)
        if path and self._cancel:            # finished just as the user cancelled or closed the window
            if self.main.install_info.kind == "appimage":
                updater._unlink(Path(path))
            err = "cancelled"
        if err == "cancelled":
            self.set_releases(self.newer)
            return
        if err:
            self._plan_text(f"Couldn't download the update: {err}", error=True)
            self._buttons("Try again", self._install, skip=True, close="Later")
            return
        try:
            self._apply(Path(path))
        except Exception as exc:              # noqa: BLE001 - anything here is shown, never left half-done
            self.busy = None
            self._plan_text(f"Couldn't install the update: {exc}", error=True)
            self._buttons("Try again", self._install, skip=True, close="Later")

    def _apply(self, path: Path):
        inst, r = self.main.install_info, self.release
        kind = inst.kind
        if kind == "windows-setup":
            updater.run_after_exit(updater.setup_command(path, updater.updates_dir() / "setup.log", os.getpid()))
            self._closing_for_update()
        elif kind == "appimage":
            if self._fallback:
                self._done_path = path
                self._plan_text(f"Saved <b>{path.name}</b> to {path.parent}. RadarForge can't write to "
                                f"{inst.path.parent}, so move the new file there yourself (in place of "
                                f"{inst.path.name}).")
                self._buttons("Show in folder", self._show_folder, close="Close")
                return
            updater.replace_appimage(path, inst.path)
            self._ready_to_restart(f"RadarForge {r.version} is installed ({inst.path.name} was replaced).")
        elif kind == "linux-venv":
            program = self._unpack(path)
            self._run_installer(program)
        elif kind == "windows-venv":
            program = self._unpack(path)
            bat = program / "Windows" / "install.bat"
            if not bat.is_file():
                raise updater.UpdateError("the download has no Windows/install.bat")
            script = updater.updates_dir() / "update.cmd"
            script.write_text(updater.WINDOWS_UPDATE_SCRIPT, encoding="ascii", newline="")
            updater.run_after_exit(updater.windows_script_command(script), console=True,
                                   extra_env=updater.windows_update_env(bat, os.getpid(), updater.venv_pythonw()))
            self._closing_for_update()
        else:                                      # portable: the ZIP is in Downloads
            self._done_path = path
            self._plan_text(f"Saved <b>{path.name}</b> to {path.parent}. Extract it and start RadarForge from the "
                            "new folder as before. Your settings are kept.")
            self._buttons("Show in folder", self._show_folder, close="Close")

    def _unpack(self, zip_path: Path) -> Path:
        dest = updater.updates_dir() / f"RadarForge-{self.release.version}"
        root = updater.extract_zip(zip_path, dest)
        program = updater.find_program(root)
        if program is None:
            raise updater.UpdateError("the download doesn't contain RadarForge's program files")
        got = updater.program_version(program)
        if got and got != self.release.version:
            raise updater.UpdateError(f"the download is version {got}, not {self.release.version}")
        return program

    def _closing_for_update(self):
        self.busy = "install"
        self._plan_text("RadarForge closes now so the update can install. It starts again by itself.")
        self._buttons(close="")
        QTimer.singleShot(900, self.main.quit_for_update)

    def _ready_to_restart(self, text):
        self.busy = None
        self._plan_text(text + " Restart RadarForge to use it.")
        self._buttons("Restart now", self._restart, close="Later")
        self.main._upd_installed = self.release.version
        self.main.update_btn.setVisible(False)
        self.main.settings["update_latest"] = ""
        self.main.settings.save()

    def show_restart(self, version):
        """Reopened after an update was installed: it only needs the restart."""
        self.title.setText(f"RadarForge {version} is installed")
        self.sub.setText(f"This window is still running {__version__}.")
        self.notes.setVisible(False)
        self.log.setVisible(False)
        self.bar.setVisible(False)
        self._plan_text("Restart RadarForge to use the new version.")
        self._buttons("Restart now", self._restart, close="Later")

    def _restart(self):
        updater.run_after_exit(updater.relaunch_args(self.main.install_info))
        self.main.quit_for_update()

    def _show_folder(self):
        if self._done_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._done_path.parent)))

    # ------------------------------------------------------------------ Linux install.sh
    def _run_installer(self, program: Path):
        """install.sh runs in its own session with its output in a log file that is shown here as it grows,
        so closing RadarForge meanwhile doesn't cut it off halfway."""
        script = program / "Linux" / "install.sh"
        if not script.is_file():
            raise updater.UpdateError("the download has no Linux/install.sh")
        self.busy = "install"
        self.notes.setVisible(False)
        self.log.clear()
        self.log.setVisible(True)
        self.bar.setRange(0, 0)
        self.bar.setFormat("")
        self.bar.setVisible(True)
        self._plan_text(f"Installing RadarForge {self.release.version}… This takes a few minutes. Restart "
                        "RadarForge when it's done.")
        self._buttons(close="")
        d = updater.updates_dir()
        self._inst_log, self._inst_rc = d / "install.log", d / "install.rc"
        self._inst_pos = 0
        self._inst_tail = ""
        self.proc = updater.start_install_sh(script, self._inst_log, self._inst_rc)
        self._inst_timer = QTimer(self)
        self._inst_timer.timeout.connect(self._installer_poll)
        self._inst_timer.start(250)

    def installer_running(self) -> bool:
        return self.busy == "install" and self.proc is not None and self.proc.poll() is None

    def _installer_poll(self):
        try:
            with open(self._inst_log, "rb") as fh:
                fh.seek(self._inst_pos)
                data = fh.read()
                self._inst_pos += len(data)
        except OSError:
            data = b""
        if data:
            text = self._inst_tail + ANSI.sub("", data.decode("utf-8", "replace")).replace("\r\n", "\n")
            lines = text.split("\n")
            self._inst_tail = lines.pop()                 # an unfinished line waits for the rest
            for line in lines:
                if line.strip():
                    self.log.appendPlainText(line)
        code = None
        try:
            code = int(self._inst_rc.read_text().strip())
        except (OSError, ValueError):
            if self.proc.poll() is not None:              # bash itself went away without writing the code
                code = self.proc.returncode or -1
        if code is not None:
            self._inst_timer.stop()
            if self._inst_tail.strip():
                self.log.appendPlainText(self._inst_tail)
            self._installer_finished(code)

    def _installer_finished(self, code):
        if self.busy != "install":
            return
        self.busy = None
        self.bar.setVisible(False)
        if code == 0:
            self._ready_to_restart(f"RadarForge {self.release.version} is installed.")
        else:
            self._plan_text(f"The installer stopped with an error (see above). RadarForge {__version__} still "
                            "works; try again, or run install.sh from the new version by hand.", error=True)
            self._buttons("Try again", self._install, skip=True, close="Close")

    # ------------------------------------------------------------------ closing
    def reject(self):                  # Esc, the window's close button and "Later" all end up here
        if self.busy == "install":
            QMessageBox.information(self, "RadarForge updates", "The update is still installing; "
                                                                 "this window can close when it's done.")
            return
        if self.busy == "download":
            self._cancel = True        # the download stops; reopening shows the release again
        super().reject()
