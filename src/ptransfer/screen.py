"""See the phone's screen on the PC, and drive it from there.

How it works: ``adb exec-out screencap -p`` hands back a PNG of whatever is on
the display, and ``adb shell input`` injects taps, swipes and keystrokes. Poll
the first, send the second, and you have a working remote screen without any
extra software on the phone.

**What this can and cannot reach.** Both halves need Android itself to be
running - screen capture comes from SurfaceFlinger and ``input`` is an Android
binary. A phone sitting in recovery or fastboot has neither, so there is
nothing to mirror there. Recovery is driven by the physical buttons, and
:mod:`ptransfer.menu` covers that instead.

**On typing your PIN from the PC.** This works only on a phone that has already
authorised this computer for USB debugging - and granting that authorisation
requires physically unlocking the phone and accepting the prompt. So it is a
convenience for a phone you have already unlocked once, not a way into a locked
one. Nothing here iterates over candidate PINs, and it should stay that way:
one credential goes in, the phone accepts it or does not.
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
from dataclasses import dataclass

from .adb import Adb
from .devices import Device, State
from .proc import ToolError

log = logging.getLogger(__name__)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Android keycodes worth having buttons for.
KEYCODES = {
    "home": 3,
    "back": 4,
    "call": 5,
    "endcall": 6,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "camera": 27,
    "menu": 82,
    "enter": 66,
    "delete": 67,
    "recents": 187,
    "wakeup": 224,
    "sleep": 223,
    "unlock_swipe": 82,
}

# KEYCODE_0 is 7 and the digits run in order from there. Sending digits as
# keycodes is far more reliable on the lock screen than 'input text'.
DIGIT_BASE = 7


class ScreenUnavailable(RuntimeError):
    """The phone cannot provide a screen right now, with the reason why."""


@dataclass
class ScreenSize:
    width: int = 0
    height: int = 0

    @property
    def valid(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass
class Frame:
    png: bytes
    width: int = 0
    height: int = 0

    @property
    def ok(self) -> bool:
        return self.png.startswith(PNG_MAGIC)


def escape_input_text(text: str) -> str:
    """Make a string safe for ``input text``.

    ``input text`` splits on spaces, so a space has to arrive as ``%s``, and
    the shell metacharacters have to survive the trip through ``adb shell``.
    """
    out = []
    for ch in text:
        if ch == " ":
            out.append("%s")
        elif ch in "\\\"'`$&|;<>()*?[]#~":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


class PhoneScreen:
    """Capture and input for one connected phone."""

    def __init__(self, adb: Adb, serial: str | None = None) -> None:
        self.adb = adb
        self.serial = serial
        self._size: ScreenSize | None = None

    # --- preconditions -------------------------------------------------
    @staticmethod
    def check_supported(device: Device) -> None:
        """Raise :class:`ScreenUnavailable` if mirroring cannot work here."""
        if device.state is State.ONLINE:
            return
        if device.state is State.UNAUTHORIZED:
            raise ScreenUnavailable(
                "The phone has not authorised this computer yet. Unlock it and tap 'Allow' on "
                "the USB debugging prompt, then try again."
            )
        if device.state in (State.RECOVERY, State.SIDELOAD):
            raise ScreenUnavailable(
                "The phone is in recovery, which has no Android display service to capture and "
                "no 'input' command to drive - so there is nothing to mirror. Recovery is "
                "navigated with the physical buttons: Volume Up/Down to move, Power to select. "
                "The Recovery menu helper on the Rescue tab lists what each option does."
            )
        if device.state is State.BOOTLOADER:
            raise ScreenUnavailable(
                "The phone is in its bootloader. There is no screen to mirror here - use the "
                "Rescue tab instead."
            )
        raise ScreenUnavailable(
            f"The phone is {device.state.value}. Mirroring needs a booted phone with USB "
            "debugging authorised."
        )

    # --- capture --------------------------------------------------------
    def size(self, refresh: bool = False) -> ScreenSize:
        if self._size is not None and not refresh:
            return self._size
        res = self.adb.shell("wm size", self.serial, timeout=20)
        size = parse_wm_size(res.output)
        self._size = size
        return size

    def capture(self) -> Frame:
        """One frame, as PNG bytes."""
        args = [self.adb.path]
        if self.serial:
            args += ["-s", self.serial]
        args += ["exec-out", "screencap", "-p"]
        try:
            code, data = self.adb.runner.run_bytes(args, timeout=30)
        except ToolError as exc:
            raise ScreenUnavailable(str(exc)) from exc

        if data.startswith(PNG_MAGIC):
            size = self.size()
            return Frame(data, size.width, size.height)

        # Some old builds route screencap through a shell that turns \n into
        # \r\n. Undoing that recovers an otherwise valid PNG.
        repaired = data.replace(b"\r\n", b"\n")
        if repaired.startswith(PNG_MAGIC):
            size = self.size()
            return Frame(repaired, size.width, size.height)

        text = data[:200].decode("utf-8", "replace").strip()
        if not data:
            raise ScreenUnavailable(
                "The phone returned an empty screenshot. Some phones block screen capture on "
                "secure screens - the lock screen, banking apps, and Netflix all do this."
            )
        raise ScreenUnavailable(f"Screen capture failed: {text or 'unrecognised response'}")

    # --- input ----------------------------------------------------------
    def tap(self, x: int, y: int) -> bool:
        res = self.adb.shell(f"input tap {int(x)} {int(y)}", self.serial, timeout=20)
        return res.ok

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 200) -> bool:
        res = self.adb.shell(
            f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}", self.serial, timeout=30
        )
        return res.ok

    def key(self, name_or_code: str | int) -> bool:
        code = KEYCODES.get(str(name_or_code).lower(), name_or_code)
        res = self.adb.shell(f"input keyevent {code}", self.serial, timeout=20)
        return res.ok

    def type_text(self, text: str) -> bool:
        if not text:
            return True
        res = self.adb.shell(
            f"input text {shlex.quote(escape_input_text(text))}", self.serial, timeout=30
        )
        return res.ok

    def type_digits(self, digits: str) -> bool:
        """Send digits as keycodes - what the lock screen actually accepts."""
        for ch in digits:
            if not ch.isdigit():
                return False
            if not self.key(DIGIT_BASE + int(ch)):
                return False
        return True

    # --- lock screen ------------------------------------------------------
    def is_locked(self) -> bool | None:
        """True/False, or None when the phone will not say."""
        res = self.adb.shell(
            "dumpsys window 2>/dev/null | grep -m2 -E 'mDreamingLockscreen|isStatusBarKeyguard|mShowingLockscreen'",
            self.serial,
            timeout=20,
        )
        return parse_lock_state(res.output)

    def wake(self) -> bool:
        return self.key("wakeup")

    def unlock(self, credential: str = "", numeric: bool | None = None) -> str:
        """Wake, swipe the lock screen away, and enter one credential.

        Returns a human-readable outcome. Deliberately makes a *single*
        attempt - this is for a phone you already own and have authorised, not
        a device to guess at.
        """
        self.wake()
        size = self.size()
        if size.valid:
            # Swipe up from near the bottom to dismiss the lock screen.
            self.swipe(size.width // 2, int(size.height * 0.75), size.width // 2, int(size.height * 0.2), 200)
        else:
            self.key("menu")

        if not credential:
            return "Woke the phone and dismissed the lock screen. No passcode was given."

        if numeric is None:
            numeric = credential.isdigit()

        ok = self.type_digits(credential) if numeric else self.type_text(credential)
        if not ok:
            return "Could not send the passcode to the phone."
        self.key("enter")

        locked = self.is_locked()
        if locked is False:
            return "Unlocked."
        if locked is True:
            return (
                "The phone is still locked - the passcode was not accepted, or this phone uses a "
                "pattern, which cannot be drawn this way."
            )
        return "Passcode sent. Check the phone's screen for the result."


def parse_wm_size(text: str) -> ScreenSize:
    """Parse `wm size`. Override size wins - that is what is actually shown."""
    override = re.search(r"Override size:\s*(\d+)x(\d+)", text)
    physical = re.search(r"Physical size:\s*(\d+)x(\d+)", text)
    m = override or physical
    if not m:
        m = re.search(r"(\d{3,5})x(\d{3,5})", text)
    if not m:
        return ScreenSize()
    return ScreenSize(int(m.group(1)), int(m.group(2)))


def parse_lock_state(text: str) -> bool | None:
    low = text.lower()
    for key in ("mdreaminglockscreen", "isstatusbarkeyguard", "mshowinglockscreen"):
        m = re.search(key + r"\s*=\s*(true|false)", low)
        if m:
            return m.group(1) == "true"
    return None


def map_to_device(
    click_x: float,
    click_y: float,
    shown_w: int,
    shown_h: int,
    device_w: int,
    device_h: int,
) -> tuple[int, int]:
    """Turn a click on the scaled image into a coordinate on the phone."""
    if shown_w <= 0 or shown_h <= 0 or device_w <= 0 or device_h <= 0:
        return 0, 0
    x = int(round(click_x * device_w / shown_w))
    y = int(round(click_y * device_h / shown_h))
    return max(0, min(device_w - 1, x)), max(0, min(device_h - 1, y))


def scrcpy_path() -> str | None:
    """scrcpy gives a far smoother mirror; use it when it is installed."""
    from .platform_tools import find_scrcpy

    return find_scrcpy()


SECURE_SCREEN_HINT = (
    "The picture is black because Android refuses to screenshot this screen. The lock screen "
    "keypad is marked secure, and so are banking apps and video services - screencap returns "
    "black rather than the contents.\n\n"
    "You can still type your PIN: put it in the box below and press Unlock. The keystrokes "
    "arrive even though you cannot see the keypad.\n\n"
    "To actually see the lock screen, use scrcpy - it mirrors the display itself instead of "
    "asking for a screenshot, so secure screens come through. Press 'Get scrcpy' to install it."
)
