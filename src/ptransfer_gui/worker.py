"""Background jobs.

Every long operation runs on a worker thread; the engine's progress events are
re-emitted as Qt signals so the window can stay responsive and cancellable.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ptransfer.proc import Cancel, Cancelled
from ptransfer.progress import Event, Reporter

log = logging.getLogger(__name__)


class JobSignals(QObject):
    event = Signal(object)      # ptransfer.progress.Event
    finished = Signal(object)   # whatever the job returned
    failed = Signal(str)


class Job(QRunnable):
    """Runs ``fn(reporter, cancel, **kwargs)`` off the UI thread."""

    def __init__(self, fn: Callable[..., Any], **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.kwargs = kwargs
        self.signals = JobSignals()
        self.cancel = Cancel()

    @Slot()
    def run(self) -> None:  # pragma: no cover - thread body
        reporter = Reporter(self._emit)
        try:
            result = self.fn(reporter=reporter, cancel=self.cancel, **self.kwargs)
        except Cancelled:
            self.signals.failed.emit("Cancelled.")
        except Exception as exc:
            log.exception("job failed")
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=3)}")
        else:
            self.signals.finished.emit(result)

    def _emit(self, event: Event) -> None:
        self.signals.event.emit(event)

    def stop(self) -> None:
        self.cancel.set()
