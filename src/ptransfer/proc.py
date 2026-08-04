"""Subprocess helpers.

Every external binary (adb, fastboot, powershell) is invoked through here so
that timeouts, encoding and logging behave identically everywhere, and so that
tests can swap in a fake runner.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

log = logging.getLogger(__name__)

# On Windows, keep console windows from flashing up for every adb call.
try:  # pragma: no cover - platform dependent
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover
    _NO_WINDOW = 0


class ToolError(RuntimeError):
    """Raised when an external tool cannot be run at all."""


@dataclass
class Result:
    args: Sequence[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """stdout with stderr appended - adb likes to talk on both."""
        parts = [p for p in (self.stdout, self.stderr) if p]
        return "\n".join(parts).strip()

    def lines(self) -> list[str]:
        return [ln.strip() for ln in self.stdout.splitlines() if ln.strip()]


class Runner:
    """Thin wrapper over subprocess. Subclass/replace in tests."""

    def run(
        self,
        args: Sequence[str],
        timeout: float | None = 60.0,
        input_text: str | None = None,
        cwd: str | None = None,
    ) -> Result:
        log.debug("run: %s", " ".join(args))
        try:
            proc = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                input=input_text,
                cwd=cwd,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise ToolError(f"{args[0]!r} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            return Result(
                args=args,
                returncode=-1,
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr),
                timed_out=True,
            )
        return Result(args, proc.returncode, proc.stdout or "", proc.stderr or "")

    def stream(
        self,
        args: Sequence[str],
        on_line: Callable[[str], None],
        timeout: float | None = None,
        cancel: "Cancel | None" = None,
    ) -> Result:
        """Run a command, calling ``on_line`` for each line of output.

        Used for long operations (``adb pull`` of a whole card, sideload) where
        the user needs to see something happening.
        """
        log.debug("stream: %s", " ".join(args))
        collected: list[str] = []
        try:
            proc = subprocess.Popen(
                list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise ToolError(f"{args[0]!r} not found on PATH") from exc

        timer: threading.Timer | None = None
        if timeout:
            timer = threading.Timer(timeout, proc.kill)
            timer.start()
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                collected.append(line)
                on_line(line)
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    break
            proc.wait()
        finally:
            if timer is not None:
                timer.cancel()
        return Result(args, proc.returncode or 0, "\n".join(collected), "")


class Cancel:
    """Cooperative cancellation token shared between the GUI and the engine."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()


class Cancelled(Exception):
    """Raised inside the engine when the user aborts an operation."""


@dataclass
class FakeRunner(Runner):
    """Test double: maps a command signature to a canned :class:`Result`."""

    responses: dict[str, Result] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    default: Result | None = None

    def key(self, args: Iterable[str]) -> str:
        return " ".join(args)

    def run(self, args, timeout=60.0, input_text=None, cwd=None) -> Result:  # type: ignore[override]
        self.calls.append(list(args))
        k = self.key(args)
        if k in self.responses:
            return self.responses[k]
        for prefix, res in self.responses.items():
            if k.startswith(prefix):
                return res
        if self.default is not None:
            return self.default
        return Result(args, 0, "", "")

    def stream(self, args, on_line, timeout=None, cancel=None) -> Result:  # type: ignore[override]
        res = self.run(args)
        for line in res.stdout.splitlines():
            on_line(line)
        return res


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)
