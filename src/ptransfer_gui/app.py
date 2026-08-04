"""Desktop app entry point."""

from __future__ import annotations

import sys

from ptransfer.logging_setup import setup_logging


def run() -> int:
    setup_logging()
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Phone Data Transfer")
    app.setOrganizationName("PhoneDataTransfer")

    window = MainWindow()
    window.show()
    return app.exec()


def main() -> int:  # console/gui script entry point
    return run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
