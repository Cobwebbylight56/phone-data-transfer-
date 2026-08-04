"""Mirroring the phone properly, with scrcpy as the engine.

Why scrcpy rather than the screenshot loop:

* it captures the **display**, not an app surface, so the lock screen keypad
  comes through - the screenshot path gets a black rectangle there, which is
  precisely the screen you need when the phone is locked;
* it is 30-60 fps with low latency instead of a few frames a second;
* it forwards mouse, keyboard and text properly, including modifier keys.

So scrcpy is the default and the screenshot loop is the fallback, not the other
way round. scrcpy is downloaded automatically the first time it is needed - a
tool the user has to go and find is a tool that does not get used.

The window it opens is reparented into this app on Windows so there is one
window, not two. That is a best-effort thing: if the reparenting fails, scrcpy
still runs in its own window and everything still works.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field

from .platform_tools import Tools, find_scrcpy, is_windows

log = logging.getLogger(__name__)

WINDOW_TITLE = "PhoneDataTransfer-Mirror"

# Kept deliberately small. Every extra flag is a chance to hit a version that
# does not have it, and a phone mid-rescue is the worst place to discover that.
BASE_FLAGS = (
    "--no-audio",      # audio forwarding fails on some devices and aborts the session
    "--stay-awake",    # a phone that sleeps mid-transfer drops off USB
)


@dataclass
class MirrorPlan:
    backend: str            # "scrcpy" | "screencap"
    reason: str
    scrcpy: str = ""
    needs_download: bool = False

    @property
    def is_scrcpy(self) -> bool:
        return self.backend == "scrcpy"


def choose_backend(scrcpy: str | None = None, allow_download: bool = True) -> MirrorPlan:
    """Decide how to mirror. scrcpy wins whenever it can be had."""
    found = scrcpy if scrcpy is not None else find_scrcpy()
    if found:
        return MirrorPlan("scrcpy", "scrcpy is installed", scrcpy=found)
    if allow_download and is_windows():
        return MirrorPlan(
            "scrcpy",
            "scrcpy is not installed yet; it will be downloaded first",
            needs_download=True,
        )
    return MirrorPlan(
        "screencap",
        "scrcpy is not available, so falling back to screenshots - the lock screen will be "
        "black and it will be slower.",
    )


def scrcpy_command(
    scrcpy_path: str,
    serial: str | None = None,
    title: str = WINDOW_TITLE,
    borderless: bool = True,
    extra: tuple[str, ...] = (),
) -> list[str]:
    args = [scrcpy_path, *BASE_FLAGS, "--window-title", title]
    if borderless:
        # Only for embedding - a standalone window wants its title bar.
        args.append("--window-borderless")
    if serial:
        args += ["-s", serial]
    args += list(extra)
    return args


def scrcpy_env(tools: Tools | None = None) -> dict[str, str]:
    """Point scrcpy at the same adb this app uses.

    Two adb binaries of different versions fight over port 5037 and kill each
    other's server, which looks exactly like the phone disconnecting.
    """
    env = dict(os.environ)
    if tools is not None and tools.adb:
        env["ADB"] = tools.adb
    return env


@dataclass
class ScrcpySession:
    """A running scrcpy process, and the window it opened."""

    path: str
    serial: str | None = None
    title: str = WINDOW_TITLE
    borderless: bool = True
    tools: Tools | None = None
    process: subprocess.Popen | None = None
    hwnd: int = 0
    error: str = ""
    _output: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, extra: tuple[str, ...] = ()) -> bool:
        command = scrcpy_command(self.path, self.serial, self.title, self.borderless, extra)
        log.info("starting scrcpy: %s", " ".join(command))
        try:
            self.process = subprocess.Popen(
                command,
                env=scrcpy_env(self.tools),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self.error = f"Could not start scrcpy: {exc}"
            return False
        return True

    def wait_for_window(self, timeout: float = 20.0) -> int:
        """Find the window scrcpy opened, so it can be embedded. Windows only."""
        if not is_windows():  # pragma: no cover - platform dependent
            return 0
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running:
                self.error = self.read_output() or "scrcpy exited before opening a window."
                return 0
            hwnd = user32.FindWindowW(None, self.title)
            if hwnd:
                self.hwnd = int(hwnd)
                return self.hwnd
            time.sleep(0.25)
        self.error = "scrcpy did not open a window in time."
        return 0

    def read_output(self) -> str:
        """Whatever scrcpy said - its errors are unusually clear, so show them."""
        if self.process is None or self.process.stdout is None:
            return ""
        try:
            self.process.stdout.flush()
        except Exception:
            pass
        try:
            remaining = self.process.stdout.read() or ""
        except Exception:
            remaining = ""
        if remaining:
            self._output.append(remaining)
        return "".join(self._output).strip()

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - rare
                self.process.kill()
        self.process = None
        self.hwnd = 0


def explain_failure(output: str) -> str:
    """Turn scrcpy's exit message into something actionable."""
    low = (output or "").lower()
    if "device unauthorized" in low or "unauthorized" in low:
        return (
            "The phone has not authorised this computer. Unlock it, tap Allow on the USB "
            "debugging prompt, then start mirroring again."
        )
    # "Could not find any ADB device" is about the phone, not the binary, and
    # mentions both words - so it has to be matched before the missing-adb case.
    if "no device" in low or "device not found" in low or "find any adb device" in low:
        return "The phone is no longer connected. Check the cable and try again."
    if "adb server version" in low or "killing" in low:
        return (
            "Two different versions of adb are fighting over the connection. Close any other "
            "phone software (Samsung Smart Switch, Xperia Companion, phone-link tools) and try "
            "again."
        )
    if "could not find adb" in low or "adb: not found" in low or "no such file" in low:
        return "scrcpy could not find adb. Reinstall the app so adb is set up again."
    if "encoder" in low or "encoding" in low:
        return (
            "The phone's video encoder refused. Try again, and if it persists mirroring may not "
            "work on this device - the screenshot fallback still will."
        )
    return output.strip()[-400:] if output else "scrcpy stopped without saying why."
