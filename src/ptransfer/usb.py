"""Raw USB enumeration.

adb and fastboot only see a phone that is already running Android or a
bootloader. A soft-bricked phone often sits in a *lower* mode - Sony flash
mode, Qualcomm EDL, MediaTek preloader - which those tools cannot list at all.
Reading the USB VID/PID directly is what turns "nothing is detected" into
"your phone is in Sony flash mode and the driver is missing".
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from .proc import Runner, ToolError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsbMode:
    name: str
    detail: str
    # What the user can actually do from this mode.
    actionable: str


# (vid, pid) -> mode. pid of None means "any pid from this vendor".
KNOWN_MODES: dict[tuple[str, str | None], UsbMode] = {
    ("0fce", "adde"): UsbMode(
        "sony-flash",
        "Sony Xperia flash mode (S1 Service / green LED)",
        "Repairable with Xperia Companion 'Software repair', or by flashing an FTF "
        "with the userdata partition excluded so your files survive.",
    ),
    ("0fce", "0dde"): UsbMode(
        "sony-fastboot",
        "Sony Xperia S1Boot fastboot (blue LED)",
        "fastboot works here. Boot/flash system images; do NOT erase userdata.",
    ),
    ("0fce", "b00b"): UsbMode("sony-mtp", "Sony Xperia in MTP mode", "Phone is booted; enable USB debugging."),
    ("05c6", "9008"): UsbMode(
        "qualcomm-edl",
        "Qualcomm Emergency Download mode (9008)",
        "Deepest recovery level. Needs the model's signed firehose loader, which "
        "only the manufacturer or an authorised service centre has. Not something "
        "this app can or should do for you.",
    ),
    ("05c6", "900e"): UsbMode("qualcomm-diag", "Qualcomm diagnostic mode", "Usually a half-booted modem; try a forced reboot."),
    ("0e8d", "0003"): UsbMode(
        "mediatek-brom",
        "MediaTek BROM mode (chip-level bootloader)",
        "Many Nokia models are MediaTek. Recovery from here needs the model's "
        "scatter/DA files - Nokia's own service tool, not a third-party flasher.",
    ),
    ("0e8d", "2000"): UsbMode(
        "mediatek-preloader",
        "MediaTek preloader",
        "The phone is alive at the lowest level. Vendor recovery tool required.",
    ),
    ("18d1", "4ee0"): UsbMode("android-fastboot", "Android fastboot (Google class)", "fastboot works here."),
    ("18d1", "d00d"): UsbMode("android-fastboot", "Android fastboot (Google class)", "fastboot works here."),
    ("04e8", "685d"): UsbMode(
        "samsung-download",
        "Samsung download/Odin mode",
        "Flash the matching stock firmware without the userdata (CSC HOME) part to keep your files.",
    ),
    ("2e04", None): UsbMode("nokia", "Nokia (HMD Global) device", "See the Nokia section of the rescue guide."),
    ("0421", None): UsbMode("nokia-legacy", "Nokia (legacy Nokia Corp. IDs)", "See the Nokia section of the rescue guide."),
}


@dataclass
class UsbDevice:
    vid: str
    pid: str
    description: str = ""
    instance_id: str = ""
    problem: str = ""  # non-empty when Windows reports a driver problem
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.vid.lower(), self.pid.lower())

    def mode(self) -> UsbMode | None:
        m = KNOWN_MODES.get(self.key)
        if m:
            return m
        return KNOWN_MODES.get((self.vid.lower(), None))

    @property
    def has_driver_problem(self) -> bool:
        return bool(self.problem)

    def __str__(self) -> str:
        mode = self.mode()
        label = mode.detail if mode else (self.description or "unknown device")
        return f"{self.vid}:{self.pid} {label}"


_WIN_ID = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})")
_LSUSB = re.compile(r"ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)")

# Windows CM_PROB_* codes worth explaining in plain English.
WINDOWS_PROBLEM_CODES = {
    "1": "Device is not configured correctly - reinstall the driver.",
    "3": "Driver may be corrupted, or the system is low on memory.",
    "10": "Device cannot start - usually the wrong driver for this mode.",
    "18": "Drivers need to be reinstalled.",
    "19": "Registry configuration for this device is damaged.",
    "28": "No driver installed for this device.",
    "31": "Windows cannot load the driver for this device.",
    "43": "Windows stopped the device because it reported a problem.",
}

POWERSHELL_QUERY = (
    "Get-CimInstance Win32_PnPEntity | "
    "Where-Object { $_.PNPDeviceID -like 'USB*' } | "
    "Select-Object Name,PNPDeviceID,Status,ConfigManagerErrorCode | "
    "ConvertTo-Json -Compress"
)


def enumerate_usb(runner: Runner | None = None) -> list[UsbDevice]:
    runner = runner or Runner()
    if os.name == "nt":
        return _enumerate_windows(runner)
    return _enumerate_linux(runner)


def _enumerate_windows(runner: Runner) -> list[UsbDevice]:  # pragma: no cover - needs Windows
    try:
        res = runner.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_QUERY],
            timeout=60,
        )
    except ToolError:
        return []
    if not res.ok or not res.stdout.strip():
        return []
    return parse_windows_pnp(res.stdout)


def parse_windows_pnp(text: str) -> list[UsbDevice]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out: list[UsbDevice] = []
    for entry in data:
        pnp_id = str(entry.get("PNPDeviceID") or "")
        m = _WIN_ID.search(pnp_id)
        if not m:
            continue
        code = entry.get("ConfigManagerErrorCode")
        code_s = str(code) if code not in (None, 0, "0") else ""
        problem = ""
        if code_s:
            problem = WINDOWS_PROBLEM_CODES.get(code_s, f"Windows device error code {code_s}.")
        out.append(
            UsbDevice(
                vid=m.group(1).lower(),
                pid=m.group(2).lower(),
                description=str(entry.get("Name") or ""),
                instance_id=pnp_id,
                problem=problem,
            )
        )
    return out


def _enumerate_linux(runner: Runner) -> list[UsbDevice]:
    try:
        res = runner.run(["lsusb"], timeout=20)
    except ToolError:
        return []
    if not res.ok:
        return []
    return parse_lsusb(res.stdout)


def parse_lsusb(text: str) -> list[UsbDevice]:
    out: list[UsbDevice] = []
    for line in text.splitlines():
        m = _LSUSB.search(line)
        if not m:
            continue
        out.append(
            UsbDevice(
                vid=m.group(1).lower(),
                pid=m.group(2).lower(),
                description=(m.group(3) or "").strip(),
            )
        )
    return out


def interesting(devices: list[UsbDevice]) -> list[UsbDevice]:
    """Devices that look like a phone in some mode we recognise."""
    return [d for d in devices if d.mode() is not None]
