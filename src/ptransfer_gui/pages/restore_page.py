"""Restore page: put a bundle onto the new phone."""

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

from ptransfer.devices import Device
from ptransfer.manifest import Bundle
from ptransfer.progress import Event, human_bytes

RESTORE_SECTIONS = {
    "media": "Photos, videos and files",
    "apps": "Install the apps (APKs)",
    "contacts": "Contacts (one tap to confirm on the phone)",
    "sms": "Text messages (needs the companion app)",
    "calls": "Call log (needs the companion app)",
}


class RestorePage(QWidget):
    start_requested = Signal(object, str, tuple)
    cancel_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.device: Device | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Put it on the new phone</h2>"))

        self.device_label = QLabel("No phone selected.")
        self.device_label.setStyleSheet("color: #57606a;")
        layout.addWidget(self.device_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("Bundle:"))
        self.bundle_edit = QLineEdit()
        row.addWidget(self.bundle_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

        self.bundle_info = QLabel("")
        self.bundle_info.setWordWrap(True)
        layout.addWidget(self.bundle_info)

        box = QGroupBox("What to restore")
        box_layout = QVBoxLayout(box)
        self.checks: dict[str, QCheckBox] = {}
        for key, label in RESTORE_SECTIONS.items():
            cb = QCheckBox(label)
            cb.setChecked(True)
            self.checks[key] = cb
            box_layout.addWidget(cb)
        layout.addWidget(box)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("Start restore")
        self.start_button.clicked.connect(self._start)
        buttons.addWidget(self.start_button)
        self.cancel_button = QPushButton("Stop")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        layout.addWidget(self.progress)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

    def set_device(self, device: Device | None) -> None:
        self.device = device
        if device is None:
            self.device_label.setText("No phone selected.")
            return
        self.device_label.setText(f"Target: <b>{device.label}</b> ({device.state.value})")

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Pick the .ptbundle folder", str(Path.home()))
        if chosen:
            self.bundle_edit.setText(chosen)
            self._describe(chosen)

    def _describe(self, path: str) -> None:
        try:
            bundle = Bundle.open(path)
        except Exception as exc:
            self.bundle_info.setText(f"<span style='color:#cf222e'>{exc}</span>")
            return
        src = bundle.manifest.source
        parts = [
            f"From <b>{src.manufacturer} {src.model}</b> (Android {src.android_release}), "
            f"taken {bundle.manifest.created_at}.",
        ]
        for name, sec in sorted(bundle.manifest.sections.items()):
            if sec.status == "skipped":
                continue
            bits = []
            if sec.items:
                bits.append(f"{sec.items} items")
            if sec.files:
                bits.append(f"{sec.files} files")
            if sec.bytes:
                bits.append(human_bytes(sec.bytes))
            parts.append(f"&nbsp;&nbsp;{name}: {sec.status} ({', '.join(bits) or '-'})")
        self.bundle_info.setText("<br>".join(parts))

    def _start(self) -> None:
        if self.device is None or not self.bundle_edit.text().strip():
            self.log.append("Pick a phone and a bundle first.")
            return
        sections = tuple(k for k, cb in self.checks.items() if cb.isChecked())
        self.log.clear()
        self.start_requested.emit(self.device, self.bundle_edit.text().strip(), sections)

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    def on_event(self, e: Event) -> None:
        if e.kind == "section":
            self.log.append(f"\n— {e.message}")
        elif e.kind == "progress" and e.fraction >= 0:
            self.progress.setValue(int(e.fraction * 1000))
        elif e.kind in ("status", "warning", "error", "done") and e.message:
            prefix = {"warning": "⚠ ", "error": "✖ "}.get(e.kind, "  ")
            self.log.append(prefix + e.message)
