"""Phone to phone, both plugged into the PC at once.

The PC is the hub: it reads the old phone, holds everything, and writes it to
the new one. Two cables, one operation.

This is the shape the job actually has when a screen is broken. You cannot tap
"next" on a phone you cannot see, so the tap-to-tap transfer apps - Smart
Switch, Xperia Transfer, Google's cable copy - are all unavailable. Driving
both phones from the PC is the way through, and mirroring lets you see the
broken one while it happens.

Deliberately built on top of the backup and restore engines rather than beside
them: the staging bundle is a real, verifiable backup, and keeping it means an
interrupted transfer resumes instead of restarting, and a transfer that goes
wrong still leaves you holding your data.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .backup import SECTIONS, BackupEngine, BackupOptions, BackupReport
from .devices import Device, DeviceManager, State
from .manifest import safe_bundle_name
from .proc import Cancel, ToolError
from .progress import Reporter, human_bytes
from .restore import RestoreEngine, RestoreOptions, RestoreReport

log = logging.getLogger(__name__)

# Restore has no "deviceinfo" or "settings" step - they are reference only.
RESTORE_SECTIONS = ("media", "apps", "contacts", "sms", "calls")


@dataclass
class TransferOptions:
    sections: tuple[str, ...] = SECTIONS
    keep_bundle: bool = True
    verify: bool = True
    install_apps: bool = True

    @property
    def restore_sections(self) -> tuple[str, ...]:
        return tuple(s for s in RESTORE_SECTIONS if s in self.sections)


@dataclass
class TransferReport:
    source: str = ""
    target: str = ""
    bundle_path: str = ""
    backup: BackupReport | None = None
    restore: RestoreReport | None = None
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and self.backup is not None and self.restore is not None

    def todo(self) -> list[str]:
        """What still needs a tap on the new phone."""
        if self.restore is None:
            return []
        out = []
        for step in self.restore.needs_user:
            out.append(f"{step.name}: {step.message}")
            out.extend(f"    {t}" for t in step.todo)
        return out

    def as_text(self) -> str:
        out = [f"Transfer: {self.source}  ->  {self.target}", "=" * 60, ""]
        if self.problems:
            out.append("Stopped:")
            out.extend(f"  {p}" for p in self.problems)
            return "\n".join(out)

        if self.backup is not None:
            out.append("Copied off the old phone")
            out.append("-" * 24)
            out.extend(self.backup.summary_lines())
            out.append("")
        if self.restore is not None:
            out.append("Written to the new phone")
            out.append("-" * 24)
            out.extend(self.restore.summary_lines())
            out.append("")

        todo = self.todo()
        if todo:
            out.append("Finish these on the new phone")
            out.append("-" * 29)
            out.extend(f"  {t}" for t in todo)
            out.append("")

        out.append(f"Took {self.seconds / 60:.0f} minutes.")
        if self.bundle_path:
            out.append(f"A full backup is kept at: {self.bundle_path}")
            out.append("Keep it until you have checked the new phone has everything.")
        return "\n".join(out)


class TransferEngine:
    def __init__(
        self,
        manager: DeviceManager,
        source: Device,
        target: Device,
        reporter: Reporter | None = None,
        cancel: Cancel | None = None,
    ) -> None:
        self.manager = manager
        self.source = source
        self.target = target
        self.report = reporter or Reporter()
        self.cancel = cancel or Cancel()

    # --- refuse early rather than halfway ------------------------------
    def preflight(self, staging: Path | str = ".") -> list[str]:
        """Everything that would stop this working, found before it starts."""
        problems: list[str] = []

        if self.source.serial and self.source.serial == self.target.serial:
            problems.append(
                "The same phone is selected on both sides. Pick the old phone on the left and "
                "the new one on the right."
            )

        for role, device in (("old", self.source), ("new", self.target)):
            if device.state is State.ONLINE:
                continue
            if device.state is State.UNAUTHORIZED:
                problems.append(
                    f"The {role} phone has not authorised this computer. Unlock it and tap Allow, "
                    "or use 'Ask the phone for permission'."
                )
            else:
                problems.append(
                    f"The {role} phone is {device.state.value}, not ready. It needs to be booted "
                    "with USB debugging allowed."
                )

        needed = self.estimate_bytes()
        if needed:
            free = self.free_space(staging)
            if free and free < needed * 1.1:
                problems.append(
                    f"Not enough room in {staging}: about {human_bytes(needed)} is needed and "
                    f"{human_bytes(free)} is free. Choose a drive with more space."
                )
        return problems

    def estimate_bytes(self) -> int:
        """Roughly how much is on the old phone's storage."""
        try:
            adb = self.manager.adb
        except ToolError:
            return 0
        serial = self.source.serial or None
        root = adb.storage_root(serial)
        res = adb.shell(f"du -s -k {root} 2>/dev/null | tail -n 1", serial, timeout=300)
        first = res.stdout.strip().split()
        if first and first[0].isdigit():
            return int(first[0]) * 1024
        return 0

    @staticmethod
    def free_space(path: Path | str) -> int:
        target = Path(path)
        while not target.exists() and target.parent != target:
            target = target.parent
        try:
            return shutil.disk_usage(target).free
        except OSError:
            return 0

    # --- the transfer --------------------------------------------------
    def run(
        self,
        staging: Path | str,
        options: TransferOptions | None = None,
        bundle_name: str = "",
    ) -> TransferReport:
        options = options or TransferOptions()
        started = time.time()
        result = TransferReport(source=self.source.label, target=self.target.label)

        problems = self.preflight(staging)
        if problems:
            result.problems = problems
            result.seconds = time.time() - started
            return result

        staging = Path(staging)
        name = bundle_name or safe_bundle_name(self.source.label)
        bundle = staging / (name if name.endswith(".ptbundle") else name + ".ptbundle")
        result.bundle_path = str(bundle)

        # 1. Old phone -> PC.
        self.report.start_section("transfer", f"Reading {self.source.label}")
        backup = BackupEngine(self.manager.adb, self.source, self.report, self.cancel)
        result.backup = backup.run(
            bundle,
            BackupOptions(sections=options.sections, verify=options.verify),
        )

        if not any(s.succeeded for s in result.backup.sections.values()):
            result.problems.append(
                "Nothing could be read off the old phone, so there is nothing to write to the "
                "new one. Check the old phone is unlocked and still connected."
            )
            result.seconds = time.time() - started
            return result

        self.cancel.raise_if_cancelled()

        # 2. PC -> new phone.
        self.report.start_section("transfer", f"Writing to {self.target.label}")
        restore = RestoreEngine(
            self.manager.adb, self.target.serial or None, self.report, self.cancel
        )
        result.restore = restore.run(
            bundle,
            RestoreOptions(
                sections=options.restore_sections,
                install_apps=options.install_apps,
            ),
        )

        if not options.keep_bundle:
            self.report.status("Removing the staging copy")
            shutil.rmtree(bundle, ignore_errors=True)
            result.bundle_path = ""

        result.seconds = time.time() - started
        self.report.done(f"Transfer finished in {result.seconds / 60:.0f} minutes")
        return result


def pick_pair(devices: list[Device]) -> tuple[Device | None, Device | None]:
    """Guess which phone is the old one and which is the new one.

    Only a starting suggestion - the user picks. The heuristic is that the
    phone with more on it is the one being replaced.
    """
    ready = [d for d in devices if d.state is State.ONLINE]
    if len(ready) < 2:
        return (ready[0] if ready else None), None

    # Older Android is more likely to be the phone being retired.
    by_age = sorted(ready, key=lambda d: d.sdk or 0)
    return by_age[0], by_age[-1]
