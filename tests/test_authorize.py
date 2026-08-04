"""Triggering the phone's 'Allow USB debugging?' prompt.

The distinction that matters throughout: debugging switched *off* and this PC
merely *untrusted* look identical from the outside, and need opposite advice.
"""

from __future__ import annotations

import pytest

from ptransfer import authorize as auth_mod
from ptransfer.authorize import Authorizer
from ptransfer.devices import Device, DeviceManager, State
from ptransfer.platform_tools import Tools
from ptransfer.proc import FakeRunner, Result
from ptransfer.usb import UsbDevice


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(auth_mod.time, "sleep", lambda _s: None)


def manager(runner, usb=None, monkeypatch=None) -> DeviceManager:
    return DeviceManager(Tools("adb", "fastboot"), runner)


@pytest.fixture(autouse=True)
def no_usb(monkeypatch):
    # Both names matter: devices.py builds the Device list, and authorize.py
    # asks the USB layer directly to tell 'debugging off' from 'not plugged in'.
    monkeypatch.setattr("ptransfer.devices.enumerate_usb", lambda runner=None: [])
    monkeypatch.setattr("ptransfer.authorize.enumerate_usb", lambda runner=None: [])


def usb_shows(monkeypatch, *devices):
    monkeypatch.setattr("ptransfer.devices.enumerate_usb", lambda runner=None: list(devices))
    monkeypatch.setattr("ptransfer.authorize.enumerate_usb", lambda runner=None: list(devices))


def adb_says(line: str) -> FakeRunner:
    runner = FakeRunner()
    runner.responses["adb devices -l"] = Result(
        ["adb"], 0, f"List of devices attached\n{line}\n" if line else "List of devices attached\n"
    )
    return runner


# --- telling the situations apart ---------------------------------------
def test_already_authorised_is_recognised():
    situation = Authorizer(manager(adb_says("R58M device model:SM_S938B"))).situation()
    assert situation.ready
    assert situation.kind == "authorised"
    assert not situation.can_prompt


def test_unauthorised_is_worth_prompting():
    situation = Authorizer(manager(adb_says("R58M unauthorized"))).situation()

    assert situation.kind == "unauthorised"
    assert situation.can_prompt
    assert "has not been told to trust this computer" in situation.summary
    assert any("Always allow" in s for s in situation.steps)


def test_offline_suggests_revoking_and_replugging():
    situation = Authorizer(manager(adb_says("R58M offline"))).situation()

    assert situation.kind == "offline"
    assert situation.can_prompt
    assert any("Revoke USB debugging" in s for s in situation.steps)


def test_visible_on_usb_but_not_adb_means_debugging_is_off(monkeypatch):
    usb_shows(monkeypatch, UsbDevice("04e8", "6860", "Samsung Galaxy"))
    situation = Authorizer(manager(adb_says(""))).situation()

    assert situation.kind == "debugging-off"
    assert "Samsung Galaxy" in situation.summary
    assert not situation.can_prompt  # there is no prompt to trigger
    assert "has to be done on the phone" in situation.summary
    assert any("Build number" in s for s in situation.steps)


def test_nothing_at_all_blames_the_cable_first():
    situation = Authorizer(manager(adb_says(""))).situation()

    assert situation.kind == "nothing"
    assert "charge-only" in situation.steps[0].lower()


# --- the on-phone steps are brand-aware ----------------------------------
def test_samsung_steps_go_via_software_information():
    steps = Authorizer(manager(FakeRunner())).enable_steps("samsung")
    assert any("Software information" in s for s in steps)


def test_nokia_steps_do_not():
    steps = Authorizer(manager(FakeRunner())).enable_steps("nokia")
    assert not any("Software information" in s for s in steps)
    assert any("Build number" in s for s in steps)


def test_unknown_brand_gets_the_generic_path():
    steps = Authorizer(manager(FakeRunner())).enable_steps("some-brand")
    assert any("Developer options" in s for s in steps)


def test_debugging_off_uses_the_brand_of_the_phone_on_usb(monkeypatch):
    usb_shows(monkeypatch, UsbDevice("04e8", "6860"))
    situation = Authorizer(manager(adb_says(""))).situation(brand_hint="samsung")
    assert any("Software information" in s for s in situation.steps)


# --- actually asking ------------------------------------------------------
class Handshake(FakeRunner):
    """Unauthorised until the adb server restarts, then authorised."""

    def __init__(self, allow_after_restart: bool = True) -> None:
        super().__init__()
        self.allow_after_restart = allow_after_restart
        self.restarted = False

    def run(self, args, timeout=60.0, input_text=None, cwd=None):
        self.calls.append(list(args))
        joined = " ".join(args)
        if "kill-server" in joined:
            self.restarted = True
            return Result(args, 0, "")
        if "devices" in joined:
            state = "device" if (self.restarted and self.allow_after_restart) else "unauthorized"
            return Result(args, 0, f"List of devices attached\nR58M {state}\n")
        return Result(args, 0, "")


def test_requesting_restarts_the_daemon_and_detects_the_allow():
    phone = Handshake()
    result = Authorizer(manager(phone)).request(timeout=5)

    assert result.authorised
    assert "now trusts this computer" in result.message
    issued = [" ".join(c) for c in phone.calls]
    # Restarting the server is what makes the phone ask again.
    assert any("kill-server" in c for c in issued)
    assert any("start-server" in c for c in issued)


def test_never_tapping_allow_times_out_with_advice():
    phone = Handshake(allow_after_restart=False)
    result = Authorizer(manager(phone)).request(timeout=0.3)

    assert not result.authorised
    assert "only appears while the screen is unlocked" in result.message
    assert "Revoke USB debugging authorisations" in result.message


def test_requesting_when_already_authorised_does_nothing():
    phone = adb_says("R58M device")
    result = Authorizer(manager(phone)).request(timeout=5)

    assert result.authorised
    assert not any("kill-server" in " ".join(c) for c in phone.calls)


def test_requesting_with_debugging_off_explains_instead_of_waiting(monkeypatch):
    usb_shows(monkeypatch, UsbDevice("2e04", "c026"))
    phone = adb_says("")
    result = Authorizer(manager(phone)).request(timeout=5)

    assert not result.authorised
    assert "no prompt to trigger" in result.message
    assert "Build number" in result.message
    assert not any("kill-server" in " ".join(c) for c in phone.calls)


def test_situation_renders_numbered_steps():
    text = Authorizer(manager(adb_says("R58M unauthorized"))).situation().as_text()
    assert "1. " in text and "2. " in text
