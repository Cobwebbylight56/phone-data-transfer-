"""The boot-loop repair: undo the update, or finish it, and check the result.

Real waiting is patched out - these tests are about the decisions, not the
clock.
"""

from __future__ import annotations

import json

import pytest

from ptransfer import nokia as nokia_mod
from ptransfer.devices import Device, DeviceManager, State
from ptransfer.nokia import NokiaRescue, last_slot_switch
from ptransfer.platform_tools import Tools
from ptransfer.proc import FakeRunner, Result

GETVAR_SLOTS = (
    "(bootloader) slot-count: 2\n"
    "(bootloader) current-slot: a\n"
    "(bootloader) slot-successful:a: no\n"
    "(bootloader) slot-unbootable:a: yes\n"
    "(bootloader) slot-successful:b: yes\n"
    "(bootloader) slot-unbootable:b: no\n"
)


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(nokia_mod.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    """Keep the slot-switch record out of the real user data directory."""
    monkeypatch.setattr(nokia_mod, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(nokia_mod, "enumerate_usb", lambda runner=None: [])
    return tmp_path


def manager(runner) -> DeviceManager:
    return DeviceManager(Tools("adb", "fastboot"), runner)


def nokia(state=State.BOOTLOADER, **kw) -> Device:
    kw.setdefault("manufacturer", "HMD Global")
    kw.setdefault("model", "Nokia 8.3 5G")
    return Device(serial="NOK123", state=state, **kw)


class Phone(FakeRunner):
    """A scripted phone: says what state it is in, one answer per poll."""

    def __init__(self, states: list[str], boot_completed: bool = True, fastboot_after: bool = False):
        super().__init__()
        self.states = list(states)
        self.boot_completed = boot_completed
        self.fastboot_after = fastboot_after
        self.rebooted = False

    def run(self, args, timeout=5.0, input_text=None, cwd=None):
        self.calls.append(list(args))
        joined = " ".join(args)

        if "getvar all" in joined:
            return Result(args, 0, stderr=GETVAR_SLOTS)
        if "set_active" in joined:
            return Result(args, 0, "OKAY")
        if joined.startswith("fastboot") and "reboot" in joined:
            self.rebooted = True
            return Result(args, 0, "")
        if joined.startswith("fastboot") and "devices" in joined:
            return Result(args, 0, "NOK123 fastboot\n" if self.fastboot_after else "")
        if "adb devices" in joined:
            state = self.states.pop(0) if self.states else (self.states[-1] if self.states else "")
            if not state:
                return Result(args, 0, "List of devices attached\n")
            return Result(args, 0, f"List of devices attached\nNOK123 {state}\n")
        if "sys.boot_completed" in joined:
            return Result(args, 0, "1" if self.boot_completed else "")
        return Result(args, 0, "")


# --- watching for a boot ------------------------------------------------
def test_boot_is_detected_when_the_phone_comes_back():
    phone = Phone(["", "", "device"], boot_completed=True)
    watch = NokiaRescue(manager(phone)).wait_for_boot("NOK123", timeout=5)

    assert watch.booted is True
    assert "finished booting" in watch.detail


def test_a_phone_that_keeps_vanishing_is_called_a_loop():
    phone = Phone(["device", "", "device", "", "device", ""] * 4, boot_completed=False)
    watch = NokiaRescue(manager(phone)).wait_for_boot("NOK123", timeout=0.5)

    assert watch.booted is False
    assert watch.looping
    assert watch.disappearances >= 2
    assert "still looping" in watch.detail


def test_a_silent_phone_is_reported_as_not_booted():
    phone = Phone([""] * 40, boot_completed=False)
    watch = NokiaRescue(manager(phone)).wait_for_boot("NOK123", timeout=0.3)

    assert watch.booted is False
    assert not watch.looping
    assert "No sign of a completed boot" in watch.detail


def test_usb_only_phone_is_unknown_not_failed(monkeypatch):
    """USB debugging off: adb sees nothing, but the phone may be fine."""
    from ptransfer.usb import UsbDevice

    monkeypatch.setattr(nokia_mod, "enumerate_usb", lambda runner=None: [UsbDevice("2e04", "c026")])
    phone = Phone([""] * 40, boot_completed=False)
    watch = NokiaRescue(manager(phone)).wait_for_boot("NOK123", timeout=0.3)

    assert watch.booted is None
    assert "USB debugging was never switched on" in watch.detail


# --- reverting the update ------------------------------------------------
def test_revert_switches_slot_reboots_and_reports_success():
    phone = Phone(["", "device"], boot_completed=True)
    attempt = NokiaRescue(manager(phone)).revert_update(nokia(), timeout=5)

    assert attempt.fixed_it
    assert "slot B" in attempt.message
    assert "The update was the problem" in attempt.message
    assert "Back up now" in attempt.detail
    assert phone.rebooted
    assert any("set_active b" in " ".join(c) for c in phone.calls)


def test_revert_records_the_original_slot_so_it_can_be_undone(private_state):
    phone = Phone(["", "device"], boot_completed=True)
    NokiaRescue(manager(phone)).revert_update(nokia(), timeout=5)

    record = last_slot_switch()
    assert record["from"] == "a" and record["to"] == "b"
    assert json.loads((private_state / "last-slot-switch.json").read_text())["serial"] == "NOK123"


def test_a_failed_revert_is_rolled_back_when_the_phone_returns_to_fastboot():
    phone = Phone([""] * 40, boot_completed=False, fastboot_after=True)
    attempt = NokiaRescue(manager(phone)).revert_update(nokia(), timeout=0.3)

    assert attempt.performed
    assert attempt.booted is False
    assert attempt.rolled_back
    # Switched to b, then back to a.
    switches = [" ".join(c) for c in phone.calls if "set_active" in " ".join(c)]
    assert switches[0].endswith("set_active b")
    assert switches[-1].endswith("set_active a")


def test_a_failed_revert_that_cannot_roll_back_says_how_to_do_it():
    phone = Phone([""] * 40, boot_completed=False, fastboot_after=False)
    attempt = NokiaRescue(manager(phone)).revert_update(nokia(), timeout=0.3)

    assert not attempt.rolled_back
    assert "--undo-slot" in attempt.detail


def test_rollback_can_be_declined():
    phone = Phone([""] * 40, boot_completed=False, fastboot_after=True)
    attempt = NokiaRescue(manager(phone)).revert_update(
        nokia(), timeout=4, roll_back_on_failure=False
    )
    assert not attempt.rolled_back


def test_revert_needs_fastboot_and_says_so():
    attempt = NokiaRescue(manager(FakeRunner())).revert_update(nokia(State.RECOVERY))
    assert not attempt.performed
    assert "Volume Down" in attempt.detail


def test_revert_is_skipped_on_a_single_slot_phone():
    runner = FakeRunner()
    runner.responses["fastboot -s NOK123 getvar all"] = Result(
        ["fastboot"], 0, stderr="(bootloader) slot-count: 1\n"
    )
    attempt = NokiaRescue(manager(runner)).revert_update(nokia())
    assert not attempt.performed
    assert "no previous system" in attempt.message


# --- undoing later --------------------------------------------------------
def test_undo_slot_switch_restores_the_original(private_state):
    phone = Phone(["", "device"], boot_completed=True)
    rescue = NokiaRescue(manager(phone))
    rescue.revert_update(nokia(), timeout=5)

    outcome = rescue.undo_slot_switch(nokia())
    assert outcome.helped
    assert "back to A" in outcome.message
    assert last_slot_switch() is None  # record consumed


def test_undo_with_nothing_recorded_is_a_no_op():
    outcome = NokiaRescue(manager(FakeRunner())).undo_slot_switch(nokia())
    assert outcome.status == "skipped"
    assert "No slot switch to undo" in outcome.message


# --- the combined run -----------------------------------------------------
def test_repair_stops_as_soon_as_reverting_works():
    phone = Phone(["", "device"], boot_completed=True)
    session = NokiaRescue(manager(phone)).repair_boot_loop(nokia(), timeout=5)

    assert session.fixed
    assert len(session.attempts) == 1  # no need to try the update
    assert "Back the phone up now" in session.advice
    assert "FIXED IT" in session.as_text()


def test_repair_moves_on_to_the_update_when_reverting_fails():
    phone = Phone([""] * 60, boot_completed=False)
    session = NokiaRescue(manager(phone)).repair_boot_loop(nokia(), timeout=0.2)

    names = [a.name for a in session.attempts]
    assert "Revert to the previous system" in names
    assert "Finish the interrupted update" in names
    assert not session.fixed


def test_advice_when_neither_could_even_run():
    # Phone in an unknown state: neither experiment applies.
    session = NokiaRescue(manager(FakeRunner())).repair_boot_loop(nokia(State.UNKNOWN), timeout=0.2)
    assert not session.fixed
    assert "fastboot (power off, hold Volume Down" in session.advice
    assert "recovery (power off, hold Volume Up" in session.advice


def test_advice_after_a_real_failure_points_at_the_log_and_warns_off_a_reset():
    phone = Phone([""] * 60, boot_completed=False)
    session = NokiaRescue(manager(phone)).repair_boot_loop(nokia(), timeout=0.2)

    assert "--logs" in session.advice
    assert "Do not factory reset yet" in session.advice


def test_a_skipped_option_is_not_reported_as_ruled_out():
    """Reverting failed but the update was never tried - say so."""
    phone = Phone([""] * 60, boot_completed=False)
    session = NokiaRescue(manager(phone)).repair_boot_loop(nokia(), timeout=0.2)

    skipped = [a for a in session.attempts if not a.performed]
    assert skipped, "the update step should have been skipped from fastboot"
    assert "has not been tried yet" in session.advice
    assert "Neither going back nor going forward" not in session.advice
    assert "hold Volume Up" in session.advice


def test_failure_detail_does_not_repeat_the_message():
    phone = Phone([""] * 60, boot_completed=False, fastboot_after=False)
    attempt = NokiaRescue(manager(phone)).revert_update(nokia(), timeout=0.3)

    assert attempt.message not in attempt.detail
    assert "--undo-slot" in attempt.detail


def test_strategy_revert_only_does_not_touch_the_update():
    phone = Phone([""] * 60, boot_completed=False)
    session = NokiaRescue(manager(phone)).repair_boot_loop(nokia(), strategy="revert", timeout=0.2)
    assert [a.name for a in session.attempts] == ["Revert to the previous system"]


def test_report_text_marks_each_outcome():
    phone = Phone([""] * 60, boot_completed=False)
    session = NokiaRescue(manager(phone)).repair_boot_loop(nokia(), strategy="revert", timeout=0.2)
    text = session.as_text()

    assert "Boot loop repair" in text
    assert "did not help" in text
