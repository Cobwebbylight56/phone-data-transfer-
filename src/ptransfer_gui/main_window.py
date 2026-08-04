"""The main window: wires the pages to the engine through background jobs."""

from __future__ import annotations

import logging
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
from ptransfer.backup import BackupEngine, BackupOptions
from ptransfer.devices import Device, DeviceManager
from ptransfer.logging_setup import log_file
from ptransfer.platform_tools import download_platform_tools, discover
from ptransfer.progress import Event, Reporter
from ptransfer.recovery import RescueEngine, diagnose
from ptransfer.restore import RestoreEngine, RestoreOptions

from .pages.backup_page import BackupPage
from .pages.devices_page import DevicesPage
from .pages.guide_page import GuidePage
from .pages.rescue_page import RescuePage
from .pages.restore_page import RestorePage
from .worker import Job

log = logging.getLogger(__name__)

PAGES = ["Phones", "Back up", "Restore", "Rescue", "Brand guides"]


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
        self.backup_page = BackupPage()
        self.restore_page = RestorePage()
        self.rescue_page = RescuePage()
        self.guide_page = GuidePage()
        for page in (self.devices_page, self.backup_page, self.restore_page, self.rescue_page, self.guide_page):
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
        self.devices_page.device_selected.connect(self._device_selected)

        self.backup_page.start_requested.connect(self.start_backup)
        self.backup_page.cancel_requested.connect(self.cancel_job)

        self.restore_page.start_requested.connect(self.start_restore)
        self.restore_page.cancel_requested.connect(self.cancel_job)

        self.rescue_page.diagnose_requested.connect(self.run_diagnosis)
        self.rescue_page.extract_requested.connect(self.start_extract)
        self.rescue_page.sideload_requested.connect(self.start_sideload)
        self.rescue_page.reboot_requested.connect(self.do_reboot)
        self.rescue_page.restart_adb_requested.connect(self.do_restart_adb)

    def _switch_page(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        if row == 0 and self._ready:
            self.rescan()

    def _device_selected(self, device: Device) -> None:
        self.selected = device
        self.backup_page.set_device(device)
        self.restore_page.set_device(device)

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
            self.statusBar().showMessage(f"{len(devices)} device(s) found")

        self._run(scan, done)

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

    def do_reboot(self, device: Device, target: str) -> None:
        engine = RescueEngine(self.manager)
        self.rescue_page.append(engine.reboot(device, target))

    def do_restart_adb(self) -> None:
        engine = RescueEngine(self.manager)
        self.rescue_page.append(engine.restart_adb())
        self.rescan()

    # --- window -------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
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
