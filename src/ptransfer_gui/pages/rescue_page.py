"""Rescue page: for a phone that will not boot.

Deliberately opinionated layout - the diagnosis at the top, the ordered plan
below it, and the destructive options visually separated from the safe ones.
Someone using this screen is stressed and about to make an irreversible choice.
"""

from __future__ import annotations

from html import escape as esc

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ptransfer.devices import State
from ptransfer.menu import MENU, NAVIGATION, SAFE_ORDER
from ptransfer.menu import entry as menu_entry
from ptransfer.oem import ALL_PROFILES
from ptransfer.progress import Event
from ptransfer.recovery import Diagnosis

SEVERITY_STYLE = {
    "fine": ("#1a7f37", "The phone is healthy"),
    "soft-brick": ("#9a6700", "Soft-bricked — repairable"),
    "deep": ("#b35900", "Deep rescue mode"),
    "unknown": ("#57606a", "Not sure yet"),
}


class RescuePage(QWidget):
    diagnose_requested = Signal(str)
    extract_requested = Signal(object, str)
    sideload_requested = Signal(object, str)
    reboot_requested = Signal(object, str)
    restart_adb_requested = Signal()
    backup_requested = Signal()
    nokia_requested = Signal(object, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.diagnosis: Diagnosis | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Phone will not start</h2>"))

        intro = QLabel(
            "Run the diagnosis with the phone connected. It checks adb, fastboot, and the raw USB "
            "layer, so it can still identify a phone that ordinary tools cannot see at all."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #57606a;")
        layout.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Brand:"))
        self.brand_combo = QComboBox()
        self.brand_combo.addItem("Detect automatically", "")
        for p in ALL_PROFILES:
            self.brand_combo.addItem(p.display_name, p.key)
        row.addWidget(self.brand_combo)
        self.diagnose_button = QPushButton("Diagnose")
        self.diagnose_button.clicked.connect(
            lambda: self.diagnose_requested.emit(self.brand_combo.currentData())
        )
        row.addWidget(self.diagnose_button)
        restart = QPushButton("Restart adb")
        restart.clicked.connect(self.restart_adb_requested.emit)
        row.addWidget(restart)
        row.addStretch(1)
        layout.addLayout(row)

        self.headline = QLabel("")
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)

        self.report = QTextEdit()
        self.report.setReadOnly(True)
        layout.addWidget(self.report, 3)

        safe_box = QGroupBox("Safe actions — none of these erase anything")
        safe_layout = QHBoxLayout(safe_box)
        for label, target in (
            ("Reboot to system", ""),
            ("Reboot to recovery", "recovery"),
            ("Reboot to bootloader", "bootloader"),
        ):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, t=target: self._reboot(t))
            safe_layout.addWidget(b)
        extract = QPushButton("Try to rescue files from recovery")
        extract.clicked.connect(self._extract)
        safe_layout.addWidget(extract)
        layout.addWidget(safe_box)

        repair_box = QGroupBox("Repair the system (keeps your files)")
        repair_layout = QHBoxLayout(repair_box)
        sideload = QPushButton("Apply an OTA update package…")
        sideload.clicked.connect(self._sideload)
        repair_layout.addWidget(sideload)
        repair_layout.addStretch(1)
        layout.addWidget(repair_box)

        # Nokia gets its own panel: these actions do real work rather than
        # print advice, and they are the ones most likely to fix the phone.
        self.nokia_box = QGroupBox("Nokia — everything here is data-safe")
        nokia_outer = QVBoxLayout(self.nokia_box)

        fix_row = QHBoxLayout()
        self.fix_button = QPushButton("Fix the boot loop — undo the update, or finish it")
        self.fix_button.setToolTip(
            "Switches back to the system that was working before the update, restarts the phone "
            "and checks whether it came back. If that does not help, it tries applying the update "
            "in full instead. Neither writes to your data."
        )
        self.fix_button.clicked.connect(self._nokia_fix_bootloop)
        fix_row.addWidget(self.fix_button, 1)
        undo = QPushButton("Undo slot switch")
        undo.clicked.connect(self._nokia_undo_slot)
        fix_row.addWidget(undo)
        nokia_outer.addLayout(fix_row)

        nokia_layout = QHBoxLayout()
        for label, slot in (
            ("Guided rescue", self._nokia_guided),
            ("Read the phone's crash log", self._nokia_logs),
            ("Switch system slot", self._nokia_switch_slot),
            ("Find update on phone", self._nokia_find_ota),
        ):
            b = QPushButton(label)
            b.clicked.connect(slot)
            nokia_layout.addWidget(b)
        nokia_outer.addLayout(nokia_layout)

        self.nokia_box.setVisible(False)
        layout.addWidget(self.nokia_box)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        layout.addWidget(self.log, 1)

    # --- display ------------------------------------------------------
    def show_diagnosis(self, diagnosis: Diagnosis) -> None:
        self.diagnosis = diagnosis
        self.nokia_box.setVisible(diagnosis.profile.key == "nokia")
        colour, label = SEVERITY_STYLE.get(diagnosis.severity, SEVERITY_STYLE["unknown"])
        self.headline.setText(
            f"<h3 style='color:{colour}'>{label}</h3>"
            f"<p><b>{diagnosis.situation}</b></p><p>{diagnosis.explanation}</p>"
        )
        self.report.setHtml(self._render(diagnosis))

    def _render_recovery_menu(self) -> str:
        """The recovery menu, annotated - the one screen we cannot mirror."""
        html = [
            "<h4>You are looking at the recovery menu</h4>",
            "<p>The touchscreen does nothing here. Use the physical buttons:</p><ul>",
        ]
        for line in NAVIGATION:
            html.append(f"<li>{esc(line)}</li>")
        html.append("</ul>")

        html.append("<p><b>Work through these, in order:</b></p><ol>")
        for label in SAFE_ORDER:
            e = menu_entry(label)
            advice = f"<br><i>{esc(e.advice)}</i>" if e and e.advice else ""
            html.append(f"<li><b>{esc(label)}</b>{advice}</li>")
        html.append("</ol>")

        html.append("<p><b>Every option, and what it costs you:</b></p><ul>")
        for e in MENU:
            if e.destroys_data:
                html.append(
                    f"<li><span style='color:#cf222e'><b>{esc(e.label)} — ERASES EVERYTHING</b></span>"
                    f"<br>{esc(e.does)}<br><i>{esc(e.advice)}</i></li>"
                )
            else:
                html.append(
                    f"<li><span style='color:#1a7f37'><b>{esc(e.label)}</b></span> — {esc(e.does)}"
                    + (f"<br><i>{esc(e.advice)}</i>" if e.advice else "")
                    + "</li>"
                )
        html.append("</ul>")
        html.append(
            "<p style='color:#cf222e'><b>A word about the advice you will find elsewhere.</b> "
            "Nearly every walkthrough ends with 'if the cache wipe does not work, do a factory "
            "reset'. That is right if you want a working phone and do not need what is on it. It "
            "is the wrong move here: the reset discards the encryption key, so afterwards no tool "
            "and no recovery service can get your photos or messages back. Exhaust the safe "
            "options first.</p>"
        )
        return "".join(html)

    def _render(self, d: Diagnosis) -> str:
        html: list[str] = []
        if d.warnings:
            html.append("<div style='color:#cf222e'>")
            for w in d.warnings:
                html.append(f"<p><b>Warning:</b> {esc(w)}</p>")
            html.append("</div>")

        html.append("<h4>What to do, in order</h4><ol>")
        for step in d.steps:
            flag = " <span style='color:#cf222e'><b>[ERASES YOUR DATA]</b></span>" if step.warns else ""
            html.append(f"<li><b>{esc(step.title)}</b>{flag}<br>{esc(step.detail)}</li>")
        html.append("</ol>")

        # When the phone is sitting in recovery, what is on its screen right
        # now matters more than anything else we could show.
        if d.device is not None and d.device.state in (State.RECOVERY, State.SIDELOAD):
            html.append(self._render_recovery_menu())

        if d.profile.key_combos:
            html.append(f"<h4>Button combinations — {esc(d.profile.display_name)}</h4><ul>")
            for c in d.profile.key_combos:
                note = f"<br><i>{esc(c.note)}</i>" if c.note else ""
                html.append(f"<li><b>{esc(c.mode)}</b>: {esc(c.steps)}{note}</li>")
            html.append("</ul>")

        if d.profile.tools:
            html.append("<h4>Vendor tools</h4><ul>")
            for t in d.profile.tools:
                colour = {"safe": "#1a7f37", "usually": "#9a6700", "wipes": "#cf222e"}[t.data_safety.value]
                tag = {"safe": "data-safe", "usually": "usually keeps data", "wipes": "ERASES DATA"}[t.data_safety.value]
                url = f"<br><a href='{esc(t.url)}'>{esc(t.url)}</a>" if t.url else ""
                note = f"<br><i>{esc(t.note)}</i>" if t.note else ""
                html.append(
                    f"<li><b>{esc(t.name)}</b> <span style='color:{colour}'>[{tag}]</span>"
                    f"<br>{esc(t.purpose)}{url}{note}</li>"
                )
            html.append("</ul>")

        if d.profile.notes:
            html.append("<h4>Worth knowing</h4><ul>")
            for n in d.profile.notes:
                html.append(f"<li>{esc(n)}</li>")
            html.append("</ul>")
        return "".join(html)

    # --- actions ------------------------------------------------------
    def _device(self):
        return self.diagnosis.device if self.diagnosis else None

    def _reboot(self, target: str) -> None:
        device = self._device()
        if device is None:
            self.log.append("Run the diagnosis first so I know which phone to talk to.")
            return
        self.reboot_requested.emit(device, target)

    def _extract(self) -> None:
        device = self._device()
        if device is None:
            self.log.append("Run the diagnosis first.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Where should rescued files go?")
        if folder:
            self.extract_requested.emit(device, folder)

    def _sideload(self) -> None:
        device = self._device()
        if device is None:
            self.log.append("Run the diagnosis first.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose the OTA update package", "", "Update packages (*.zip)")
        if path:
            self.log.append(
                "Put the phone in recovery and choose 'Apply update from ADB' before this starts."
            )
            self.sideload_requested.emit(device, path)

    # --- Nokia --------------------------------------------------------
    def _nokia(self, action: str) -> None:
        device = self._device()
        if device is None:
            self.log.append("Run the diagnosis first.")
            return
        self.nokia_requested.emit(device, action)

    def _nokia_guided(self) -> None:
        self._nokia("guided")

    def _nokia_logs(self) -> None:
        self._nokia("logs")

    def _nokia_switch_slot(self) -> None:
        self._nokia("switch-slot")

    def _nokia_find_ota(self) -> None:
        self._nokia("find-ota")

    def _nokia_fix_bootloop(self) -> None:
        self._nokia("fix-bootloop")

    def _nokia_undo_slot(self) -> None:
        self._nokia("undo-slot")

    def on_event(self, e: Event) -> None:
        if e.message:
            self.log.append(e.message)

    def append(self, text: str) -> None:
        self.log.append(text)
