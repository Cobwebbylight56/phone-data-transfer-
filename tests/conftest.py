from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
