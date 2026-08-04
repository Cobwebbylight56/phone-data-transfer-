"""Tests for the soft-brick side: diagnosis, and the honest extraction attempt."""

from __future__ import annotations

import pytest

from fake_phone import FakePhone
from ptransfer.devices import Device, DeviceManager, State
from ptransfer.platform_tools import Tools
from ptransfer.proc import Result
from ptransfer.recovery import RescueEngine, _looks_encrypted, diagnose
from ptransfer.usb import UsbDevice


def dev(state: State, **kw) -> Device:
    return Device(serial=kw.pop("serial", "S1"), state=state, **kw)


def test_nothing_connected_blames_the_cable_first():
    d = diagnose([])
    assert d.severity == "unknown"
    assert "cable" in d.steps[0].title.lower()
    assert not d.data_reachable


def test_online_phone_is_told_to_back_up_now():
    d = diagnose([dev(State.ONLINE, manufacturer="Sony", model="XQ-CT54")])
    assert d.severity == "fine"
    assert d.data_reachable
    assert d.steps[0].action == "backup-now"
    assert d.profile.key == "sony"


def test_unauthorized_is_not_treated_as_a_brick():
    d = diagnose([dev(State.UNAUTHORIZED)])
    assert d.severity == "fine"
    assert "Allow" in d.steps[0].title


def test_recovery_mode_warns_about_the_factory_reset_trap():
    d = diagnose([dev(State.RECOVERY, manufacturer="HMD Global")])
    assert d.severity == "soft-brick"
    assert d.profile.key == "nokia"
    assert any("factory reset" in w.lower() for w in d.warnings)
    assert d.steps[0].action == "extract-from-recovery"


def test_bootloader_locked_phone_is_told_not_to_unlock():
    device = dev(State.BOOTLOADER, fastboot_vars={"unlocked": "no", "product": "pdx223"})
    d = diagnose([device])
    assert d.severity == "soft-brick"
    assert any("unlocking forces a full wipe" in w for w in d.warnings)


def test_sony_flash_mode_is_recognised_from_usb_alone():
    usb = UsbDevice(vid="0fce", pid="adde", description="SOMC Flash Device")
    d = diagnose([Device(serial="", state=State.LOW_LEVEL, usb=usb)])

    assert d.severity == "deep"
    assert "flash mode" in d.situation.lower()
    assert d.profile.key == "sony"
    assert any("scam" in w.lower() for w in d.warnings)


def test_qualcomm_edl_is_identified_and_not_over_promised():
    usb = UsbDevice(vid="05c6", pid="9008", description="Qualcomm HS-USB QDLoader 9008")
    d = diagnose([Device(serial="", state=State.LOW_LEVEL, usb=usb)])

    assert d.severity == "deep"
    assert "Emergency Download" in d.situation
    assert "authorised service centre" in d.explanation


def test_every_diagnosis_says_the_data_is_encrypted_when_it_cannot_boot():
    for state in (State.RECOVERY, State.BOOTLOADER):
        d = diagnose([dev(state)])
        assert not d.data_reachable
        assert "encrypt" in d.explanation.lower()


def test_online_phone_beats_a_stale_usb_entry_when_picking():
    devices = [
        Device(serial="", state=State.LOW_LEVEL, usb=UsbDevice("0fce", "adde")),
        dev(State.ONLINE, manufacturer="Sony"),
    ]
    assert diagnose(devices).severity == "fine"


def test_brand_hint_overrides_detection():
    d = diagnose([dev(State.RECOVERY)], hint_brand="nokia")
    assert d.profile.key == "nokia"


def test_diagnosis_renders_to_text_with_the_plan():
    text = diagnose([dev(State.RECOVERY, manufacturer="Sony")]).as_text()
    assert "What to do, in order:" in text
    assert "Key combinations for this brand:" in text
    assert "Vendor tools:" in text


# --- extraction -------------------------------------------------------
def manager_with(runner) -> DeviceManager:
    return DeviceManager(Tools("adb", "fastboot"), runner)


def test_extraction_reports_encryption_honestly():
    phone = FakePhone()

    def shell(args, timeout=60.0, input_text=None, cwd=None):
        joined = " ".join(args)
        if "echo ok" in joined:
            return Result(args, 0, "ok")
        if "ls /data/media/0" in joined:
            return Result(args, 0, "Aq7bXm2p9RtY4vNz\nKp3wLs8dFg1hJk5m\nZx9cVb2nMq6wEr4t")
        return Result(args, 0, "")

    phone.run = shell  # type: ignore[assignment]
    engine = RescueEngine(manager_with(phone))
    outcome = engine.extract_from_recovery(dev(State.RECOVERY), "/tmp/out")

    assert not outcome.succeeded
    assert outcome.encrypted
    assert "no pc tool can decrypt them" in outcome.message.lower()


def test_extraction_works_when_storage_is_actually_readable(tmp_path):
    phone = FakePhone(files={"/data/media/0/DCIM/a.jpg": b"photo", "/data/media/0/Download/b.pdf": b"doc"})
    original_shell = phone._shell

    def shell(args, command):
        if command.startswith("ls /data/media/0"):
            return Result(args, 0, "DCIM\nDownload")
        return original_shell(args, command)

    phone._shell = shell  # type: ignore[assignment]
    engine = RescueEngine(manager_with(phone))
    outcome = engine.extract_from_recovery(dev(State.RECOVERY), tmp_path / "rescued")

    assert outcome.succeeded
    assert outcome.files_pulled == 2
    assert (tmp_path / "rescued" / "DCIM" / "a.jpg").read_bytes() == b"photo"


def test_extraction_gives_up_cleanly_without_an_adb_shell():
    phone = FakePhone()
    phone.run = lambda *a, **k: Result(["adb"], 1, "")  # type: ignore[assignment]
    outcome = RescueEngine(manager_with(phone)).extract_from_recovery(dev(State.RECOVERY), "/tmp/x")

    assert not outcome.succeeded
    assert "adb shell" in outcome.message


@pytest.mark.parametrize(
    "listing,expected",
    [
        ("DCIM Download Pictures Android", False),
        ("Aq7bXm2p9RtY4vNz Kp3wLs8dFg1hJk5m", True),
        ("", False),
        ("photo.jpg document.pdf", False),
    ],
)
def test_encrypted_name_heuristic(listing, expected):
    assert _looks_encrypted(listing) is expected
