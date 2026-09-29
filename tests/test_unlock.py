"""The guided unlock flow: wait, narrate, send the PIN once, then allow mirroring."""

from __future__ import annotations

import pytest

from ptransfer import unlock as unlock_mod
from ptransfer.devices import DeviceManager
from ptransfer.platform_tools import Tools
from ptransfer.proc import FakeRunner, Result
from ptransfer.unlock import Readiness, UnlockFlow


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(unlock_mod.time, "sleep", lambda _s: None)


def manager(runner) -> DeviceManager:
    return DeviceManager(Tools("adb", "fastboot"), runner)


class ScriptedPhone(FakeRunner):
    """A phone that walks through boot states, one per `adb devices` poll."""

    def __init__(self, states: list[str], boot_completed=True, locked="true"):
        super().__init__()
        self.states = list(states)
        self.boot_completed = boot_completed
        self.locked = locked  # "true" | "false" | "" (unknown)

    def run(self, args, timeout=60.0, input_text=None, cwd=None):
        self.calls.append(list(args))
        joined = " ".join(args)
        if "devices" in joined:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            if not state:
                return Result(args, 0, "List of devices attached\n")
            return Result(args, 0, f"List of devices attached\nR58M {state}\n")
        if "sys.boot_completed" in joined:
            return Result(args, 0, "1" if self.boot_completed else "")
        if "dumpsys window" in joined:
            if self.locked == "":
                return Result(args, 0, "nothing useful")
            return Result(args, 0, f"mDreamingLockscreen={self.locked}")
        if "wm size" in joined:
            return Result(args, 0, "Physical size: 1080x2340")
        return Result(args, 0, "")


# --- reading the state --------------------------------------------------
def test_no_device_when_adb_sees_nothing():
    flow = UnlockFlow(manager(ScriptedPhone([""])), "R58M")
    assert flow.readiness() is Readiness.NO_DEVICE


def test_unauthorized_is_reported():
    flow = UnlockFlow(manager(ScriptedPhone(["unauthorized"])), "R58M")
    assert flow.readiness() is Readiness.UNAUTHORIZED


def test_recovery_has_no_screen_to_unlock():
    flow = UnlockFlow(manager(ScriptedPhone(["recovery"])), "R58M")
    assert flow.readiness() is Readiness.NO_SCREEN


def test_online_but_not_booted_is_booting():
    flow = UnlockFlow(manager(ScriptedPhone(["device"], boot_completed=False)), "R58M")
    assert flow.readiness() is Readiness.BOOTING


def test_booted_and_locked():
    flow = UnlockFlow(manager(ScriptedPhone(["device"], locked="true")), "R58M")
    r = flow.readiness()
    assert r is Readiness.LOCKED
    assert r.ready_for_pin


def test_booted_and_unlocked():
    flow = UnlockFlow(manager(ScriptedPhone(["device"], locked="false")), "R58M")
    r = flow.readiness()
    assert r is Readiness.UNLOCKED
    assert r.can_mirror


def test_booted_but_lock_state_unknown_is_still_actionable():
    flow = UnlockFlow(manager(ScriptedPhone(["device"], locked="")), "R58M")
    r = flow.readiness()
    assert r is Readiness.UNKNOWN_LOCK
    assert r.ready_for_pin


# --- waiting through the transient states -------------------------------
def test_wait_narrates_each_change_and_settles():
    seen: list[str] = []
    reporter = unlock_mod.Reporter(lambda e: seen.append(e.message) if e.kind == "status" else None)
    # nothing -> booting -> locked
    phone = ScriptedPhone(["", "device", "device"], boot_completed=True, locked="true")
    # boot_completed only becomes true once online; force the first 'device' to be booting
    phone.boot_completed = True

    flow = UnlockFlow(manager(phone), "R58M", reporter)
    result = flow.wait_until_ready(timeout=5)

    assert result is Readiness.LOCKED
    joined = " ".join(seen)
    assert "Waiting for the phone" in joined
    assert "ready for your PIN" in joined


def test_wait_stops_on_unauthorized_rather_than_spinning():
    phone = ScriptedPhone(["unauthorized"])
    result = UnlockFlow(manager(phone), "R58M").wait_until_ready(timeout=5)
    assert result is Readiness.UNAUTHORIZED


def test_wait_times_out_to_no_device_when_nothing_connects():
    phone = ScriptedPhone([""])
    result = UnlockFlow(manager(phone), "R58M").wait_until_ready(timeout=0.3)
    assert result in (Readiness.NO_DEVICE, Readiness.BOOTING)


# --- the whole chain ----------------------------------------------------
def test_locked_phone_with_a_pin_is_unlocked_and_cleared_for_mirroring():
    # locked at first, then unlocked after the PIN is sent
    phone = ScriptedPhone(["device"], locked="true")
    flow = UnlockFlow(manager(phone), "R58M")

    calls_before = len(phone.calls)
    # After sending the PIN, report the phone as unlocked.
    original = phone.run

    def run(args, timeout=60.0, input_text=None, cwd=None):
        joined = " ".join(args)
        # once the PIN keyevents have gone, flip the lock state
        if "keyevent 66" in joined:  # enter
            phone.locked = "false"
        return original(args, timeout, input_text, cwd)

    phone.run = run  # type: ignore[assignment]

    result = flow.wait_unlock_and_prepare(credential="1902", timeout=5)

    assert result.unlocked
    assert result.can_mirror
    issued = [" ".join(c) for c in phone.calls]
    # digits went as keycodes (KEYCODE_1 = 8), and enter was pressed
    assert any("keyevent 8" in c for c in issued)
    assert any("keyevent 66" in c for c in issued)


def test_unauthorized_stops_the_chain_with_advice():
    phone = ScriptedPhone(["unauthorized"])
    result = UnlockFlow(manager(phone), "R58M").wait_unlock_and_prepare("1902", timeout=5)

    assert not result.unlocked
    assert not result.can_mirror
    assert "trusted this PC" in result.message


def test_already_unlocked_skips_straight_to_mirroring():
    phone = ScriptedPhone(["device"], locked="false")
    result = UnlockFlow(manager(phone), "R58M").wait_unlock_and_prepare("", timeout=5)

    assert result.unlocked
    assert result.can_mirror
    # No PIN sent - nothing to do.
    assert not any("keyevent 8" in " ".join(c) for c in phone.calls)


def test_locked_with_no_pin_asks_for_one_but_still_allows_mirroring():
    phone = ScriptedPhone(["device"], locked="true")
    result = UnlockFlow(manager(phone), "R58M").wait_unlock_and_prepare("", timeout=5)

    assert not result.unlocked
    assert result.can_mirror  # you can open the mirror and unlock by hand
    assert "Type your PIN" in result.message


def test_recovery_gives_no_mirror():
    phone = ScriptedPhone(["recovery"])
    result = UnlockFlow(manager(phone), "R58M").wait_unlock_and_prepare("1902", timeout=5)

    assert not result.unlocked
    assert not result.can_mirror
    assert "recovery" in result.message.lower()


def test_the_pin_is_sent_only_once():
    """No brute forcing: one attempt, whatever the outcome."""
    phone = ScriptedPhone(["device"], locked="true")  # stays locked
    flow = UnlockFlow(manager(phone), "R58M")
    flow.wait_unlock_and_prepare("0000", timeout=5)

    enters = [c for c in phone.calls if "keyevent 66" in " ".join(c)]
    assert len(enters) == 1
