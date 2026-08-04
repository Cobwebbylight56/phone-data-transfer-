"""Devices page: what is plugged in, and what state it is in."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ptransfer.devices import Device, State
from ptransfer.oem import profile_for

STATE_COLOURS = {
    State.ONLINE: "#1a7f37",
    State.UNAUTHORIZED: "#9a6700",
    State.RECOVERY: "#9a6700",
    State.SIDELOAD: "#9a6700",
    State.BOOTLOADER: "#9a6700",
    State.LOW_LEVEL: "#b35900",
    State.OFFLINE: "#cf222e",
    State.UNKNOWN: "#57606a",
}

STATE_HELP = {
    State.ONLINE: "Ready. You can back this phone up now.",
    State.UNAUTHORIZED: "Unlock the phone and tap Allow on the USB debugging prompt.",
    State.RECOVERY: "In recovery. Go to the Rescue tab.",
    State.SIDELOAD: "Waiting for an update package. Go to the Rescue tab.",
    State.BOOTLOADER: "In fastboot. Go to the Rescue tab.",
    State.LOW_LEVEL: "In a low-level rescue mode. Go to the Rescue tab.",
    State.OFFLINE: "Detected but not responding - try Restart adb on the Rescue tab.",
    State.UNKNOWN: "Unrecognised state.",
}


class DevicesPage(QWidget):
    device_selected = Signal(object)
    rescan_requested = Signal()
    authorize_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.devices: list[Device] = []

        layout = QVBoxLayout(self)
        header = QLabel("<h2>Connected phones</h2>")
        layout.addWidget(header)

        # Plain text, not rich: Qt only treats a string as HTML when it contains
        # tags, so entities in a tag-free string would render literally.
        hint = QLabel(
            "Plug the phone in with a data cable, unlock it, and turn on USB debugging "
            "(Settings → About phone → tap Build number seven times, then "
            "Settings → System → Developer options → USB debugging)."
        )
        hint.setTextFormat(Qt.TextFormat.PlainText)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #57606a;")
        layout.addWidget(hint)

        row = QHBoxLayout()
        self.scan_button = QPushButton("Scan for phones")
        self.scan_button.clicked.connect(self.rescan_requested.emit)
        row.addWidget(self.scan_button)

        self.authorize_button = QPushButton("Ask the phone for permission")
        self.authorize_button.setToolTip(
            "Makes the phone show its 'Allow USB debugging?' prompt, then waits for you to tap "
            "Allow. Use this when the phone shows as unauthorized."
        )
        self.authorize_button.clicked.connect(self.authorize_requested.emit)
        row.addWidget(self.authorize_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_row)
        layout.addWidget(self.list, 2)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 3)

    def set_devices(self, devices: list[Device]) -> None:
        self.devices = devices
        self.list.clear()
        for d in devices:
            item = QListWidgetItem(f"{d.label}   —   {d.state.value}")
            item.setData(Qt.ItemDataRole.UserRole, d.serial)
            self.list.addItem(item)
        if devices:
            self.list.setCurrentRow(0)
        else:
            self.details.setPlainText(
                "Nothing detected.\n\n"
                "Most common causes, in order:\n"
                "  1. The cable only carries power. Charge-only cables look identical to data cables.\n"
                "  2. USB debugging is off, or the Allow prompt was never accepted.\n"
                "  3. A front-panel USB port or hub. Use one on the back of the PC.\n"
                "  4. Missing driver - the Rescue tab will tell you if Windows sees the phone at all.\n\n"
                "A phone that will not boot may still be detectable. Try the Rescue tab."
            )

    def current_device(self) -> Device | None:
        row = self.list.currentRow()
        if 0 <= row < len(self.devices):
            return self.devices[row]
        return None

    def _on_row(self, row: int) -> None:
        device = self.current_device()
        if device is None:
            return
        self.details.setHtml(self._describe(device))
        self.device_selected.emit(device)

    def _describe(self, d: Device) -> str:
        colour = STATE_COLOURS.get(d.state, "#57606a")
        profile = profile_for(d.vendor_key)
        rows = [
            ("State", f"<span style='color:{colour}'><b>{d.state.value}</b></span>"),
            ("Model", d.model or "-"),
            ("Manufacturer", d.manufacturer or d.brand or "-"),
            ("Android", f"{d.android_release} (SDK {d.sdk})" if d.android_release else "-"),
            ("Build", d.build_id or "-"),
            ("Serial", d.serial or "-"),
            ("Brand guide", profile.display_name),
        ]
        if d.usb is not None:
            rows.append(("USB", str(d.usb)))
        html = ["<table cellspacing='6'>"]
        for k, v in rows:
            html.append(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>")
        html.append("</table>")
        html.append(f"<p>{STATE_HELP.get(d.state, '')}</p>")
        if d.note:
            html.append(f"<p style='color:#9a6700'><b>Note:</b> {d.note}</p>")
        return "".join(html)
