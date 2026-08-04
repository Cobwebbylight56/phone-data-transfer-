"""Progress plumbing shared by the CLI and the GUI.

The engine never imports Qt and never prints: it emits events, and whoever is
driving it decides what to do with them.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class Event:
    kind: str            # section | status | progress | warning | error | done
    section: str = ""
    message: str = ""
    fraction: float = -1.0   # 0..1, or -1 when unknown
    done_bytes: int = 0
    total_bytes: int = 0
    done_items: int = 0
    total_items: int = 0
    at: float = field(default_factory=time.time)


Sink = Callable[[Event], None]


class Reporter:
    def __init__(self, sink: Sink | None = None) -> None:
        self.sink = sink or (lambda e: None)
        self.section = ""
        self._last_emit = 0.0

    def start_section(self, name: str, message: str = "") -> None:
        self.section = name
        self.emit(Event("section", name, message or name))

    def status(self, message: str) -> None:
        log.info("%s: %s", self.section or "-", message)
        self.emit(Event("status", self.section, message))

    def progress(
        self,
        fraction: float = -1.0,
        message: str = "",
        done_bytes: int = 0,
        total_bytes: int = 0,
        done_items: int = 0,
        total_items: int = 0,
        throttle: float = 0.1,
    ) -> None:
        now = time.time()
        if throttle and now - self._last_emit < throttle and fraction < 1.0:
            return
        self._last_emit = now
        self.emit(
            Event(
                "progress",
                self.section,
                message,
                fraction,
                done_bytes,
                total_bytes,
                done_items,
                total_items,
            )
        )

    def warn(self, message: str) -> None:
        log.warning("%s: %s", self.section or "-", message)
        self.emit(Event("warning", self.section, message))

    def error(self, message: str) -> None:
        log.error("%s: %s", self.section or "-", message)
        self.emit(Event("error", self.section, message))

    def done(self, message: str = "") -> None:
        self.emit(Event("done", self.section, message))

    def emit(self, event: Event) -> None:
        try:
            self.sink(event)
        except Exception:  # a broken UI must never kill a transfer
            log.exception("progress sink raised")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
