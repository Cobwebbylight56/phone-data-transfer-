"""Phone to phone, both connected: pick old, pick new, go."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
from ptransfer.devices import Device, State
from ptransfer.progress import Event, human_bytes
from ptransfer.transfer import pick_pair

SECTION_LABELS = {
    "deviceinfo": "Phone details",
    "media": "Photos, videos and files",
    "contacts": "Contacts",
    "sms": "Text messages",
    "calls": "Call log",
    "apps": "Apps",
    "settings": "Settings snapshot",
}


class PhonePicker(QGroupBox):
    """One side of the transfer."""

    mirror_requested = Signal(object)
    changed = Signal()

    def __init__(self, title: str, subtitle: str, parent=None) -> None:
        super().__init__(title, parent)
        self.devices: list[Device] = []

        layout = QVBoxLayout(self)
        caption = QLabel(subtitle)
        caption.setStyleSheet("color: #57606a;")
        caption.setWordWrap(True)
        layout.addWidget(caption)

        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        layout.addWidget(self.combo)

        self.state = QLabel("")
        self.state.setWordWrap(True)
        layout.addWidget(self.state)

        # The whole point when a screen is broken: see this phone on the PC.
        self.mirror_button = QPushButton("Show this phone's screen")
        self.mirror_button.setToolTip(
            "Mirror this phone so you can see and control it from here - for a phone whose "
            "screen is broken or unreadable."
        )
        self.mirror_button.clicked.connect(lambda: self.mirror_requested.emit(self.current()))
        layout.addWidget(self.mirror_button)
        layout.addStretch(1)

    def set_devices(self, devices: list[Device], prefer: Device | None = None) -> None:
        self.devices = devices
        current = self.current()
        self.combo.blockSignals(True)
        self.combo.clear()
        for d in devices:
            self.combo.addItem(f"{d.label}  —  {d.state.value}", d.serial)
        self.combo.blockSignals(False)

        wanted = prefer or current
        if wanted is not None:
            index = self.combo.findData(wanted.serial)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        self._describe()

    def current(self) -> Device | None:
        serial = self.combo.currentData()
        for d in self.devices:
            if d.serial == serial:
                return d
        return None

    def _describe(self) -> None:
        device = self.current()
        if device is None:
            self.state.setText("<i>No phone selected.</i>")
            self.mirror_button.setEnabled(False)
            return
        ready = device.state is State.ONLINE
        colour = "#1a7f37" if ready else "#cf222e"
        note = "Ready." if ready else f"Not ready — {device.state.value}."
        extra = ""
        if device.android_release:
            extra = f"<br>Android {device.android_release}"
        self.state.setText(f"<span style='color:{colour}'>{note}</span>{extra}")
        self.mirror_button.setEnabled(ready)


class TransferPage(QWidget):
    start_requested = Signal(object, object, str, tuple, bool)
    cancel_requested = Signal()
    rescan_requested = Signal()
    mirror_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Phone to phone</h2>"))

        intro = QLabel(
            "Plug both phones in. This copies the old one onto the new one with the PC in the "
            "middle — which is the way to do it when a screen is broken, because there is no "
            "tapping on the phone itself."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #57606a;")
        layout.addWidget(intro)

        picker_row = QHBoxLayout()
        self.source = PhonePicker("From — the old phone", "Everything is copied off this one.")
        self.target = PhonePicker("To — the new phone", "Everything is written onto this one.")
        for picker in (self.source, self.target):
            picker.mirror_requested.connect(self.mirror_requested.emit)
            picker.changed.connect(self._validate)
            picker_row.addWidget(picker, 1)
        layout.addLayout(picker_row)

        controls = QHBoxLayout()
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self.rescan_requested.emit)
        controls.addWidget(rescan)
        swap = QPushButton("⇄ Swap")
        swap.setToolTip("Got them the wrong way round? This swaps old and new.")
        swap.clicked.connect(self._swap)
        controls.addWidget(swap)
        controls.addStretch(1)
        layout.addLayout(controls)

        box = QGroupBox("What to copy")
        box_layout = QHBoxLayout(box)
        self.checks: dict[str, QCheckBox] = {}
        for name in SECTIONS:
            cb = QCheckBox(SECTION_LABELS[name])
            cb.setChecked(True)
            self.checks[name] = cb
            box_layout.addWidget(cb)
        layout.addWidget(box)

        staging_row = QHBoxLayout()
        staging_row.addWidget(QLabel("Hold the copy in:"))
        self.staging = QLineEdit(str(Path.home() / "PhoneTransfers"))
        staging_row.addWidget(self.staging, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        staging_row.addWidget(browse)
        layout.addLayout(staging_row)

        self.keep_check = QCheckBox("Keep the copy on the PC afterwards (recommended — it is a full backup)")
        self.keep_check.setChecked(True)
        layout.addWidget(self.keep_check)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("Start the transfer")
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

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

        self._validate()

    # --- devices ------------------------------------------------------
    def set_devices(self, devices: list[Device]) -> None:
        guess_source, guess_target = pick_pair(devices)
        # pick_pair only suggests phones that are ready. When one is not, fall
        # back to two *different* phones anyway - otherwise both sides land on
        # the same one and the user is told "same phone selected" when the real
        # problem is that the other phone has not been authorised.
        if len(devices) >= 2:
            if guess_source is None:
                guess_source = devices[0]
            if guess_target is None or guess_target.serial == guess_source.serial:
                guess_target = next(
                    (d for d in devices if d.serial != guess_source.serial), guess_target
                )
        self.source.set_devices(devices, guess_source)
        self.target.set_devices(devices, guess_target)
        self._validate()

    def _swap(self) -> None:
        a, b = self.source.current(), self.target.current()
        if a is None or b is None:
            return
        self.source.set_devices(self.source.devices, b)
        self.target.set_devices(self.target.devices, a)
        self._validate()

    def _validate(self) -> None:
        source, target = self.source.current(), self.target.current()
        if source is None or target is None:
            self.status.setText("Connect both phones, then press Rescan.")
            self.start_button.setEnabled(False)
            return
        if source.serial == target.serial:
            self.status.setText(
                "<span style='color:#cf222e'>Both sides are the same phone. Pick a different "
                "one, or press Swap.</span>"
            )
            self.start_button.setEnabled(False)
            return
        not_ready = [
            ("old", source) for _ in (1,) if source.state is not State.ONLINE
        ] + [("new", target) for _ in (1,) if target.state is not State.ONLINE]
        if not_ready:
            names = ", ".join(f"the {role} phone" for role, _ in not_ready)
            self.status.setText(
                f"<span style='color:#cf222e'>Not ready: {names}. Each phone needs USB debugging "
                "allowed — use 'Ask the phone for permission' on the Phones tab.</span>"
            )
            self.start_button.setEnabled(False)
            return
        self.status.setText(
            f"Ready: <b>{source.label}</b> → <b>{target.label}</b>. "
            "Nothing on the old phone is changed or deleted."
        )
        self.start_button.setEnabled(True)

    # --- actions ------------------------------------------------------
    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Where should the copy be held?", self.staging.text())
        if chosen:
            self.staging.setText(chosen)

    def _start(self) -> None:
        source, target = self.source.current(), self.target.current()
        if source is None or target is None:
            return
        sections = tuple(n for n, cb in self.checks.items() if cb.isChecked())
        if not sections:
            self.log.append("Nothing selected to copy.")
            return
        self.log.clear()
        self.start_requested.emit(
            source, target, self.staging.text(), sections, self.keep_check.isChecked()
        )

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        for cb in self.checks.values():
            cb.setEnabled(not running)

    def on_event(self, e: Event) -> None:
        if e.kind == "section":
            self.log.append(f"\n— {e.message}")
        elif e.kind == "progress":
            if e.fraction >= 0:
                self.progress.setValue(int(e.fraction * 1000))
            detail = e.message
            if e.total_bytes:
                detail += f"   {human_bytes(e.done_bytes)} of {human_bytes(e.total_bytes)}"
            elif e.total_items:
                detail += f"   {e.done_items}/{e.total_items}"
            self.status.setText(detail)
        elif e.kind in ("status", "warning", "error", "done") and e.message:
            prefix = {"warning": "⚠ ", "error": "✖ "}.get(e.kind, "  ")
            self.log.append(prefix + e.message)
