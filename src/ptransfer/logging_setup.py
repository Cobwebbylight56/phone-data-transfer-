"""Logging: quiet on screen, complete in the log file.

When a transfer goes wrong the log is the only evidence, so it always records
at DEBUG regardless of what the console shows.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .platform_tools import user_data_dir

_configured = False


def log_file() -> Path:
    return user_data_dir() / "ptransfer.log"


def setup_logging(verbose: bool = False, to_file: bool = True) -> Path | None:
    global _configured
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if not _configured:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if verbose else logging.WARNING)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)

    path = None
    if to_file and not _configured:
        try:
            path = log_file()
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s")
            )
            root.addHandler(handler)
        except OSError:
            path = None

    _configured = True
    return path


def attach_bundle_log(bundle_dir: Path) -> logging.Handler | None:
    """Also write the log into the bundle being created."""
    try:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(bundle_dir / "transfer.log", encoding="utf-8")
    except OSError:
        return None
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s"))
    logging.getLogger().addHandler(handler)
    return handler


def detach(handler: logging.Handler | None) -> None:
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()
