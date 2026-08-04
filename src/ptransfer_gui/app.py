"""Desktop app entry point."""

from __future__ import annotations

import sys

from ptransfer.logging_setup import setup_logging

MISSING_QT = """
Phone Data Transfer needs PySide6 for its window, and it is not installed.

Install it with:

    python -m pip install PySide6

Or re-run install.bat, which does it for you.

You do not need it for the command line - 'ptransfer devices', 'ptransfer backup'
and 'ptransfer nokia --fix-bootloop' all work without a window.
"""


def run() -> int:
    setup_logging()
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        # Someone double-clicking a shortcut should get a sentence, not a
        # traceback about a module they have never heard of.
        print(MISSING_QT, file=sys.stderr)
        return 2

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
