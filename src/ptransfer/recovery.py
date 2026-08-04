"""Soft-brick diagnosis, rescue, and last-ditch data extraction.

The honest physics of this, which the whole module is built around:

Since Android 10 every phone ships with file-based encryption. The key for
your photos and messages is wrapped by your screen lock and only unwrapped
*after* Android boots and you unlock once. A phone stuck at the logo has never
unwrapped it. So a PC connected to a dead phone is looking at ciphertext, and
no tool - this one, or any of the paid ones that advertise otherwise - can read
it out.

That means the route to the data on a soft-bricked phone is always: repair the
boot without wiping userdata, let Android start, then back up immediately.
This module is organised in exactly that order, and it tries the extraction
path anyway (older FDE phones, unencrypted storage, custom recoveries do
sometimes give it up) while telling you honestly what it found.
"""

from __future__ import annotations

import logging
import posixpath
import time
from dataclasses import dataclass, field
from pathlib import Path

from .adb import Adb
from .devices import Device, DeviceManager, State
from .fastboot import Fastboot
from .oem import DataSafety, RescueStep, VendorProfile, profile_for, profile_for_usb_vendor
from .proc import Cancel
from .progress import Reporter

log = logging.getLogger(__name__)


@dataclass
class Diagnosis:
    device: Device | None
    profile: VendorProfile
    situation: str
    severity: str  # "fine" | "soft-brick" | "deep" | "unknown"
    explanation: str
    data_reachable: bool
    steps: list[RescueStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        out = [f"Situation: {self.situation}", "", self.explanation, ""]
        if self.device is not None:
            out.append(f"Device: {self.device.summary()}")
            if self.device.note:
                out.append(f"Note: {self.device.note}")
            out.append("")
        out.append(f"Brand guide: {self.profile.display_name}")
        out.append("")
        out.append("What to do, in order:")
        for i, step in enumerate(self.steps, 1):
            flag = "  [WIPES DATA]" if step.warns else ""
            out.append(f"  {i}. {step.title}{flag}")
            out.append(f"     {step.detail}")
        if self.profile.key_combos:
            out.append("")
            out.append("Key combinations for this brand:")
            for c in self.profile.key_combos:
                out.append(f"  {c.mode}: {c.steps}")
                if c.note:
                    out.append(f"      {c.note}")
        if self.profile.tools:
            out.append("")
            out.append("Vendor tools:")
            for t in self.profile.tools:
                safety = {"safe": "data-safe", "usually": "usually keeps data", "wipes": "ERASES DATA"}[t.data_safety.value]
                out.append(f"  {t.name} ({safety})")
                out.append(f"      {t.purpose}")
                if t.url:
                    out.append(f"      {t.url}")
                if t.note:
                    out.append(f"      {t.note}")
        if self.warnings:
            out.append("")
            out.append("Warnings:")
            out.extend(f"  ! {w}" for w in self.warnings)
        return "\n".join(out)


ENCRYPTION_EXPLANATION = (
    "Android encrypts your files with a key that is only unlocked once the phone boots and you "
    "enter your PIN. While it will not boot, the storage is unreadable to any PC - so the goal is "
    "to repair the boot without erasing anything, then back up the moment Android starts."
)


def diagnose(devices: list[Device], hint_brand: str = "") -> Diagnosis:
    """Work out what state the phone is in and what to do about it."""
    if not devices:
        profile = profile_for(hint_brand)
        return Diagnosis(
            device=None,
            profile=profile,
            situation="Nothing detected on USB",
            severity="unknown",
            explanation=(
                "Windows is not reporting any phone at all. Before assuming the worst: this is far "
                "more often a cable or a driver than a dead phone. Charge-only cables are extremely "
                "common and look identical to data cables."
            ),
            data_reachable=False,
            steps=[
                RescueStep(
                    "Try a different USB cable - a data one",
                    "Use the cable that came with the phone, or one you have moved files with before. "
                    "Cables that only charge are the single most common cause of 'my PC cannot see it'.",
                ),
                RescueStep(
                    "Use a USB port on the back of the PC",
                    "Front-panel ports and hubs drop out under the sustained current a phone in flash "
                    "mode draws.",
                ),
                RescueStep(
                    "Charge it for 30 minutes and try the force-restart combo",
                    profile.combo("force-restart").steps if profile.combo("force-restart") else
                    "Hold Power for 20 seconds.",
                ),
                *profile.rescue_steps,
            ],
            warnings=["No device on USB - the steps below assume the phone is genuinely connected."],
        )

    device = _most_interesting(devices)
    profile = _profile_for_device(device, hint_brand)

    if device.state is State.ONLINE:
        return Diagnosis(
            device=device,
            profile=profile,
            situation="Phone is booted and reachable",
            severity="fine",
            explanation=(
                "Android is running and USB debugging is authorised. Back it up now - if the phone "
                "has been misbehaving, this window may not stay open."
            ),
            data_reachable=True,
            steps=[
                RescueStep(
                    "Back up immediately",
                    "Run a full backup before troubleshooting anything else.",
                    action="backup-now",
                )
            ],
        )

    if device.state is State.UNAUTHORIZED:
        return Diagnosis(
            device=device,
            profile=profile,
            situation="Phone is booted, but has not authorised this PC",
            severity="fine",
            explanation=(
                "The phone is healthy. It is waiting for you to approve USB debugging - a security "
                "prompt that only appears while the screen is unlocked."
            ),
            data_reachable=False,
            steps=[
                RescueStep(
                    "Unlock the screen and tap 'Allow'",
                    "Tick 'Always allow from this computer' so it does not ask again.",
                ),
                RescueStep(
                    "No prompt? Revoke and retry",
                    "Settings > Developer options > Revoke USB debugging authorisations, then unplug "
                    "and replug the cable.",
                    action="scan",
                ),
                RescueStep(
                    "Still nothing? Restart the adb server",
                    "Some Windows USB stacks wedge the adb daemon; restarting it clears that.",
                    action="restart-adb",
                ),
            ],
        )

    if device.state in (State.RECOVERY, State.SIDELOAD):
        return Diagnosis(
            device=device,
            profile=profile,
            situation="Phone is in recovery mode",
            severity="soft-brick",
            explanation=(
                "The phone's rescue partition works, which is good news: the hardware is fine and "
                "only the Android system is broken. "
                + ENCRYPTION_EXPLANATION
            ),
            data_reachable=False,
            steps=[
                RescueStep(
                    "Try reading storage from recovery anyway",
                    "Worth one attempt: older phones and some custom recoveries will hand the files "
                    "over. This app will tell you plainly if the storage comes back encrypted.",
                    action="extract-from-recovery",
                ),
                RescueStep(
                    "Wipe the cache partition",
                    "In the recovery menu choose 'Wipe cache partition'. This does not touch your "
                    "files and often fixes a boot loop caused by a failed update.",
                ),
                RescueStep(
                    "Reboot and see if it starts",
                    "If it boots, back up straight away.",
                    action="reboot-system",
                ),
                RescueStep(
                    "Sideload a full OTA if you have the signed zip",
                    "Recovery > 'Apply update from ADB', then point this app at the zip. A full OTA "
                    "rebuilds the system and keeps userdata.",
                    action="sideload",
                ),
                *[s for s in profile.rescue_steps if s.data_safety is not DataSafety.SAFE],
            ],
            warnings=[
                "Do not choose 'Wipe data/factory reset' in that menu. It erases exactly what you "
                "are trying to save, and it is one menu item away from the safe options.",
            ],
        )

    if device.state is State.BOOTLOADER:
        unlocked = (device.fastboot_vars.get("unlocked") or "").lower()
        warn = []
        if unlocked in ("no", "false"):
            warn.append(
                "Bootloader is locked. Stock signed firmware still flashes fine; do not unlock it, "
                "because unlocking forces a full wipe."
            )
        return Diagnosis(
            device=device,
            profile=profile,
            situation="Phone is in its bootloader (fastboot)",
            severity="soft-brick",
            explanation=(
                "The bootloader is alive and talking, so this is a software problem, not a dead "
                "phone. " + ENCRYPTION_EXPLANATION
            ),
            data_reachable=False,
            steps=[
                RescueStep(
                    "Just try booting it",
                    "Some phones sit in fastboot after an interrupted update and boot fine when told to.",
                    action="reboot-system",
                ),
                RescueStep(
                    "Boot into recovery from here",
                    "Recovery gives you the data-safe cache wipe and OTA sideload options.",
                    action="reboot-recovery",
                ),
                RescueStep(
                    "Reflash the system partitions with stock firmware - never userdata",
                    "Use the vendor tool listed below in its 'keep data' mode. This app refuses to "
                    "flash userdata for you, on purpose.",
                    DataSafety.USUALLY_SAFE,
                ),
                *[s for s in profile.rescue_steps if s.action in ("", "backup-now")],
            ],
            warnings=warn,
        )

    if device.state is State.LOW_LEVEL:
        mode = device.usb.mode() if device.usb else None
        detail = mode.detail if mode else "an unrecognised low-level mode"
        action = mode.actionable if mode else ""
        return Diagnosis(
            device=device,
            profile=profile,
            situation=f"Phone is in {detail}",
            severity="deep",
            explanation=(
                "The phone is powered and its chip-level bootloader is responding, so it is not "
                "dead - but this mode is below the level adb and fastboot work at. "
                + (action + " " if action else "")
                + ENCRYPTION_EXPLANATION
            ),
            data_reachable=False,
            steps=[
                RescueStep(
                    "Try the force-restart combo first",
                    profile.combo("force-restart").steps if profile.combo("force-restart") else
                    "Hold Power for 20 seconds.",
                    action="force-restart",
                ),
                RescueStep(
                    "Get it into fastboot or recovery if you can",
                    (profile.combo("fastboot").steps if profile.combo("fastboot") else
                     "Power off, hold Volume Down, connect USB.")
                    + " From there the data-safe repairs become available.",
                ),
                *profile.rescue_steps[2:],
            ],
            warnings=[
                "This is where people get scammed. 'Unbrick' services and leaked service tools for "
                "this mode routinely factory-reset the phone as step one. Ask what happens to "
                "userdata before you hand money over.",
            ],
        )

    if device.state is State.OFFLINE:
        return Diagnosis(
            device=device,
            profile=profile,
            situation="Phone is detected but not responding to adb",
            severity="soft-brick",
            explanation=(
                "Windows sees the phone but the debug bridge will not connect. Usually a stale adb "
                "daemon or a half-booted system rather than a real brick."
            ),
            data_reachable=False,
            steps=[
                RescueStep("Restart the adb server", "Clears a wedged daemon on the PC side.", action="restart-adb"),
                RescueStep("Unplug, force-restart the phone, plug back in", "", action="force-restart"),
                RescueStep("Rescan", "", action="scan"),
                *profile.rescue_steps[3:],
            ],
        )

    return Diagnosis(
        device=device,
        profile=profile,
        situation="Phone is in an unrecognised state",
        severity="unknown",
        explanation="Falling back to the general rescue sequence for this brand.",
        data_reachable=False,
        steps=list(profile.rescue_steps),
    )


@dataclass
class ExtractionResult:
    succeeded: bool
    message: str
    files_pulled: int = 0
    destination: str = ""
    encrypted: bool = False


class RescueEngine:
    """The actions a :class:`Diagnosis` step can actually run."""

    def __init__(
        self,
        manager: DeviceManager,
        reporter: Reporter | None = None,
        cancel: Cancel | None = None,
    ) -> None:
        self.manager = manager
        self.report = reporter or Reporter()
        self.cancel = cancel or Cancel()

    # --- simple actions ----------------------------------------------
    def restart_adb(self) -> str:
        adb = self.manager.adb
        adb.kill_server()
        time.sleep(1)
        adb.start_server()
        return "adb server restarted."

    def reboot(self, device: Device, target: str = "") -> str:
        if device.state is State.BOOTLOADER:
            res = self.manager.fastboot.reboot(target, device.serial or None)
        else:
            res = self.manager.adb.reboot(target, device.serial or None)
        where = target or "system"
        return f"Reboot to {where} requested." if res.ok else f"Reboot failed: {res.output.strip()[:200]}"

    def sideload(self, device: Device, zip_path: str) -> str:
        p = Path(zip_path)
        if not p.exists():
            return f"{zip_path} does not exist."
        if p.suffix.lower() != ".zip":
            return "An OTA package is a .zip file. Pick the full OTA, not a factory image archive."
        self.report.status(f"Sideloading {p.name} - do not unplug the phone")
        res = self.manager.adb.sideload(
            str(p),
            device.serial or None,
            on_line=lambda ln: self.report.status(ln.strip()[-90:]),
            cancel=self.cancel,
        )
        if res.ok:
            return "Sideload finished. Choose 'Reboot system now' on the phone."
        return f"Sideload failed: {res.output.strip()[-300:]}"

    # --- the interesting one -----------------------------------------
    def extract_from_recovery(self, device: Device, dest: str | Path) -> ExtractionResult:
        """Try to read user storage while the phone sits in recovery.

        This usually fails on a modern phone, and it fails for a good reason.
        The point is to try honestly and explain the result rather than leave
        someone wondering whether another tool would have done better.
        """
        adb = self.manager.adb
        serial = device.serial or None
        dest = Path(dest)

        self.report.start_section("rescue", "Attempting to read storage from recovery")

        probe = adb.shell("echo ok", serial, timeout=20)
        if "ok" not in probe.output:
            return ExtractionResult(
                False,
                "This recovery does not provide an adb shell, so there is nothing to read from. "
                "Repairing the boot is the only route to the data.",
            )

        # Recovery usually leaves /data unmounted.
        adb.shell("mount /data 2>/dev/null", serial, timeout=30)
        adb.shell("mount -o rw /data 2>/dev/null", serial, timeout=30)

        source = "/data/media/0"
        listing = adb.shell(f"ls {source} 2>&1", serial, timeout=60)
        out = listing.output.strip()

        if "No such file" in out or "Permission denied" in out or not out:
            source = "/sdcard"
            listing = adb.shell(f"ls {source} 2>&1", serial, timeout=30)
            out = listing.output.strip()
            if "No such file" in out or "Permission denied" in out or not out:
                return ExtractionResult(
                    False,
                    "Storage is not mounted in this recovery and cannot be mounted from here. "
                    "That is normal for a stock recovery.",
                )

        if _looks_encrypted(out):
            return ExtractionResult(
                False,
                "Storage mounted, but the file names come back as encrypted gibberish - this is "
                "file-based encryption doing its job. The files are intact and will be perfectly "
                "readable once the phone boots and you unlock it once. No PC tool can decrypt them "
                "from here; anything that claims otherwise is selling you something.",
                encrypted=True,
            )

        self.report.status(f"Storage is readable. Copying {source} - do not unplug.")
        dest.mkdir(parents=True, exist_ok=True)

        pulled = 0
        entries = [e.strip() for e in out.splitlines() if e.strip()]
        for i, entry in enumerate(entries, 1):
            self.cancel.raise_if_cancelled()
            remote = posixpath.join(source, entry)
            self.report.progress(i / max(1, len(entries)), f"copying {entry}", done_items=i, total_items=len(entries))
            res = adb.pull(remote, str(dest / entry), serial, timeout=None)
            if res.ok:
                pulled += 1

        if pulled == 0:
            return ExtractionResult(
                False,
                "Storage listed but nothing could be copied - recovery has read permission but the "
                "data is not accessible.",
                destination=str(dest),
            )
        return ExtractionResult(
            True,
            f"Rescued {pulled} of {len(entries)} top-level folders from recovery into {dest}. "
            "Check the contents before you factory-reset the phone.",
            files_pulled=pulled,
            destination=str(dest),
        )


def _looks_encrypted(listing: str) -> bool:
    """FBE-encrypted names are long base64-ish strings with no extensions."""
    names = [n.strip() for n in listing.split() if n.strip()]
    if not names:
        return False
    familiar = {"DCIM", "Download", "Pictures", "Movies", "Music", "Android", "Documents", "WhatsApp", "Downloads"}
    if any(n in familiar for n in names):
        return False
    odd = 0
    for n in names:
        if len(n) < 16 or "." in n:
            continue
        stripped = n.replace("-", "").replace("_", "").replace("+", "").replace(",", "")
        # Mixed letters *and* digits, no extension, long: that is a wrapped name.
        if stripped.isalnum() and not stripped.isalpha() and not stripped.isdigit():
            odd += 1
    return odd >= max(1, len(names) // 2)


def _most_interesting(devices: list[Device]) -> Device:
    order = {
        State.ONLINE: 0,
        State.RECOVERY: 1,
        State.SIDELOAD: 1,
        State.BOOTLOADER: 2,
        State.LOW_LEVEL: 3,
        State.UNAUTHORIZED: 4,
        State.OFFLINE: 5,
        State.UNKNOWN: 6,
    }
    return sorted(devices, key=lambda d: order.get(d.state, 9))[0]


def _profile_for_device(device: Device, hint_brand: str) -> VendorProfile:
    if hint_brand:
        return profile_for(hint_brand)
    if device.vendor_key:
        return profile_for(device.vendor_key)
    if device.usb is not None:
        p = profile_for_usb_vendor(device.usb.vid)
        if p is not None:
            return p
    return profile_for("")
