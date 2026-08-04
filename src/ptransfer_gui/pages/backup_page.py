"""Backup page: choose what to copy, then watch it happen."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ptransfer.backup import SECTIONS
from ptransfer.devices import Device
from ptransfer.manifest import safe_bundle_name
from ptransfer.progress import Event, human_bytes

SECTION_LABELS = {
    "deviceinfo": ("Phone details", "Model, Android version, settings inventory. Tiny."),
    "media": ("Photos, videos and files", "Everything in internal storage. This is the big one."),
    "contacts": ("Contacts", "Exported as a standard .vcf any phone can import."),
    "sms": ("Text messages", "Saved as JSON. Needs the companion app to write back."),
    "calls": ("Call log", "Saved as JSON."),
    "apps": ("Installed apps (APKs)", "The apps themselves. Their private data cannot be copied without root."),
    "settings": ("Settings snapshot", "A readable record. Android will not let most of it be written back."),
}


class BackupPage(QWidget):
    start_requested = Signal(object, str, tuple, bool)  # device, dest, sections, fresh
    cancel_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.device: Device | None = None
        self._running = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Copy the old phone</h2>"))

        self.device_label = QLabel("No phone selected.")
        self.device_label.setStyleSheet("color: #57606a;")
        layout.addWidget(self.device_label)

        box = QGroupBox("What to copy")
        box_layout = QVBoxLayout(box)
        self.checks: dict[str, QCheckBox] = {}
        for name in SECTIONS:
            title, detail = SECTION_LABELS[name]
            cb = QCheckBox(f"{title}  —  {detail}")
            cb.setChecked(True)
            self.checks[name] = cb
            box_layout.addWidget(cb)
        layout.addWidget(box)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Save to:"))
        self.dest_edit = QLineEdit(str(Path.home() / "PhoneTransfers"))
        dest_row.addWidget(self.dest_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        dest_row.addWidget(browse)
        layout.addLayout(dest_row)

        self.fresh_check = QCheckBox("Re-copy files that are already saved (slower; normally leave off)")
        layout.addWidget(self.fresh_check)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("Start backup")
        self.start_button.clicked.connect(self._start)
        button_row.addWidget(self.start_button)
        self.cancel_button = QPushButton("Stop")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        button_row.addWidget(self.cancel_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        layout.addWidget(self.progress)

        self.status = QLabel("")
        layout.addWidget(self.status)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

    def set_device(self, device: Device | None) -> None:
        self.device = device
        if device is None:
            self.device_label.setText("No phone selected.")
            self.start_button.setEnabled(False)
            return
        ready = device.state.can_transfer
        self.device_label.setText(
            f"Source: <b>{device.label}</b> ({device.state.value})"
            + ("" if ready else " — this phone is not readable yet; see the Rescue tab.")
        )
        self.start_button.setEnabled(ready and not self._running)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Where should the backup go?", self.dest_edit.text())
        if chosen:
            self.dest_edit.setText(chosen)

    def _start(self) -> None:
        if self.device is None:
            return
        sections = tuple(n for n, cb in self.checks.items() if cb.isChecked())
        if not sections:
            self.append("Nothing selected to copy.")
            return
        name = safe_bundle_name(self.device.label) + ".ptbundle"
        dest = str(Path(self.dest_edit.text()) / name)
        self.log.clear()
        self.append(f"Backing up to {dest}")
        self.start_requested.emit(self.device, dest, sections, self.fresh_check.isChecked())

    def set_running(self, running: bool) -> None:
        self._running = running
        self.start_button.setEnabled(not running and self.device is not None and self.device.state.can_transfer)
        self.cancel_button.setEnabled(running)
        for cb in self.checks.values():
            cb.setEnabled(not running)

    def on_event(self, e: Event) -> None:
        if e.kind == "section":
            self.append(f"\n— {e.message}")
            self.progress.setValue(0)
        elif e.kind == "progress":
            if e.fraction >= 0:
                self.progress.setValue(int(e.fraction * 1000))
            detail = e.message
            if e.total_bytes:
                detail += f"   {human_bytes(e.done_bytes)} of {human_bytes(e.total_bytes)}"
            elif e.total_items:
                detail += f"   {e.done_items}/{e.total_items}"
            self.status.setText(detail)
        elif e.kind == "status":
            self.append(f"  {e.message}")
        elif e.kind == "warning":
            self.append(f"  ⚠ {e.message}")
        elif e.kind == "error":
            self.append(f"  ✖ {e.message}")
        elif e.kind == "done":
            self.progress.setValue(1000)
            self.append(e.message)

    def append(self, text: str) -> None:
        self.log.append(text)
