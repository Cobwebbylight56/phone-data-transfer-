"""Guided unlock: wait for the phone, tell the user, take the PIN, open the mirror.

This is for one situation and it is a common one: your own phone, screen dead or
blank, that you cannot see to swipe and tap. Everything here drives it over the
USB debugging channel you have *already* authorised - which is the point. It is
not a way past a lock screen you do not own:

* the channel only works because the phone was unlocked once, physically, to
  tick "Always allow from this computer";
* the PIN is tried **once**, exactly as you typed it - nothing iterates,
  nothing guesses;
* if the phone will not say whether it unlocked, the app says so rather than
  pretending.

The chain the user asked for:

    wait for the phone to boot  ->  "on and ready for your PIN"  ->  you type it
    ->  unlock  ->  open the mirror automatically

so a blank-screen phone can be seen and driven from the PC.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

from .devices import DeviceManager
from .proc import Cancel, ToolError
from .progress import Reporter
from .screen import PhoneScreen

log = logging.getLogger(__name__)


class Readiness(str, Enum):
    NO_DEVICE = "no-device"          # nothing on adb yet
    UNAUTHORIZED = "unauthorized"    # connected, but this PC is not trusted
    NO_SCREEN = "no-screen"          # recovery / fastboot - nothing to unlock or mirror
    BOOTING = "booting"              # online, but Android has not finished starting
    LOCKED = "locked"                # booted, lock screen up - ready for the PIN
    UNKNOWN_LOCK = "unknown-lock"    # booted, but the phone will not say if it is locked
    UNLOCKED = "unlocked"            # booted and unlocked - ready to mirror

    @property
    def ready_for_pin(self) -> bool:
        return self in (Readiness.LOCKED, Readiness.UNKNOWN_LOCK)

    @property
    def can_mirror(self) -> bool:
        return self in (Readiness.UNLOCKED, Readiness.LOCKED, Readiness.UNKNOWN_LOCK)

    @property
    def settled(self) -> bool:
        """True once the user (or the flow) can actually act."""
        return self in (
            Readiness.UNAUTHORIZED,
            Readiness.NO_SCREEN,
            Readiness.LOCKED,
            Readiness.UNKNOWN_LOCK,
            Readiness.UNLOCKED,
        )


# What to show the user for each state, so the app narrates the wait.
NARRATION = {
    Readiness.NO_DEVICE: "Waiting for the phone… plug it in and let it boot.",
    Readiness.UNAUTHORIZED: "Phone connected, but it has not trusted this PC. Tap Allow on the "
    "phone, or use 'Ask the phone for permission'.",
    Readiness.NO_SCREEN: "Phone is in recovery or the bootloader - there is no lock screen to "
    "open here. Use the Rescue tab.",
    Readiness.BOOTING: "Phone is on and still starting up…",
    Readiness.LOCKED: "Phone is on and locked - ready for your PIN.",
    Readiness.UNKNOWN_LOCK: "Phone is on. Enter your PIN and it will be sent.",
    Readiness.UNLOCKED: "Phone is unlocked and ready.",
}


@dataclass
class UnlockResult:
    unlocked: bool
    readiness: Readiness
    message: str
    can_mirror: bool = False


class UnlockFlow:
    def __init__(
        self,
        manager: DeviceManager,
        serial: str | None,
        reporter: Reporter | None = None,
        cancel: Cancel | None = None,
    ) -> None:
        self.manager = manager
        self.serial = serial
        self.report = reporter or Reporter()
        self.cancel = cancel or Cancel()

    def _screen(self) -> PhoneScreen:
        return PhoneScreen(self.manager.adb, self.serial)

    # --- what state is the phone in right now? --------------------------
    def readiness(self) -> Readiness:
        adb = self.manager.adb
        try:
            devices = adb.devices()
        except ToolError:
            return Readiness.NO_DEVICE

        mine = [d for d in devices if not self.serial or d.serial == self.serial]
        if not mine:
            return Readiness.NO_DEVICE
        device = mine[0]

        if device.state == "unauthorized":
            return Readiness.UNAUTHORIZED
        if device.state in ("recovery", "sideload", "bootloader", "rescue"):
            return Readiness.NO_SCREEN
        if device.state != "device":
            return Readiness.BOOTING  # offline etc. - still settling

        # Online. Has Android actually finished starting?
        try:
            completed = adb.getprop("sys.boot_completed", self.serial).strip()
        except ToolError:
            completed = ""
        if completed != "1":
            return Readiness.BOOTING

        locked = self._screen().is_locked()
        if locked is True:
            return Readiness.LOCKED
        if locked is False:
            return Readiness.UNLOCKED
        return Readiness.UNKNOWN_LOCK

    # --- wait for it to get somewhere the user can act ------------------
    def wait_until_ready(self, timeout: float = 300.0) -> Readiness:
        """Poll until the phone reaches a state the user can act on.

        Narrates each change - that is the 'tell me on and ready for the PIN'
        the user asked for - and keeps waiting through the transient states
        (nothing connected yet, still booting) until something settled shows up.
        """
        started = time.time()
        last: Readiness | None = None

        while time.time() - started < timeout:
            self.cancel.raise_if_cancelled()
            state = self.readiness()
            if state != last:
                self.report.status(NARRATION[state])
                last = state
            if state.settled:
                return state
            elapsed = time.time() - started
            self.report.progress(
                min(0.99, elapsed / timeout),
                NARRATION[state],
                throttle=1.0,
            )
            time.sleep(2)

        return last or Readiness.NO_DEVICE

    # --- the whole chain -----------------------------------------------
    def wait_unlock_and_prepare(
        self,
        credential: str = "",
        numeric: bool | None = None,
        timeout: float = 300.0,
    ) -> UnlockResult:
        """Wait, then unlock with the PIN if the phone is locked.

        Returns a result the caller can act on - importantly, ``can_mirror``
        tells the GUI whether to open the mirror next. The PIN is sent once.
        """
        state = self.wait_until_ready(timeout)

        if state is Readiness.UNAUTHORIZED:
            return UnlockResult(False, state, NARRATION[state])
        if state is Readiness.NO_SCREEN:
            return UnlockResult(False, state, NARRATION[state])
        if state is Readiness.NO_DEVICE:
            return UnlockResult(
                False,
                state,
                "The phone never appeared. Check the cable is a data cable and USB debugging is on.",
            )
        if state is Readiness.UNLOCKED:
            return UnlockResult(True, state, "Phone is already unlocked.", can_mirror=True)

        # LOCKED or UNKNOWN_LOCK.
        if not credential:
            return UnlockResult(
                False,
                state,
                "Phone is on and ready. Type your PIN or password and send it.",
                can_mirror=state.can_mirror,  # you can still mirror and unlock by hand
            )

        self.report.status("Sending your PIN to the phone…")
        message = self._screen().unlock(credential, numeric)
        after = self.readiness()
        unlocked = after is Readiness.UNLOCKED or "Unlocked" in message
        return UnlockResult(unlocked, after, message, can_mirror=True)
