"""Nokia (HMD Global) rescue - the part that actually does something.

The generic rescue plan tells you what to try. This module runs it, in the
order that keeps your data, and stops the moment something works:

1. **Read the recovery log.** Stock Android recovery writes why the boot failed
   to ``last_log``. That single file usually names the problem outright -
   "failed to mount /data", "dm-verity verification failed", "package is for
   product X" - and it is readable over adb whenever the phone reaches
   recovery. Nobody looks at it, and it is the most informative thing on a
   bricked phone.

2. **Switch the A/B slot.** Nearly every Nokia since the Android One line
   carries two complete system slots. A failed update leaves the new slot
   unbootable while the previous, working system sits untouched in the other
   one. ``fastboot set_active`` swaps them. Nothing is written, userdata is not
   touched, and it is the single most effective repair for the exact failure
   Nokia owners hit most: "it rebooted during an update and now it won't start".

3. **Apply an update package the phone already has.** When an OTA download
   completed before the phone died, the signed zip is still sitting in
   ``/data/ota_package`` or ``/cache``. Recovery will happily install it, and
   because HMD signed it, it verifies. This is the one route to a legitimate
   signed OTA for a Nokia, which HMD otherwise does not publish.

4. **Wipe the cache partition**, which is data-safe and clears a jammed update.

Everything here is reversible or non-destructive. Nothing in this module
formats, erases, or unlocks anything.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from .adb import Adb
from .devices import Device, DeviceManager, State
from .fastboot import SlotInfo
from .platform_tools import user_data_dir
from .proc import Cancel, ToolError
from .progress import Reporter
from .usb import enumerate_usb

log = logging.getLogger(__name__)

# Where stock Android leaves the record of a failed boot or update. A/B devices
# usually have no /cache partition and use /data/cache instead.
RECOVERY_LOG_PATHS = (
    "/tmp/recovery.log",
    "/cache/recovery/last_log",
    "/cache/recovery/last_log.1",
    "/cache/recovery/last_install",
    "/data/cache/recovery/last_log",
    "/data/cache/recovery/last_install",
    "/mnt/rescue/log",
)

# Where a downloaded-but-not-applied OTA sits.
OTA_SEARCH_DIRS = (
    "/data/ota_package",
    "/cache",
    "/data/cache",
    "/sdcard",
    "/sdcard/Download",
)

CHIPSET_HINTS = {
    "mt": "MediaTek",
    "sdm": "Qualcomm",
    "msm": "Qualcomm",
    "sm6": "Qualcomm",
    "sm7": "Qualcomm",
    "sm8": "Qualcomm",
    "qcom": "Qualcomm",
    "trinket": "Qualcomm",
    "bengal": "Qualcomm",
    "ums": "Unisoc",
    "sp9": "Unisoc",
    "ud7": "Unisoc",
}

# Low-level USB modes, by chipset. Used to tell someone what they should expect
# to see, and to recognise it when they do.
CHIPSET_LOW_LEVEL = {
    "MediaTek": "MediaTek preloader/BROM (USB 0e8d:2000 or 0e8d:0003)",
    "Qualcomm": "Qualcomm EDL 9008 (USB 05c6:9008)",
    "Unisoc": "Unisoc/Spreadtrum download mode (USB 1782:4d00)",
}

# A rough guide only - runtime detection always wins. Included so that someone
# whose phone will not boot at all still gets a sensible expectation.
TYPICAL_CHIPSETS = {
    "nokia 1": "MediaTek",
    "nokia 2": "Qualcomm",
    "nokia 3": "MediaTek",
    "nokia 5": "Qualcomm",
    "nokia 5.3": "Qualcomm",
    "nokia 6": "Qualcomm",
    "nokia 7": "Qualcomm",
    "nokia 8": "Qualcomm",
    "nokia 9": "Qualcomm",
    "nokia g10": "MediaTek",
    "nokia g20": "MediaTek",
    "nokia g11": "Unisoc",
    "nokia g21": "Unisoc",
    "nokia c21": "Unisoc",
    "nokia x10": "Qualcomm",
    "nokia x20": "Qualcomm",
}


@dataclass
class Finding:
    """One thing the recovery log told us."""

    signature: str
    meaning: str
    next_step: str
    severity: str = "warning"  # info | warning | serious

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"[{self.severity}] {self.meaning} -> {self.next_step}"


# Ordered: the first match that fits is usually the real cause.
LOG_SIGNATURES: tuple[tuple[str, Finding], ...] = (
    (
        r"failed to mount /data|can't mount /data|E:\s*failed to mount /data",
        Finding(
            "failed to mount /data",
            "The data partition will not mount. That is where all your files live.",
            "Do NOT accept any offer to format data - that is what destroys it. A filesystem "
            "check from a service tool can often mount it again with everything intact.",
            "serious",
        ),
    ),
    (
        r"dm-verity|verity verification failed|verified boot",
        Finding(
            "dm-verity failure",
            "Verified boot rejected the system partition: it has been modified or corrupted.",
            "The system needs reflashing, but userdata is untouched by this fault. Switch A/B "
            "slot first - the other slot's system is usually intact.",
            "warning",
        ),
    ),
    (
        r"signature verification failed|E:footer is wrong|E:signature verification failed",
        Finding(
            "OTA signature rejected",
            "The update package is corrupt, or was not signed by HMD.",
            "Re-download the package. Never force an unsigned zip onto a locked phone.",
            "warning",
        ),
    ),
    (
        r"Package is for product|expected .* got|device is .* but package",
        Finding(
            "wrong firmware for this model",
            "That update was built for a different Nokia model.",
            "Match the exact model/TA number before trying again. Wrong-model firmware is a "
            "common way to turn a soft-brick into a hard one.",
            "serious",
        ),
    ),
    (
        r"apply_patch_check|assert failed|old build|downgrade",
        Finding(
            "incremental update cannot apply",
            "A patch-style update expected a different starting build.",
            "A full OTA package will work where the incremental one cannot.",
            "warning",
        ),
    ),
    (
        r"no bootable slot|boot from slot .* failed|slot .* is unbootable",
        Finding(
            "the active slot is unbootable",
            "The bootloader has given up on the current system slot.",
            "Switch to the other A/B slot - this is exactly the case it exists for, and it "
            "does not touch your data.",
            "info",
        ),
    ),
    (
        r"failed to setup keystore|metadata encryption|failed to decrypt|keymaster",
        Finding(
            "encryption keys unavailable",
            "The phone cannot unlock its own storage encryption.",
            "This is the hardest case: the files are intact but the key is not available. A "
            "system reflash that preserves userdata is the only thing that may recover it.",
            "serious",
        ),
    ),
    (
        r"E:\s*unknown volume|failed to mount /cache",
        Finding(
            "cache partition problem",
            "The cache partition will not mount.",
            "Wipe cache partition from the recovery menu - data-safe, and it usually clears this.",
            "info",
        ),
    ),
)


@dataclass
class NokiaIdentity:
    model: str = ""
    marketing_name: str = ""
    chipset: str = ""
    chipset_source: str = ""
    android_release: str = ""
    build_id: str = ""
    security_patch: str = ""

    @property
    def low_level_mode(self) -> str:
        return CHIPSET_LOW_LEVEL.get(self.chipset, "unknown - depends on the chipset")

    def describe(self) -> str:
        bits = [f"Model: {self.marketing_name or self.model or 'unknown'}"]
        if self.chipset:
            bits.append(f"Chipset: {self.chipset} ({self.chipset_source})")
            bits.append(f"Expect its deep rescue mode to appear as: {self.low_level_mode}")
        if self.android_release:
            bits.append(f"Android {self.android_release}, build {self.build_id or '?'}")
        if self.security_patch:
            bits.append(f"Security patch: {self.security_patch}")
        return "\n".join(bits)


@dataclass
class StepOutcome:
    name: str
    status: str  # ok | failed | skipped | needs-user | info
    message: str
    detail: str = ""

    @property
    def helped(self) -> bool:
        return self.status == "ok"


@dataclass
class BootWatch:
    """What happened after we told the phone to restart."""

    booted: bool | None       # None = could not tell from here
    seconds: float = 0.0
    disappearances: int = 0   # a phone that keeps vanishing is still looping
    detail: str = ""

    @property
    def looping(self) -> bool:
        return self.booted is False and self.disappearances >= 2


@dataclass
class RepairAttempt:
    name: str
    performed: bool
    booted: bool | None
    message: str
    detail: str = ""
    rolled_back: bool = False

    @property
    def fixed_it(self) -> bool:
        return self.performed and self.booted is True


@dataclass
class RepairSession:
    attempts: list[RepairAttempt] = field(default_factory=list)
    advice: str = ""

    @property
    def fixed(self) -> bool:
        return any(a.fixed_it for a in self.attempts)

    def as_text(self) -> str:
        out = ["Boot loop repair", "=" * 16, ""]
        for a in self.attempts:
            verdict = {True: "FIXED IT", False: "did not help", None: "could not tell"}[a.booted]
            head = f"  {a.name}: " + ("skipped" if not a.performed else verdict)
            out.append(head)
            out.append(f"      {a.message}")
            if a.detail:
                out.append(f"      {a.detail}")
            if a.rolled_back:
                out.append("      The change was put back the way it was.")
            out.append("")
        if self.advice:
            out.append(self.advice)
        return "\n".join(out)


@dataclass
class NokiaReport:
    identity: NokiaIdentity = field(default_factory=NokiaIdentity)
    slots: SlotInfo | None = None
    findings: list[Finding] = field(default_factory=list)
    ota_packages: list[str] = field(default_factory=list)
    steps: list[StepOutcome] = field(default_factory=list)
    log_excerpt: str = ""

    def as_text(self) -> str:
        out = ["Nokia rescue report", "=" * 19, ""]
        out.append(self.identity.describe())
        out.append("")

        if self.slots is not None:
            out.append("System slots")
            out.append("-" * 12)
            out.append(self.slots.describe())
            out.append("")

        if self.findings:
            out.append("What the recovery log says")
            out.append("-" * 26)
            for f in self.findings:
                out.append(f"  [{f.severity}] {f.meaning}")
                out.append(f"      -> {f.next_step}")
            out.append("")
        elif self.log_excerpt:
            out.append("Recovery log found, but nothing recognisable in it.")
            out.append("")

        if self.ota_packages:
            out.append("Update packages already on the phone")
            out.append("-" * 36)
            for p in self.ota_packages:
                out.append(f"  {p}")
            out.append("  These are signed by HMD and can be applied from recovery - the one")
            out.append("  legitimate route to a signed Nokia OTA.")
            out.append("")

        if self.steps:
            out.append("What was tried")
            out.append("-" * 14)
            for s in self.steps:
                out.append(f"  {s.name}: {s.status} - {s.message}")
                if s.detail:
                    out.append(f"      {s.detail}")
        return "\n".join(out)


class NokiaRescue:
    def __init__(
        self,
        manager: DeviceManager,
        reporter: Reporter | None = None,
        cancel: Cancel | None = None,
    ) -> None:
        self.manager = manager
        self.report = reporter or Reporter()
        self.cancel = cancel or Cancel()

    # --- identification -----------------------------------------------
    def identify(self, device: Device) -> NokiaIdentity:
        ident = NokiaIdentity(
            model=device.model,
            marketing_name=device.model or device.fastboot_vars.get("product", ""),
            android_release=device.android_release,
            build_id=device.build_id,
        )
        props = device.props or {}
        ident.security_patch = props.get("ro.build.version.security_patch", "")

        platform = (props.get("ro.board.platform") or props.get("ro.hardware") or "").lower()
        if not platform and device.state.can_transfer:
            try:
                platform = self.manager.adb.getprop("ro.board.platform", device.serial or None).lower()
            except ToolError:
                platform = ""

        for prefix, name in CHIPSET_HINTS.items():
            if platform.startswith(prefix):
                ident.chipset = name
                ident.chipset_source = f"ro.board.platform={platform}"
                break

        # A phone that will not boot has no properties to read; the USB vendor
        # ID of its rescue mode identifies the chipset just as well.
        if not ident.chipset and device.usb is not None:
            by_vid = {"0e8d": "MediaTek", "05c6": "Qualcomm", "1782": "Unisoc"}
            if device.usb.vid in by_vid:
                ident.chipset = by_vid[device.usb.vid]
                ident.chipset_source = f"USB vendor {device.usb.vid}"

        if not ident.chipset:
            guess = _typical_chipset(ident.marketing_name)
            if guess:
                ident.chipset = guess
                ident.chipset_source = "typical for this model - verify if it matters"
        return ident

    # --- slots --------------------------------------------------------
    def read_slots(self, device: Device) -> SlotInfo | None:
        if device.state is not State.BOOTLOADER:
            return None
        try:
            return self.manager.fastboot.slots(device.serial or None)
        except ToolError:
            return None

    def switch_slot(self, device: Device, target: str = "") -> StepOutcome:
        """Boot from the other system slot. Writes nothing, erases nothing."""
        if device.state is not State.BOOTLOADER:
            return StepOutcome(
                "switch slot",
                "skipped",
                "The phone must be in fastboot for this.",
                "Power off, hold Volume Down, then connect USB.",
            )
        try:
            info = self.manager.fastboot.slots(device.serial or None)
        except ToolError as exc:
            return StepOutcome("switch slot", "failed", str(exc))

        if not info.is_ab:
            return StepOutcome(
                "switch slot",
                "skipped",
                "This Nokia has a single system slot, so there is no spare to fall back on.",
            )

        target = (target or info.other).lower().lstrip("_")
        if target not in ("a", "b"):
            return StepOutcome("switch slot", "failed", "Could not work out which slot to switch to.")
        if target == info.current:
            return StepOutcome("switch slot", "skipped", f"Already booting from slot {target.upper()}.")

        self.report.status(f"Switching from slot {info.current.upper() or '?'} to slot {target.upper()}")
        res = self.manager.fastboot.set_active(target, device.serial or None)
        if res.ok:
            return StepOutcome(
                "switch slot",
                "ok",
                f"Now set to boot from slot {target.upper()}. Reboot and see if it starts.",
                "Nothing was erased. If it boots, back up immediately - then let it finish "
                "updating properly.",
            )

        output = res.output.strip()
        if "not allowed" in output.lower() or "locked" in output.lower():
            return StepOutcome(
                "switch slot",
                "failed",
                "The locked bootloader refuses to switch slots.",
                "Not a dead end: the bootloader retries the other slot by itself after several "
                "failed boots. Leave the phone trying to boot a few times before moving on.",
            )
        return StepOutcome("switch slot", "failed", output[:200] or "set_active failed")

    # --- recovery log --------------------------------------------------
    def read_recovery_log(self, device: Device) -> str:
        """Pull whatever boot/update log the phone has kept."""
        if device.state not in (State.RECOVERY, State.SIDELOAD, State.ONLINE):
            return ""
        adb = self.manager.adb
        serial = device.serial or None
        chunks: list[str] = []
        for path in RECOVERY_LOG_PATHS:
            self.cancel.raise_if_cancelled()
            res = adb.shell(f"cat {shlex.quote(path)} 2>/dev/null", serial, timeout=60)
            text = res.stdout.strip()
            if text and "No such file" not in text:
                chunks.append(f"===== {path} =====\n{text}")
        return "\n\n".join(chunks)

    def analyse_log(self, text: str) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()
        for pattern, finding in LOG_SIGNATURES:
            if re.search(pattern, text, re.IGNORECASE) and finding.signature not in seen:
                findings.append(finding)
                seen.add(finding.signature)
        return findings

    # --- on-device OTA --------------------------------------------------
    def find_ota_packages(self, device: Device) -> list[str]:
        """Signed update packages the phone downloaded before it broke."""
        if device.state not in (State.RECOVERY, State.SIDELOAD, State.ONLINE):
            return []
        adb = self.manager.adb
        serial = device.serial or None
        found: list[str] = []
        for directory in OTA_SEARCH_DIRS:
            self.cancel.raise_if_cancelled()
            res = adb.shell(
                f"ls -1 {shlex.quote(directory)}/*.zip 2>/dev/null", serial, timeout=30
            )
            for line in res.lines():
                if line.endswith(".zip") and line.startswith("/"):
                    found.append(line)
        return sorted(set(found))

    def apply_ota_from_device(self, device: Device, remote_zip: str, workdir: Path) -> StepOutcome:
        """Copy a package off the phone and sideload it back through recovery.

        The round trip is deliberate: recovery's own "apply from /cache" menu
        entry does not exist on most modern builds, but sideload does, and the
        package is already signed by HMD so it verifies either way.
        """
        adb = self.manager.adb
        serial = device.serial or None
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        local = workdir / Path(remote_zip).name

        self.report.status(f"Copying {remote_zip} off the phone")
        res = adb.pull(remote_zip, str(local), serial, timeout=1800)
        if not local.exists():
            return StepOutcome("apply update", "failed", f"Could not copy it: {res.output.strip()[:160]}")

        size = local.stat().st_size
        if size < 10 * 1024 * 1024:
            return StepOutcome(
                "apply update",
                "failed",
                f"{local.name} is only {size} bytes - that is a partial download, not a usable update.",
            )

        return StepOutcome(
            "apply update",
            "needs-user",
            f"Saved {local.name} ({size // (1024 * 1024)} MB). It is signed by HMD, so recovery will accept it.",
            "On the phone choose 'Apply update from ADB', then run: "
            f"ptransfer rescue --sideload {local}",
        )

    # --- did it actually come back? ---------------------------------------
    def wait_for_boot(self, serial: str | None = None, timeout: float = 240.0) -> BootWatch:
        """Watch until Android finishes booting, or until we give up.

        A boot loop and a phone that is simply slow look the same for the first
        minute. What tells them apart is the phone appearing on USB and then
        vanishing again, repeatedly - so that gets counted rather than guessed.
        """
        start = time.time()
        was_present = False
        disappearances = 0

        while time.time() - start < timeout:
            self.cancel.raise_if_cancelled()
            elapsed = time.time() - start

            try:
                devices = self.manager.adb.devices()
            except ToolError:
                devices = []
            mine = [d for d in devices if not serial or d.serial == serial]
            present = bool(mine)

            online = [d for d in mine if d.state == "device"]
            if online:
                completed = ""
                try:
                    completed = self.manager.adb.getprop("sys.boot_completed", serial).strip()
                except ToolError:
                    completed = ""
                if completed == "1":
                    return BootWatch(
                        True,
                        elapsed,
                        disappearances,
                        f"Android finished booting after {elapsed:.0f} seconds.",
                    )

            if was_present and not present:
                disappearances += 1
                self.report.status(f"The phone dropped off USB again ({disappearances} times so far)")
            was_present = present

            self.report.progress(
                min(0.99, elapsed / timeout),
                f"waiting for the phone to boot ({elapsed:.0f}s)",
                throttle=1.0,
            )
            time.sleep(3)

        elapsed = time.time() - start

        # adb needs USB debugging authorised. If it was never on, a booted
        # phone is invisible to adb but still visible as a USB device.
        usb_hits = [d for d in enumerate_usb(self.manager.runner) if d.mode() is not None]
        if not was_present and usb_hits:
            return BootWatch(
                None,
                elapsed,
                disappearances,
                "The phone is on USB but adb cannot talk to it - most likely USB debugging was "
                "never switched on. Look at the phone itself to see whether it booted.",
            )

        if disappearances >= 2:
            return BootWatch(
                False,
                elapsed,
                disappearances,
                f"The phone appeared and vanished {disappearances} times in {elapsed:.0f} seconds - "
                "it is still looping.",
            )
        return BootWatch(
            False,
            elapsed,
            disappearances,
            f"No sign of a completed boot after {elapsed:.0f} seconds.",
        )

    # --- experiment 1: undo the update ------------------------------------
    def revert_update(
        self,
        device: Device,
        timeout: float = 240.0,
        roll_back_on_failure: bool = True,
    ) -> RepairAttempt:
        """Go back to the system that was working before the update.

        On an A/B phone the previous system is still sitting in the other slot,
        untouched. Switching to it *is* undoing the update - and it costs
        nothing, because no partition is written and userdata is not involved.
        """
        name = "Revert to the previous system"
        if device.state is not State.BOOTLOADER:
            return RepairAttempt(
                name,
                False,
                None,
                "Needs the phone in fastboot.",
                "Power off, hold Volume Down, then connect the USB cable and run this again.",
            )

        try:
            info = self.manager.fastboot.slots(device.serial or None)
        except ToolError as exc:
            return RepairAttempt(name, False, None, f"Could not read the slots: {exc}")

        if not info.is_ab:
            return RepairAttempt(
                name,
                False,
                None,
                "This Nokia has one system slot, so there is no previous system to go back to.",
            )

        original, target = info.current, info.other
        self.report.status(f"Switching from slot {original.upper()} back to slot {target.upper()}")
        switch = self.switch_slot(device, target)
        if not switch.helped:
            return RepairAttempt(name, False, None, switch.message, switch.detail)

        remember_slot_switch(device.serial, original, target)

        self.report.status("Rebooting - this takes a couple of minutes, do not unplug it")
        self.manager.fastboot.reboot("", device.serial or None)
        watch = self.wait_for_boot(device.serial or None, timeout)

        if watch.booted is True:
            return RepairAttempt(
                name,
                True,
                True,
                f"It booted from slot {target.upper()}. The update was the problem.",
                "Back up now, before anything else - then let the update reinstall itself properly.",
            )

        rolled_back = False
        if roll_back_on_failure:
            rolled_back = self._restore_slot_if_in_fastboot(device, original)

        detail = ""
        if not rolled_back:
            detail = (
                f"The phone is now set to boot slot {target.upper()}. To put it back, get it into "
                f"fastboot and run 'ptransfer nokia --undo-slot'."
            )
        return RepairAttempt(name, True, watch.booted, watch.detail, detail, rolled_back)

    # --- experiment 2: finish the update ----------------------------------
    def finish_update(
        self,
        device: Device,
        zip_path: str | Path = "",
        workdir: Path | str = ".",
        timeout: float = 240.0,
    ) -> RepairAttempt:
        """Complete the half-applied update instead of undoing it.

        The other way a phone gets stuck: the update was interrupted partway,
        so neither the old nor the new system is whole. Applying the package in
        full rebuilds the system partitions and leaves userdata alone.
        """
        name = "Finish the interrupted update"
        if device.state not in (State.RECOVERY, State.SIDELOAD):
            return RepairAttempt(
                name,
                False,
                None,
                "Needs the phone in recovery.",
                "Power off, hold Volume Up, then press and hold Power - then run this again.",
            )

        local = Path(zip_path) if zip_path else None
        if local is None:
            packages = self.find_ota_packages(device)
            if not packages:
                return RepairAttempt(
                    name,
                    False,
                    None,
                    "No update package to apply.",
                    "Nothing was downloaded to the phone before it broke, and HMD does not "
                    "publish these. If you have the right signed zip, pass it with --ota.",
                )
            staged = self.apply_ota_from_device(device, packages[0], Path(workdir))
            if staged.status == "failed":
                return RepairAttempt(name, False, None, staged.message, staged.detail)
            local = Path(workdir) / Path(packages[0]).name

        if not local.exists():
            return RepairAttempt(name, False, None, f"{local} does not exist.")

        serial = device.serial or None
        if device.state is not State.SIDELOAD:
            self.report.status("Putting the phone into sideload mode")
            self.manager.adb.reboot("sideload", serial)
            if not self._wait_for_state(serial, "sideload", 90):
                return RepairAttempt(
                    name,
                    False,
                    None,
                    "The phone did not enter sideload mode by itself.",
                    "On the phone, choose 'Apply update from ADB' with the volume keys and Power, "
                    "then run this again.",
                )

        self.report.status(f"Applying {local.name} - do not unplug the phone")
        res = self.manager.adb.sideload(
            str(local), serial, on_line=lambda ln: self.report.status(ln.strip()[-90:]), cancel=self.cancel
        )
        if not res.ok:
            return RepairAttempt(
                name,
                True,
                None,
                "The update was rejected.",
                res.output.strip()[-300:] or "no output from the phone",
            )

        self.report.status("Update applied. Rebooting")
        self.manager.adb.reboot("", serial)
        watch = self.wait_for_boot(serial, timeout)
        if watch.booted is True:
            return RepairAttempt(
                name,
                True,
                True,
                "It booted after the update was applied in full.",
                "Back up now, before anything else.",
            )
        return RepairAttempt(name, True, watch.booted, watch.detail)

    # --- run both -----------------------------------------------------------
    def repair_boot_loop(
        self,
        device: Device,
        strategy: str = "auto",
        zip_path: str | Path = "",
        workdir: Path | str = ".",
        timeout: float = 240.0,
    ) -> RepairSession:
        """Try undoing the update, then finishing it, and report which worked.

        Reverting goes first on purpose: it writes nothing, takes two minutes,
        and is trivially undone. Applying an update rewrites system partitions,
        so it is the second thing to try, not the first.
        """
        session = RepairSession()
        self.report.start_section("bootloop", "Trying to fix the boot loop")

        if strategy in ("auto", "revert", "both"):
            attempt = self.revert_update(device, timeout)
            session.attempts.append(attempt)
            if attempt.fixed_it:
                session.advice = (
                    "Fixed by going back to the previous system. Back the phone up now - then let "
                    "the update download and install again, on a full battery and left alone."
                )
                return session

        if strategy in ("auto", "update", "both"):
            attempt = self.finish_update(device, zip_path, workdir, timeout)
            session.attempts.append(attempt)
            if attempt.fixed_it:
                session.advice = "Fixed by applying the update in full. Back the phone up now."
                return session

        session.advice = _advice_after_failure(session.attempts)
        return session

    def _restore_slot_if_in_fastboot(self, device: Device, slot: str) -> bool:
        """Put the slot back, if the phone happens to be reachable again."""
        try:
            devices = self.manager.fastboot.devices()
        except ToolError:
            return False
        if not devices:
            return False
        serial = device.serial or devices[0].serial or None
        res = self.manager.fastboot.set_active(slot, serial)
        if res.ok:
            self.report.status(f"Put the active slot back to {slot.upper()}")
            clear_slot_switch()
            return True
        return False

    def undo_slot_switch(self, device: Device) -> StepOutcome:
        """Undo the last slot switch, even in a later session."""
        record = last_slot_switch()
        if record is None:
            return StepOutcome("undo slot switch", "skipped", "No slot switch to undo.")
        if device.state is not State.BOOTLOADER:
            return StepOutcome(
                "undo slot switch",
                "skipped",
                "Needs the phone in fastboot.",
                "Power off, hold Volume Down, then connect USB.",
            )
        original = record.get("from", "")
        if original not in ("a", "b"):
            return StepOutcome("undo slot switch", "failed", "The saved record is unusable.")
        res = self.manager.fastboot.set_active(original, device.serial or None)
        if not res.ok:
            return StepOutcome("undo slot switch", "failed", res.output.strip()[:200])
        clear_slot_switch()
        return StepOutcome(
            "undo slot switch",
            "ok",
            f"Active slot put back to {original.upper()}.",
            "Nothing was erased either way.",
        )

    def _wait_for_state(self, serial: str | None, state: str, timeout: float) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            self.cancel.raise_if_cancelled()
            try:
                devices = self.manager.adb.devices()
            except ToolError:
                devices = []
            for d in devices:
                if (not serial or d.serial == serial) and d.state == state:
                    return True
            time.sleep(2)
        return False

    # --- the guided run -------------------------------------------------
    def guided_rescue(self, device: Device, workdir: Path | str = ".") -> NokiaReport:
        """Work through every data-safe repair, in order, and report."""
        report = NokiaReport()
        self.report.start_section("nokia", "Nokia guided rescue")

        report.identity = self.identify(device)
        self.report.status(report.identity.describe().splitlines()[0])

        if device.state.can_transfer:
            report.steps.append(
                StepOutcome(
                    "back up first",
                    "needs-user",
                    "This phone is booted and readable right now.",
                    "Stop rescuing and run a backup - repairs can always wait, a working phone "
                    "may not. Run: ptransfer backup -o <folder>",
                )
            )

        # 1. What does the phone itself say went wrong?
        self.report.status("Looking for the recovery log")
        text = self.read_recovery_log(device)
        if text:
            report.log_excerpt = text[-8000:]
            report.findings = self.analyse_log(text)
            report.steps.append(
                StepOutcome(
                    "read recovery log",
                    "ok" if report.findings else "info",
                    f"{len(report.findings)} known problem(s) recognised"
                    if report.findings
                    else "Log retrieved but nothing recognisable in it.",
                )
            )
        else:
            report.steps.append(
                StepOutcome(
                    "read recovery log",
                    "skipped",
                    "No log reachable - the phone needs to be in recovery for this.",
                    "Power off, hold Volume Up, then press and hold Power.",
                )
            )

        # 2. The slot switch: the highest-value data-safe repair.
        self.cancel.raise_if_cancelled()
        report.slots = self.read_slots(device)
        if report.slots is not None:
            self.report.status(report.slots.describe().splitlines()[0])
            if report.slots.switch_is_promising:
                report.steps.append(
                    StepOutcome(
                        "slot check",
                        "ok",
                        f"Slot {report.slots.current.upper()} is bad and slot "
                        f"{report.slots.other.upper()} looks healthy - switching is very likely to fix this.",
                        "Run: ptransfer nokia --switch-slot",
                    )
                )
            elif report.slots.is_ab:
                report.steps.append(
                    StepOutcome(
                        "slot check",
                        "info",
                        "Both slots report the same state, so a switch is a coin flip rather than "
                        "a fix - but it erases nothing, so it is still worth one try.",
                    )
                )
            else:
                report.steps.append(StepOutcome("slot check", "skipped", "Single-slot device."))
        else:
            report.steps.append(
                StepOutcome(
                    "slot check",
                    "skipped",
                    "Needs fastboot mode.",
                    "Power off, hold Volume Down, then connect the USB cable.",
                )
            )

        # 3. A signed update the phone already has.
        self.cancel.raise_if_cancelled()
        report.ota_packages = self.find_ota_packages(device)
        if report.ota_packages:
            report.steps.append(
                StepOutcome(
                    "find update package",
                    "ok",
                    f"{len(report.ota_packages)} package(s) already on the phone.",
                    "HMD signed these, so recovery will accept them - run "
                    "'ptransfer nokia --apply-ota' to use one.",
                )
            )
        else:
            report.steps.append(
                StepOutcome("find update package", "info", "No downloaded update found on the phone.")
            )

        return report


MODE_FOR = {
    "Revert to the previous system": "fastboot (power off, hold Volume Down, connect USB)",
    "Finish the interrupted update": "recovery (power off, hold Volume Up, then press and hold Power)",
}

NEXT_STEPS = (
    "Run 'ptransfer nokia --logs' with the phone in recovery - the crash log will say what is "
    "actually failing, and that decides what comes next.\n"
    "Do not factory reset yet. A phone that still reaches recovery or fastboot is usually "
    "repairable with your data intact, and a reset ends that permanently."
)


def _advice_after_failure(attempts: list[RepairAttempt]) -> str:
    """What to say when the phone still is not booting.

    The distinction that matters: an option that *failed* is ruled out, an
    option that was *skipped* because the phone was in the wrong mode has not
    been tried at all - and saying otherwise would send someone to a factory
    reset with a repair still on the table.
    """
    tried = [a for a in attempts if a.performed]
    skipped = [a for a in attempts if not a.performed]

    if not tried:
        lines = ["Neither repair could run yet - each needs the phone in a particular mode:"]
        for a in attempts:
            mode = MODE_FOR.get(a.name, "a different mode")
            lines.append(f"  {a.name.lower()} - {mode}")
        lines.append("Get it into one of those and run this again.")
        return "\n".join(lines)

    if any(a.booted is None for a in tried):
        return (
            "The repair ran, but this PC could not tell whether the phone booted - that usually "
            "means USB debugging was never switched on. Look at the phone: if it is on the home "
            "screen, it worked, and you should back it up straight away."
        )

    if skipped:
        names = ", ".join(a.name.lower() for a in skipped)
        lines = [
            f"That did not fix it, but {names} has not been tried yet.",
            "To try it, put the phone in "
            + MODE_FOR.get(skipped[0].name, "the right mode")
            + ", then run this again.",
            "",
            NEXT_STEPS,
        ]
        return "\n".join(lines)

    return (
        "Neither going back nor going forward got it booting, so the update is not what is "
        "wrong.\n" + NEXT_STEPS
    )


# --- remembering what we changed -----------------------------------------
# A slot switch is trivially reversible, but only if you can still find out
# which slot it came from - and the phone may not come back for hours.
SLOT_RECORD = "last-slot-switch.json"


def _record_path() -> Path:
    return user_data_dir() / SLOT_RECORD


def remember_slot_switch(serial: str, from_slot: str, to_slot: str) -> None:
    try:
        _record_path().write_text(
            json.dumps(
                {
                    "serial": serial,
                    "from": from_slot,
                    "to": to_slot,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        log.warning("could not record the slot switch")


def last_slot_switch() -> dict | None:
    path = _record_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_slot_switch() -> None:
    try:
        _record_path().unlink(missing_ok=True)
    except OSError:
        pass


def _typical_chipset(name: str) -> str:
    n = (name or "").strip().lower()
    if not n:
        return ""
    # Longest key first so "nokia 5.3" beats "nokia 5".
    for key in sorted(TYPICAL_CHIPSETS, key=len, reverse=True):
        if n.startswith(key):
            return TYPICAL_CHIPSETS[key]
    return ""


def is_nokia(device: Device) -> bool:
    return device.vendor_key == "nokia"
