"""The main window: wires the pages to the engine through background jobs."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ptransfer import __version__
from ptransfer.authorize import Authorizer
from ptransfer.backup import BackupEngine, BackupOptions
from ptransfer.devices import Device, DeviceManager
from ptransfer.logging_setup import log_file
from ptransfer.mirror import ScrcpySession, choose_backend, explain_failure
from ptransfer.nokia import NokiaRescue
from ptransfer.platform_tools import discover, download_platform_tools, download_scrcpy
from ptransfer.progress import Event, Reporter
from ptransfer.recovery import RescueEngine, diagnose
from ptransfer.restore import RestoreEngine, RestoreOptions
from ptransfer.screen import PhoneScreen, ScreenUnavailable, scrcpy_path
from ptransfer.unlock import UnlockFlow
from ptransfer.transfer import TransferEngine, TransferOptions

from .pages.backup_page import BackupPage
from .pages.devices_page import DevicesPage
from .pages.guide_page import GuidePage
from .pages.rescue_page import RescuePage
from .pages.restore_page import RestorePage
from .pages.transfer_page import TransferPage
from .pages.screen_page import FrameStreamer, ScreenPage
from .worker import Job

log = logging.getLogger(__name__)

PAGES = ["Phones", "Phone to phone", "Back up", "Restore", "Screen", "Rescue", "Brand guides"]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Phone Data Transfer {__version__}")
        self.resize(1080, 780)

        self.manager = DeviceManager()
        self.pool = QThreadPool.globalInstance()
        self.current_job: Job | None = None
        self.selected: Device | None = None
        # Building the window must not start background work; the first scan is
        # kicked off by _first_run_check once there is an event loop to run it.
        self._ready = False
        self.phone_screen: PhoneScreen | None = None
        self.streamer: FrameStreamer | None = None
        self.scrcpy: ScrcpySession | None = None

        central = QWidget()
        layout = QHBoxLayout(central)

        self.nav = QListWidget()
        self.nav.addItems(PAGES)
        self.nav.setFixedWidth(170)
        self.nav.currentRowChanged.connect(self._switch_page)
        layout.addWidget(self.nav)

        right = QVBoxLayout()
        self.stack = QStackedWidget()
        self.devices_page = DevicesPage()
        self.transfer_page = TransferPage()
        self.backup_page = BackupPage()
        self.restore_page = RestorePage()
        self.screen_page = ScreenPage()
        self.rescue_page = RescuePage()
        self.guide_page = GuidePage()
        for page in (
            self.devices_page,
            self.transfer_page,
            self.backup_page,
            self.restore_page,
            self.screen_page,
            self.rescue_page,
            self.guide_page,
        ):
            self.stack.addWidget(page)
        right.addWidget(self.stack)
        layout.addLayout(right, 1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.nav.setCurrentRow(0)

        self._connect()
        self._ready = True
        QTimer.singleShot(200, self._first_run_check)

    # --- wiring -------------------------------------------------------
    def _connect(self) -> None:
        self.devices_page.rescan_requested.connect(self.rescan)
        self.devices_page.authorize_requested.connect(self.ask_for_permission)
        self.devices_page.device_selected.connect(self._device_selected)

        self.transfer_page.rescan_requested.connect(self.rescan)
        self.transfer_page.start_requested.connect(self.start_transfer)
        self.transfer_page.cancel_requested.connect(self.cancel_job)
        self.transfer_page.mirror_requested.connect(self.mirror_this_phone)

        self.backup_page.start_requested.connect(self.start_backup)
        self.backup_page.cancel_requested.connect(self.cancel_job)

        self.restore_page.start_requested.connect(self.start_restore)
        self.restore_page.cancel_requested.connect(self.cancel_job)

        self.rescue_page.diagnose_requested.connect(self.run_diagnosis)
        self.rescue_page.extract_requested.connect(self.start_extract)
        self.rescue_page.sideload_requested.connect(self.start_sideload)
        self.rescue_page.reboot_requested.connect(self.do_reboot)
        self.rescue_page.restart_adb_requested.connect(self.do_restart_adb)
        self.rescue_page.nokia_requested.connect(self.run_nokia_action)

        self.screen_page.start_requested.connect(self.start_mirroring)
        self.screen_page.stop_requested.connect(self.stop_mirroring)
        self.screen_page.tap_requested.connect(lambda x, y: self._screen_do(lambda s: s.tap(x, y)))
        self.screen_page.swipe_requested.connect(
            lambda x1, y1, x2, y2: self._screen_do(lambda s: s.swipe(x1, y1, x2, y2))
        )
        self.screen_page.key_requested.connect(lambda k: self._screen_do(lambda s: s.key(k)))
        self.screen_page.text_requested.connect(lambda t: self._screen_do(lambda s: s.type_text(t)))
        self.screen_page.unlock_requested.connect(self.do_unlock)
        self.screen_page.wait_unlock_requested.connect(self.guided_unlock_and_mirror)
        self.screen_page.scrcpy_button.clicked.connect(self.launch_scrcpy)
        self.screen_page.get_scrcpy_button.clicked.connect(self.install_scrcpy)

    def _switch_page(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        if row == 0 and self._ready:
            self.rescan()

    def _device_selected(self, device: Device) -> None:
        self.selected = device
        self.backup_page.set_device(device)
        self.restore_page.set_device(device)
        self.screen_page.set_device(device)

    # --- startup ------------------------------------------------------
    def _first_run_check(self) -> None:
        ready, message = self.manager.tools_ready()
        if ready:
            self.rescan()
            return
        answer = QMessageBox.question(
            self,
            "Android tools needed",
            message + "\n\nDownload them from Google now? (about 15 MB)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("adb/fastboot missing - most features are unavailable.")
            return
        self.statusBar().showMessage("Downloading platform-tools…")
        try:
            download_platform_tools()
        except Exception as exc:
            QMessageBox.warning(self, "Download failed", str(exc))
            return
        self.manager = DeviceManager(discover())
        self.statusBar().showMessage("Android tools installed.")
        self.rescan()

    # --- jobs ---------------------------------------------------------
    def _run(self, fn, on_finished, page=None, **kwargs) -> None:
        if self.current_job is not None:
            QMessageBox.information(self, "Busy", "Something is already running. Wait for it or stop it first.")
            return
        job = Job(fn, **kwargs)
        self.current_job = job
        if page is not None:
            job.signals.event.connect(page.on_event)
            if hasattr(page, "set_running"):
                page.set_running(True)

        def done(result):
            self.current_job = None
            if page is not None and hasattr(page, "set_running"):
                page.set_running(False)
            on_finished(result)

        def failed(message):
            self.current_job = None
            if page is not None and hasattr(page, "set_running"):
                page.set_running(False)
            self.statusBar().showMessage(message.splitlines()[0][:120])
            QMessageBox.warning(self, "That did not work", message)

        job.signals.finished.connect(done)
        job.signals.failed.connect(failed)
        self.pool.start(job)

    def cancel_job(self) -> None:
        if self.current_job is not None:
            self.current_job.stop()
            self.statusBar().showMessage("Stopping…")

    # --- actions ------------------------------------------------------
    def rescan(self) -> None:
        self.statusBar().showMessage("Scanning…")

        def scan(reporter, cancel):
            return self.manager.scan()

        def done(devices):
            self.devices_page.set_devices(devices)
            self.transfer_page.set_devices(devices)
            self.statusBar().showMessage(f"{len(devices)} device(s) found")

        self._run(scan, done)

    def start_transfer(self, source: Device, target: Device, staging: str, sections: tuple, keep: bool) -> None:
        """Old phone -> PC -> new phone, in one go."""
        options = TransferOptions(sections=tuple(sections), keep_bundle=keep)

        def work(reporter: Reporter, cancel):
            engine = TransferEngine(self.manager, source, target, reporter, cancel)
            return engine.run(Path(staging), options)

        def done(report) -> None:
            self.transfer_page.log.append("")
            self.transfer_page.log.append(report.as_text())
            self.statusBar().showMessage(
                "Transfer finished" if report.ok else "Transfer finished with problems"
            )
            todo = report.todo()
            body = report.as_text()
            if todo:
                body = "Finish these on the new phone:\n\n" + "\n".join(todo)
            QMessageBox.information(self, "Transfer finished", body[:2000])

        self._run(work, done, page=self.transfer_page)

    def mirror_this_phone(self, device: Device) -> None:
        """Jump to the Screen tab already mirroring the phone that was picked."""
        if device is None:
            return
        self.selected = device
        self.screen_page.set_device(device)
        self.nav.setCurrentRow(PAGES.index("Screen"))
        self.start_mirroring(device)

    def start_backup(self, device: Device, dest: str, sections: tuple, fresh: bool) -> None:
        options = BackupOptions(sections=tuple(sections), skip_existing=not fresh)

        def work(reporter: Reporter, cancel):
            engine = BackupEngine(self.manager.adb, device, reporter, cancel)
            return engine.run(dest, options)

        def done(report):
            self.backup_page.append("")
            for line in report.summary_lines():
                self.backup_page.append(line)
            self.backup_page.append(f"\nSaved to {report.bundle_path}")
            self.statusBar().showMessage("Backup finished")
            QMessageBox.information(
                self,
                "Backup finished",
                f"Saved to:\n{report.bundle_path}\n\n"
                + ("\n".join(report.warnings[:4]) if report.warnings else "No problems reported."),
            )

        self._run(work, done, page=self.backup_page)

    def start_restore(self, device: Device, bundle: str, sections: tuple) -> None:
        options = RestoreOptions(sections=tuple(sections))

        def work(reporter: Reporter, cancel):
            engine = RestoreEngine(self.manager.adb, device.serial or None, reporter, cancel)
            return engine.run(bundle, options)

        def done(report):
            for line in report.summary_lines():
                self.restore_page.log.append(line)
            pending = report.needs_user
            if pending:
                text = "\n\n".join(
                    f"{s.name}: {s.message}" + ("\n  • " + "\n  • ".join(s.todo) if s.todo else "")
                    for s in pending
                )
                QMessageBox.information(self, "Finish on the phone", text)
            self.statusBar().showMessage("Restore finished")

        self._run(work, done, page=self.restore_page)

    def run_diagnosis(self, brand: str) -> None:
        def work(reporter, cancel):
            devices = self.manager.scan()
            return diagnose(devices, hint_brand=brand)

        def done(result):
            self.rescue_page.show_diagnosis(result)
            self.statusBar().showMessage(result.situation)

        self.statusBar().showMessage("Diagnosing…")
        self._run(work, done)

    def start_extract(self, device: Device, folder: str) -> None:
        def work(reporter: Reporter, cancel):
            engine = RescueEngine(self.manager, reporter, cancel)
            return engine.extract_from_recovery(device, Path(folder) / "rescued-files")

        def done(result):
            self.rescue_page.append(result.message)
            icon = QMessageBox.Icon.Information if result.succeeded else QMessageBox.Icon.Warning
            box = QMessageBox(icon, "Rescue attempt", result.message, parent=self)
            box.exec()

        self._run(work, done, page=self.rescue_page)

    def start_sideload(self, device: Device, zip_path: str) -> None:
        def work(reporter: Reporter, cancel):
            engine = RescueEngine(self.manager, reporter, cancel)
            return engine.sideload(device, zip_path)

        def done(message):
            self.rescue_page.append(message)

        self._run(work, done, page=self.rescue_page)

    # --- screen mirroring ---------------------------------------------
    def start_mirroring(self, device: Device) -> None:
        """Mirror the phone - scrcpy by default, screenshots only as a fallback."""
        try:
            PhoneScreen.check_supported(device)
        except ScreenUnavailable as exc:
            self.screen_page.show_error(str(exc))
            return

        # Always keep a PhoneScreen around: the unlock box and the phone
        # buttons use it whichever backend is drawing the picture.
        self.phone_screen = PhoneScreen(self.manager.adb, device.serial or None)

        plan = choose_backend()
        if plan.needs_download:
            self.screen_page.show_status("Downloading scrcpy (about 30 MB) - one time only…")
            self._run(
                lambda reporter, cancel: str(download_scrcpy(progress=lambda f: reporter.progress(f, "downloading scrcpy"))),
                lambda _where: self._start_scrcpy(device),
                page=self.screen_page,
            )
            return

        if plan.is_scrcpy:
            self._start_scrcpy(device)
            return

        self.screen_page.show_status(plan.reason)
        self._start_screenshot_mirror(device)

    def _start_scrcpy(self, device: Device) -> None:
        self.screen_page.refresh_scrcpy_state()
        path = scrcpy_path()
        if not path:
            self.screen_page.show_error("scrcpy is still not available; using screenshots instead.")
            self._start_screenshot_mirror(device)
            return

        self.scrcpy = ScrcpySession(
            path=path,
            serial=device.serial or None,
            borderless=True,
            tools=self.manager.tools,
        )
        if not self.scrcpy.start():
            self.screen_page.show_error(self.scrcpy.error)
            self._start_screenshot_mirror(device)
            return

        self.screen_page.set_streaming(True)
        self.screen_page.show_status("Starting the live mirror…")

        def wait(reporter: Reporter, cancel):
            return self.scrcpy.wait_for_window(timeout=25)

        def done(hwnd: int) -> None:
            if not hwnd:
                # scrcpy died, or this is not Windows: say why, keep whatever
                # window it managed to open, and fall back if it is gone.
                message = explain_failure(self.scrcpy.error or self.scrcpy.read_output())
                if self.scrcpy.running:
                    self.screen_page.show_status(
                        "scrcpy is running in its own window - it could not be embedded here, "
                        "but it works the same."
                    )
                    return
                self.screen_page.show_error(f"{message}\n\nFalling back to screenshots.")
                self.scrcpy.stop()
                self._start_screenshot_mirror(device)
                return

            if self.screen_page.embed(hwnd):
                self.screen_page.show_status(
                    "Live mirror. The lock screen shows here too - click and type straight into it."
                )
            else:
                self.screen_page.show_status("scrcpy is running in its own window.")

        self._run(wait, done)

    def _start_screenshot_mirror(self, device: Device) -> None:
        interval = 150 if self.screen_page.smooth_check.isChecked() else 400
        self.streamer = FrameStreamer(self.phone_screen, interval)
        self.streamer.frame.connect(self.screen_page.show_frame)
        self.streamer.failed.connect(self._mirroring_failed)
        self.streamer.start()
        self.screen_page.set_streaming(True)
        self.screen_page.show_status(
            "Mirroring with screenshots. Click to tap, drag to swipe, type to send keys."
        )

    def stop_mirroring(self) -> None:
        if self.streamer is not None:
            self.streamer.stop()
            self.streamer.wait(2000)
            self.streamer = None
        if self.scrcpy is not None:
            # Detach the embedded window before killing the process that owns
            # it, or Qt is left holding a destroyed native window.
            self.screen_page.clear_embedded()
            self.scrcpy.stop()
            self.scrcpy = None
        self.screen_page.set_streaming(False)
        self.screen_page.show_status("Stopped.")

    def _mirroring_failed(self, message: str) -> None:
        self.stop_mirroring()
        self.screen_page.show_error(message)

    def ask_for_permission(self) -> None:
        """Trigger the phone's 'Allow USB debugging?' prompt and wait for it."""

        def work(reporter: Reporter, cancel):
            auth = Authorizer(self.manager, reporter, cancel)
            situation = auth.situation()
            if situation.ready or not situation.can_prompt:
                # Nothing to trigger: either it is already fine, or debugging
                # is off and only the phone's own menus can change that.
                return situation.as_text()
            return auth.request().message

        def done(message: str) -> None:
            self.statusBar().showMessage(message.splitlines()[0][:120])
            QMessageBox.information(self, "USB debugging", message)
            self.rescan()

        self.statusBar().showMessage("Asking the phone…")
        self._run(work, done)

    def install_scrcpy(self) -> None:
        """Download scrcpy - the only way to see a secure screen like the keypad."""

        def work(reporter: Reporter, cancel):
            reporter.status("Downloading scrcpy…")
            return str(download_scrcpy(progress=lambda f: reporter.progress(f, "downloading scrcpy")))

        def done(where: str) -> None:
            self.screen_page.refresh_scrcpy_state()
            self.screen_page.show_status(f"scrcpy installed to {where}. Press 'Open in scrcpy'.")

        self.screen_page.show_status("Fetching scrcpy…")
        self._run(work, done, page=self.screen_page)

    def launch_scrcpy(self) -> None:
        """Hand off to scrcpy, which mirrors at full frame rate."""
        path = scrcpy_path()
        if path is None:
            QMessageBox.information(
                self,
                "scrcpy not installed",
                "scrcpy gives a much smoother mirror than the built-in view. Install it and "
                "restart this app to enable this button.",
            )
            return
        args = [path]
        if self.selected is not None and self.selected.serial:
            args += ["-s", self.selected.serial]
        try:
            subprocess.Popen(args)
        except OSError as exc:
            QMessageBox.warning(self, "Could not start scrcpy", str(exc))
            return
        self.screen_page.show_status("scrcpy launched in its own window.")

    def _fire(self, fn, on_done=None) -> None:
        """Run a short action without taking the single-job slot.

        Taps and keystrokes arrive far faster than one at a time, and routing
        them through _run made a second tap pop a 'Busy' dialog.
        """
        job = Job(lambda reporter, cancel: fn())
        if on_done is not None:
            job.signals.finished.connect(on_done)
        job.signals.failed.connect(
            lambda message: self.screen_page.show_error(message.splitlines()[0][:160])
        )
        self.pool.start(job)

    def _screen_do(self, action) -> None:
        """Fire one input action at the phone, off the UI thread."""
        if self.phone_screen is None:
            self.screen_page.show_error("Start mirroring first.")
            return
        self._fire(lambda: action(self.phone_screen))

    def do_unlock(self, credential: str, numeric: bool) -> None:
        if self.phone_screen is None:
            self.screen_page.show_error(
                "Start mirroring first - the unlock box talks to the phone through the same "
                "connection."
            )
            return
        self.screen_page.show_status("Sending the passcode…")
        self._fire(
            lambda: self.phone_screen.unlock(credential, numeric),
            self.screen_page.show_status,
        )

    def guided_unlock_and_mirror(self, credential: str, numeric: bool, then_mirror: bool) -> None:
        """The blank-screen flow: wait for the phone, unlock it, open the mirror.

        This does not need mirroring to be running first - it drives the phone
        over adb directly, which is the whole point for a screen you cannot see.
        """
        device = self.selected or self.screen_page.device
        if device is None:
            self.screen_page.show_error("Pick a phone on the Phones tab first.")
            return

        serial = device.serial or None

        def work(reporter: Reporter, cancel):
            flow = UnlockFlow(self.manager, serial, reporter, cancel)
            return flow.wait_unlock_and_prepare(credential, numeric, timeout=300)

        def done(result) -> None:
            self.screen_page.show_status(result.message)
            if then_mirror and result.can_mirror:
                self.screen_page.show_status(result.message + "  Opening the mirror…")
                # Re-fetch the device: it may have only just come online.
                fresh = self._device_by_serial(serial) or device
                self.start_mirroring(fresh)

        self.screen_page.show_status("Waiting for the phone…")
        self._run(work, done, page=self.screen_page)

    def _device_by_serial(self, serial: str | None) -> Device | None:
        if not serial:
            return None
        try:
            for d in self.manager.scan(deep=False):
                if d.serial == serial:
                    return d
        except Exception:
            pass
        return None

    def run_nokia_action(self, device: Device, action: str) -> None:
        def work(reporter: Reporter, cancel):
            rescue = NokiaRescue(self.manager, reporter, cancel)
            if action == "guided":
                return rescue.guided_rescue(device, Path.home()).as_text()
            if action == "logs":
                text = rescue.read_recovery_log(device)
                if not text:
                    return (
                        "No crash log reachable. The phone has to be in recovery mode for this:\n"
                        "power it off, hold Volume Up, then press and hold Power."
                    )
                findings = rescue.analyse_log(text)
                if not findings:
                    return "Log retrieved, but nothing in it matches a known failure.\n\n" + text[-2000:]
                lines = ["What the phone says went wrong:", ""]
                for f in findings:
                    lines.append(f"  [{f.severity}] {f.meaning}")
                    lines.append(f"      -> {f.next_step}")
                return "\n".join(lines)
            if action == "switch-slot":
                outcome = rescue.switch_slot(device)
                text = f"{outcome.status}: {outcome.message}"
                return text + (f"\n  {outcome.detail}" if outcome.detail else "")
            if action == "fix-bootloop":
                session = rescue.repair_boot_loop(device, workdir=Path.home())
                return session.as_text()
            if action == "undo-slot":
                outcome = rescue.undo_slot_switch(device)
                return f"{outcome.status}: {outcome.message}\n  {outcome.detail}"
            if action == "find-ota":
                packages = rescue.find_ota_packages(device)
                if not packages:
                    return "No update package found on the phone."
                outcome = rescue.apply_ota_from_device(device, packages[0], Path.home())
                return f"{outcome.status}: {outcome.message}\n  {outcome.detail}"
            return f"Unknown action {action}"

        def done(text: str) -> None:
            self.rescue_page.append(text)
            if action in ("guided", "switch-slot", "fix-bootloop", "undo-slot"):
                QMessageBox.information(self, "Nokia rescue", text[:2000])

        self._run(work, done, page=self.rescue_page)

    def do_reboot(self, device: Device, target: str) -> None:
        engine = RescueEngine(self.manager)
        self.rescue_page.append(engine.reboot(device, target))

    def do_restart_adb(self) -> None:
        engine = RescueEngine(self.manager)
        self.rescue_page.append(engine.restart_adb())
        self.rescan()

    # --- window -------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.streamer is not None or self.scrcpy is not None:
            self.stop_mirroring()
        if self.current_job is not None:
            answer = QMessageBox.question(
                self,
                "Still working",
                "A transfer is running. Stop it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.current_job.stop()
        event.accept()
