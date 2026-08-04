"""Tests for the Nokia-specific rescue: slots, recovery logs, on-device OTA."""

from __future__ import annotations

import pytest

from fake_phone import FakePhone
from ptransfer.devices import Device, DeviceManager, State
from ptransfer.fastboot import Fastboot, SlotInfo
from ptransfer.nokia import NokiaRescue, is_nokia
from ptransfer.platform_tools import Tools
from ptransfer.proc import FakeRunner, Result
from ptransfer.usb import UsbDevice


def manager(runner) -> DeviceManager:
    return DeviceManager(Tools("adb", "fastboot"), runner)


def nokia_device(state=State.RECOVERY, **kw) -> Device:
    kw.setdefault("manufacturer", "HMD Global")
    kw.setdefault("model", "Nokia 8.3 5G")
    return Device(serial="NOK123", state=state, **kw)


# --- A/B slots --------------------------------------------------------
GETVAR_SLOTS = (
    "(bootloader) slot-count: 2\n"
    "(bootloader) current-slot: a\n"
    "(bootloader) slot-successful:a: no\n"
    "(bootloader) slot-unbootable:a: yes\n"
    "(bootloader) slot-successful:b: yes\n"
    "(bootloader) slot-unbootable:b: no\n"
    "(bootloader) slot-retry-count:b: 7\n"
    "(bootloader) unlocked: no\n"
)


def test_slot_info_parses_and_spots_the_broken_slot():
    runner = FakeRunner()
    runner.responses["fastboot getvar all"] = Result(["fastboot"], 0, stderr=GETVAR_SLOTS)
    info = Fastboot("fastboot", runner).slots()

    assert info.is_ab
    assert info.current == "a"
    assert info.other == "b"
    assert info.looks_broken("a")
    assert not info.looks_broken("b")
    assert info.switch_is_promising
    assert "slot A: unbootable" in info.describe()


def test_single_slot_device_has_nothing_to_switch_to():
    info = SlotInfo.from_vars({"slot-count": "1", "current-slot": ""})
    assert not info.is_ab
    assert info.other == ""
    assert not info.switch_is_promising
    assert "single system slot" in info.describe()


def test_slot_info_falls_back_to_individual_getvars():
    runner = FakeRunner()
    runner.responses["fastboot getvar all"] = Result(["fastboot"], 1, stderr="unknown command")
    runner.responses["fastboot getvar slot-count"] = Result(["fastboot"], 0, stderr="slot-count: 2\n")
    runner.responses["fastboot getvar current-slot"] = Result(["fastboot"], 0, stderr="current-slot: b\n")

    info = Fastboot("fastboot", runner).slots()
    assert info.is_ab and info.current == "b" and info.other == "a"


def test_has_slot_system_implies_ab():
    info = SlotInfo.from_vars({"has-slot:system": "yes", "current-slot": "a"})
    assert info.is_ab


def test_switch_slot_issues_set_active_and_reports_success():
    runner = FakeRunner()
    runner.responses["fastboot -s NOK123 getvar all"] = Result(["fastboot"], 0, stderr=GETVAR_SLOTS)
    runner.responses["fastboot -s NOK123 set_active b"] = Result(["fastboot"], 0, "OKAY")

    outcome = NokiaRescue(manager(runner)).switch_slot(nokia_device(State.BOOTLOADER))

    assert outcome.helped
    assert "slot B" in outcome.message
    assert ["fastboot", "-s", "NOK123", "set_active", "b"] in runner.calls
    assert "Nothing was erased" in outcome.detail


def test_switch_slot_explains_a_locked_bootloader_refusal():
    runner = FakeRunner()
    runner.responses["fastboot -s NOK123 getvar all"] = Result(["fastboot"], 0, stderr=GETVAR_SLOTS)
    runner.responses["fastboot -s NOK123 set_active b"] = Result(
        ["fastboot"], 1, stderr="FAILED (remote: 'Slot Change is not allowed in Lock State')"
    )

    outcome = NokiaRescue(manager(runner)).switch_slot(nokia_device(State.BOOTLOADER))

    assert outcome.status == "failed"
    # The important part: it is not a dead end, and the user is told why.
    assert "retries the other slot by itself" in outcome.detail


def test_switch_slot_needs_fastboot_and_says_how_to_get_there():
    outcome = NokiaRescue(manager(FakeRunner())).switch_slot(nokia_device(State.RECOVERY))
    assert outcome.status == "skipped"
    assert "Volume Down" in outcome.detail


def test_switch_slot_refuses_an_invalid_slot_name():
    runner = FakeRunner()
    with pytest.raises(ValueError):
        Fastboot("fastboot", runner).set_active("c")


# --- recovery log -----------------------------------------------------
LOG_DATA_MOUNT = """
[ 12.3] Starting recovery on Mon Aug  4 10:00:00 2026
E:failed to mount /data (Invalid argument)
E:Can't mount /data
"""

LOG_SLOT = "boot from slot a failed, slot a is unbootable\n"

LOG_WRONG_MODEL = "Package is for product TA-1234 but expected TA-1243\n"


def phone_with_log(text: str, path: str = "/cache/recovery/last_log") -> FakePhone:
    phone = FakePhone()
    original = phone._shell

    def shell(args, command):
        if command.startswith("cat "):
            if path in command:
                return Result(args, 0, text)
            return Result(args, 0, "")
        return original(args, command)

    phone._shell = shell  # type: ignore[assignment]
    return phone


def test_recovery_log_is_read_and_the_data_mount_failure_recognised():
    phone = phone_with_log(LOG_DATA_MOUNT)
    rescue = NokiaRescue(manager(phone))
    device = nokia_device(State.RECOVERY)

    text = rescue.read_recovery_log(device)
    assert "failed to mount /data" in text

    findings = rescue.analyse_log(text)
    assert len(findings) == 1
    assert findings[0].severity == "serious"
    # The single most important instruction on a phone with an unmountable /data.
    assert "do NOT accept any offer to format data" in findings[0].next_step.lower() or \
           "not accept any offer to format" in findings[0].next_step.lower()


def test_unbootable_slot_in_the_log_points_at_the_slot_switch():
    rescue = NokiaRescue(manager(FakeRunner()))
    findings = rescue.analyse_log(LOG_SLOT)
    assert findings and "other A/B slot" in findings[0].next_step


def test_wrong_model_firmware_is_flagged_as_serious():
    findings = NokiaRescue(manager(FakeRunner())).analyse_log(LOG_WRONG_MODEL)
    assert findings[0].severity == "serious"
    assert "different Nokia model" in findings[0].meaning


def test_verity_failure_recommends_the_slot_switch_first():
    findings = NokiaRescue(manager(FakeRunner())).analyse_log("dm-verity verification failed\n")
    assert "Switch A/B slot first" in findings[0].next_step


def test_unrecognised_log_produces_no_invented_findings():
    assert NokiaRescue(manager(FakeRunner())).analyse_log("everything is fine here") == []


def test_log_is_not_read_from_a_phone_in_fastboot():
    rescue = NokiaRescue(manager(FakePhone()))
    assert rescue.read_recovery_log(nokia_device(State.BOOTLOADER)) == ""


# --- on-device OTA ----------------------------------------------------
def ota_phone(paths: list[str]) -> FakePhone:
    phone = FakePhone()
    original = phone._shell

    def shell(args, command):
        if command.startswith("ls -1 "):
            directory = command.split()[2].split("/*")[0].strip("'\"")
            hits = [p for p in paths if p.rsplit("/", 1)[0] == directory]
            return Result(args, 0, "\n".join(hits))
        return original(args, command)

    phone._shell = shell  # type: ignore[assignment]
    return phone


def test_finds_a_downloaded_ota_the_phone_already_has():
    phone = ota_phone(["/data/ota_package/update.zip", "/cache/ota.zip"])
    found = NokiaRescue(manager(phone)).find_ota_packages(nokia_device(State.RECOVERY))
    assert found == ["/cache/ota.zip", "/data/ota_package/update.zip"]


def test_apply_ota_stages_the_package_and_tells_you_the_next_command(tmp_path):
    phone = ota_phone(["/data/ota_package/update.zip"])
    phone.files["/data/ota_package/update.zip"] = b"x" * (12 * 1024 * 1024)

    outcome = NokiaRescue(manager(phone)).apply_ota_from_device(
        nokia_device(State.RECOVERY), "/data/ota_package/update.zip", tmp_path
    )

    assert outcome.status == "needs-user"
    assert (tmp_path / "update.zip").exists()
    assert "Apply update from ADB" in outcome.detail
    assert "ptransfer rescue --sideload" in outcome.detail


def test_a_truncated_download_is_rejected_rather_than_offered(tmp_path):
    phone = ota_phone(["/cache/partial.zip"])
    phone.files["/cache/partial.zip"] = b"tiny"

    outcome = NokiaRescue(manager(phone)).apply_ota_from_device(
        nokia_device(State.RECOVERY), "/cache/partial.zip", tmp_path
    )
    assert outcome.status == "failed"
    assert "partial download" in outcome.message


# --- identification ---------------------------------------------------
def test_chipset_from_board_platform():
    device = nokia_device(State.ONLINE, props={"ro.board.platform": "sm7250"})
    ident = NokiaRescue(manager(FakePhone())).identify(device)
    assert ident.chipset == "Qualcomm"
    assert "9008" in ident.low_level_mode


def test_chipset_from_usb_when_the_phone_cannot_boot():
    device = Device(serial="", state=State.LOW_LEVEL, usb=UsbDevice("0e8d", "2000"))
    ident = NokiaRescue(manager(FakePhone())).identify(device)
    assert ident.chipset == "MediaTek"
    assert "BROM" in ident.low_level_mode


def test_unisoc_models_are_recognised():
    device = nokia_device(State.ONLINE, model="Nokia G21", props={})
    ident = NokiaRescue(manager(FakePhone())).identify(device)
    assert ident.chipset == "Unisoc"
    assert "verify if it matters" in ident.chipset_source


def test_model_prefix_lookup_prefers_the_longest_match():
    rescue = NokiaRescue(manager(FakePhone()))
    ident = rescue.identify(nokia_device(State.ONLINE, model="Nokia 5.3", props={}))
    assert ident.chipset == "Qualcomm"


def test_is_nokia_accepts_hmd_branding():
    assert is_nokia(nokia_device())
    assert not is_nokia(Device(serial="x", state=State.ONLINE, manufacturer="Sony"))


# --- the guided run ---------------------------------------------------
def test_guided_rescue_gathers_everything_it_can_from_recovery():
    phone = phone_with_log(LOG_SLOT)
    report = NokiaRescue(manager(phone)).guided_rescue(nokia_device(State.RECOVERY))

    names = {s.name: s for s in report.steps}
    assert names["read recovery log"].status == "ok"
    assert report.findings
    # No fastboot from recovery, so the slot check must say so rather than guess.
    assert names["slot check"].status == "skipped"
    assert "Volume Down" in names["slot check"].detail

    text = report.as_text()
    assert "Nokia rescue report" in text
    assert "What the recovery log says" in text


def test_guided_rescue_from_fastboot_recommends_the_switch():
    runner = FakeRunner()
    runner.responses["fastboot -s NOK123 getvar all"] = Result(["fastboot"], 0, stderr=GETVAR_SLOTS)
    report = NokiaRescue(manager(runner)).guided_rescue(nokia_device(State.BOOTLOADER))

    slot_step = next(s for s in report.steps if s.name == "slot check")
    assert slot_step.status == "ok"
    assert "very likely to fix this" in slot_step.message
    assert "--switch-slot" in slot_step.detail
    assert "slot A: unbootable" in report.as_text()


def test_guided_rescue_on_a_working_phone_says_back_up_first():
    phone = FakePhone()
    report = NokiaRescue(manager(phone)).guided_rescue(nokia_device(State.ONLINE))
    first = report.steps[0]
    assert first.name == "back up first"
    assert "ptransfer backup" in first.detail


# --- the step shown depends on the mode the phone is in ---------------
def test_fastboot_plan_leads_with_the_slot_switch():
    from ptransfer.recovery import diagnose

    plan = diagnose([nokia_device(State.BOOTLOADER)])
    titles = [s.title for s in plan.steps]
    assert any("other system slot" in t for t in titles)
    # It must come before the generic "reflash the system" advice.
    assert titles.index(next(t for t in titles if "other system slot" in t)) < titles.index(
        next(t for t in titles if "Reflash" in t)
    )


def test_recovery_plan_offers_the_on_device_ota_not_the_slot_switch():
    from ptransfer.recovery import diagnose

    plan = diagnose([nokia_device(State.RECOVERY)])
    titles = [s.title for s in plan.steps]
    assert any("already downloaded" in t for t in titles)
    # Slot switching needs fastboot, so it must not be suggested from recovery.
    assert not any("other system slot" in t for t in titles)


def test_sony_plan_is_unaffected_by_the_nokia_specific_steps():
    from ptransfer.recovery import diagnose

    plan = diagnose([Device(serial="S", state=State.BOOTLOADER, manufacturer="Sony")])
    assert plan.profile.key == "sony"
    assert not any("slot" in s.title.lower() for s in plan.steps)
