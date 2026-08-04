"""scrcpy as the mirroring engine, with the screenshot loop as fallback."""

from __future__ import annotations

import pytest

from ptransfer import mirror
from ptransfer.mirror import (
    BASE_FLAGS,
    WINDOW_TITLE,
    ScrcpySession,
    choose_backend,
    explain_failure,
    scrcpy_command,
    scrcpy_env,
)
from ptransfer.platform_tools import Tools


# --- picking a backend --------------------------------------------------
def test_scrcpy_is_used_when_installed():
    plan = choose_backend(scrcpy="C:/tools/scrcpy.exe")
    assert plan.is_scrcpy
    assert not plan.needs_download
    assert plan.scrcpy.endswith("scrcpy.exe")


def test_scrcpy_is_downloaded_rather_than_skipped(monkeypatch):
    """Not installed is not a reason to fall back - fetch it."""
    monkeypatch.setattr(mirror, "is_windows", lambda: True)
    plan = choose_backend(scrcpy="", allow_download=True)

    assert plan.is_scrcpy
    assert plan.needs_download
    assert "downloaded" in plan.reason


def test_screenshots_only_when_scrcpy_cannot_be_had(monkeypatch):
    monkeypatch.setattr(mirror, "is_windows", lambda: True)
    plan = choose_backend(scrcpy="", allow_download=False)

    assert plan.backend == "screencap"
    assert "lock screen will be black" in plan.reason


def test_non_windows_without_scrcpy_falls_back(monkeypatch):
    monkeypatch.setattr(mirror, "is_windows", lambda: False)
    assert choose_backend(scrcpy="").backend == "screencap"


# --- the command --------------------------------------------------------
def test_command_targets_the_right_phone_and_window():
    command = scrcpy_command("scrcpy.exe", serial="R58M", title="Mirror")

    assert command[0] == "scrcpy.exe"
    assert command[command.index("-s") + 1] == "R58M"
    assert command[command.index("--window-title") + 1] == "Mirror"
    for flag in BASE_FLAGS:
        assert flag in command


def test_audio_is_off_and_the_phone_is_kept_awake():
    """Audio forwarding aborts on some phones; sleeping drops the USB link."""
    command = scrcpy_command("scrcpy")
    assert "--no-audio" in command
    assert "--stay-awake" in command


def test_borderless_only_when_embedding():
    assert "--window-borderless" in scrcpy_command("scrcpy", borderless=True)
    assert "--window-borderless" not in scrcpy_command("scrcpy", borderless=False)


def test_no_serial_flag_when_only_one_phone():
    assert "-s" not in scrcpy_command("scrcpy", serial=None)


def test_default_window_title_is_findable():
    assert scrcpy_command("scrcpy")[-1] != WINDOW_TITLE  # title is a flag value
    assert WINDOW_TITLE in scrcpy_command("scrcpy")


def test_scrcpy_is_pointed_at_our_own_adb():
    """Two adb versions fight over port 5037 and look like a disconnect."""
    env = scrcpy_env(Tools("C:/app/adb.exe", "C:/app/fastboot.exe"))
    assert env["ADB"] == "C:/app/adb.exe"


def test_env_is_left_alone_without_tools():
    assert "ADB" not in scrcpy_env(Tools("", ""))


# --- failures explained -------------------------------------------------
@pytest.mark.parametrize(
    "output,expected",
    [
        ("ERROR: Device is unauthorized", "tap Allow"),
        ("ERROR: Could not find any ADB device", "no longer connected"),
        ("adb server version (41) doesn't match", "fighting over the connection"),
        ("ERROR: Encoder 'x' not found", "video encoder refused"),
    ],
)
def test_scrcpy_errors_become_actionable(output, expected):
    assert expected in explain_failure(output)


def test_unknown_failure_is_passed_through_not_swallowed():
    assert "something odd happened" in explain_failure("something odd happened")


def test_silent_failure_still_says_something():
    assert explain_failure("") == "scrcpy stopped without saying why."


# --- the session --------------------------------------------------------
def test_session_reports_a_missing_binary_instead_of_raising(monkeypatch):
    def boom(*a, **k):
        raise OSError("not found")

    monkeypatch.setattr(mirror.subprocess, "Popen", boom)
    session = ScrcpySession(path="nope.exe")

    assert session.start() is False
    assert "Could not start scrcpy" in session.error
    assert not session.running


def test_session_starts_and_stops_cleanly(monkeypatch):
    class FakeProc:
        def __init__(self, *a, **k):
            self.stdout = None
            self._alive = True
            self.terminated = False

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self.terminated = True
            self._alive = False

        def wait(self, timeout=None):
            return 0

    made: list[FakeProc] = []
    monkeypatch.setattr(mirror.subprocess, "Popen", lambda *a, **k: made.append(FakeProc()) or made[-1])

    session = ScrcpySession(path="scrcpy", serial="R58M")
    assert session.start()
    assert session.running

    session.stop()
    assert made[0].terminated
    assert not session.running


def test_wait_for_window_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(mirror, "is_windows", lambda: False)
    assert ScrcpySession(path="scrcpy").wait_for_window(timeout=0.1) == 0


def test_missing_adb_and_missing_phone_are_not_confused():
    """Both messages contain 'adb' - they must not give each other's advice."""
    assert "no longer connected" in explain_failure("ERROR: Could not find any ADB device")
    assert "could not find adb" in explain_failure("ERROR: Could not find adb").lower()


# --- finding what we ship -----------------------------------------------
def test_bundled_tools_are_found_in_the_pyinstaller_internal_folder(tmp_path, monkeypatch):
    """PyInstaller 6 puts --add-data under _internal/, not beside the exe."""
    from ptransfer import platform_tools as pt

    internal = tmp_path / "_internal"
    (internal / "platform-tools").mkdir(parents=True)
    (internal / "platform-tools" / "adb").write_text("#!/bin/sh\n")
    (internal / "scrcpy" / "scrcpy-v3.0").mkdir(parents=True)
    (internal / "scrcpy" / "scrcpy-v3.0" / "scrcpy").write_text("#!/bin/sh\n")

    monkeypatch.setattr(pt, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(pt, "user_data_dir", lambda: tmp_path / "userdata")
    monkeypatch.setattr(pt, "exe", lambda name: name)
    monkeypatch.delenv("PTRANSFER_ADB", raising=False)
    monkeypatch.delenv("PTRANSFER_SCRCPY", raising=False)

    assert pt.bundle_dir() == internal

    found, source = pt.find_tool("adb", "PTRANSFER_ADB")
    assert found is not None and "platform-tools" in found
    assert source == "bundled"

    scrcpy = pt.find_scrcpy()
    assert scrcpy is not None and scrcpy.endswith("scrcpy")


def test_no_internal_folder_is_not_a_problem(tmp_path, monkeypatch):
    from ptransfer import platform_tools as pt

    monkeypatch.setattr(pt, "app_dir", lambda: tmp_path)
    assert pt.bundle_dir() is None
