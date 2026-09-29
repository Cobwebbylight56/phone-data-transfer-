from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Exit before Qt's static destructors run.

    PySide6 under the offscreen platform crashes in its C++ teardown at
    interpreter exit - after every test has already passed. That turns a green
    run into exit code 139 and fails CI for no real reason. Letting the summary
    print, then hard-exiting with pytest's real status, sidesteps it. No-op when
    the run passed cleanly on a platform that does not crash.
    """
    yield
    if os.environ.get("PTRANSFER_NO_HARD_EXIT"):
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(exitstatus))

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ptransfer.proc import FakeRunner, Result  # noqa: E402


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


def result(stdout: str = "", returncode: int = 0, stderr: str = "") -> Result:
    return Result(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def make_result():
    return result
