from __future__ import annotations

from ptransfer.devices import Device, DeviceManager, State
from ptransfer.fastboot import Fastboot
from ptransfer.oem import profile_for, profile_for_usb_vendor
from ptransfer.platform_tools import Tools
from ptransfer.proc import FakeRunner, Result
from ptransfer.usb import UsbDevice, interesting, parse_lsusb, parse_windows_pnp

WINDOWS_JSON = """[
 {"Name":"SOMC Flash Device","PNPDeviceID":"USB\\\\VID_0FCE&PID_ADDE\\\\5&1234","Status":"Error","ConfigManagerErrorCode":28},
 {"Name":"Android ADB Interface","PNPDeviceID":"USB\\\\VID_18D1&PID_4EE7\\\\R58M1234","Status":"OK","ConfigManagerErrorCode":0},
 {"Name":"USB Root Hub","PNPDeviceID":"USB\\\\ROOT_HUB30\\\\4&ABC","Status":"OK","ConfigManagerErrorCode":0}
]"""


def test_windows_pnp_parsing_finds_ids_and_driver_problems():
    devices = parse_windows_pnp(WINDOWS_JSON)
    assert len(devices) == 2  # the root hub has no VID/PID

    sony = devices[0]
    assert sony.key == ("0fce", "adde")
    assert sony.has_driver_problem
    assert "No driver installed" in sony.problem
    assert sony.mode().name == "sony-flash"

    assert not devices[1].has_driver_problem


def test_lsusb_parsing():
    text = (
        "Bus 001 Device 004: ID 05c6:9008 Qualcomm, Inc. Gobi Wireless Modem (QDL mode)\n"
        "Bus 001 Device 002: ID 8087:0024 Intel Corp. Integrated Rate Matching Hub\n"
    )
    devices = parse_lsusb(text)
    assert len(devices) == 2
    hits = interesting(devices)
    assert len(hits) == 1
    assert hits[0].mode().name == "qualcomm-edl"


def test_unknown_vendor_falls_back_to_vendor_wide_match():
    nokia = UsbDevice(vid="2e04", pid="ffff")
    assert nokia.mode() is not None
    assert nokia.mode().name == "nokia"


def test_usb_vendor_maps_to_a_brand_profile():
    assert profile_for_usb_vendor("0fce").key == "sony"
    assert profile_for_usb_vendor("04e8").key == "samsung"
    assert profile_for_usb_vendor("ffff") is None


def test_vendor_key_normalises_hmd_to_nokia():
    d = Device(serial="x", state=State.ONLINE, manufacturer="HMD Global")
    assert d.vendor_key == "nokia"
    assert profile_for(d.vendor_key).display_name.startswith("Nokia")


def test_vendor_key_from_usb_when_props_are_unavailable():
    d = Device(serial="", state=State.LOW_LEVEL, usb=UsbDevice("0fce", "adde"))
    assert d.vendor_key == "sony"


def test_profile_lookup_handles_long_manufacturer_strings():
    assert profile_for("Sony Ericsson Mobile Communications AB").key == "sony"
    assert profile_for("Xiaomi").key == "xiaomi"
    assert profile_for("Some Brand Nobody Has").key == "generic"


def test_scan_merges_adb_fastboot_and_usb(monkeypatch):
    runner = FakeRunner()
    runner.responses["adb devices -l"] = Result(["adb"], 0, "List of devices attached\nS1 device model:Nokia_8_3\n")
    runner.responses["adb -s S1 shell getprop"] = Result(
        ["adb"], 0, "[ro.product.manufacturer]: [HMD Global]\n[ro.product.model]: [Nokia 8.3]\n[ro.build.version.sdk]: [30]\n"
    )
    runner.responses["fastboot devices -l"] = Result(["fastboot"], 0, "S2 fastboot\n")
    runner.responses["fastboot -s S2 getvar all"] = Result(
        ["fastboot"], 0, stderr="(bootloader) product: pdx223\n(bootloader) unlocked: no\n"
    )
    monkeypatch.setattr("ptransfer.devices.enumerate_usb", lambda runner=None: [UsbDevice("05c6", "9008")])

    manager = DeviceManager(Tools("adb", "fastboot"), runner)
    devices = manager.scan()

    states = {d.state for d in devices}
    assert State.ONLINE in states
    assert State.BOOTLOADER in states
    assert State.LOW_LEVEL in states  # the EDL device adb/fastboot cannot see

    online = next(d for d in devices if d.state is State.ONLINE)
    assert online.manufacturer == "HMD Global"
    assert online.sdk == 30

    boot = next(d for d in devices if d.state is State.BOOTLOADER)
    assert boot.fastboot_vars["unlocked"] == "no"
    assert "do not unlock" in boot.note.lower()


def test_unauthorized_device_gets_actionable_note(monkeypatch):
    runner = FakeRunner()
    runner.responses["adb devices -l"] = Result(["adb"], 0, "List of devices attached\nS1 unauthorized\n")
    monkeypatch.setattr("ptransfer.devices.enumerate_usb", lambda runner=None: [])

    device = DeviceManager(Tools("adb", "fastboot"), runner).scan()[0]
    assert device.state is State.UNAUTHORIZED
    assert "Allow" in device.note


def test_tools_ready_reports_what_is_missing():
    ok, message = DeviceManager(Tools("", ""), FakeRunner()).tools_ready()
    assert not ok
    assert "adb" in message and "fastboot" in message


def test_fastboot_refuses_to_flash_userdata():
    fb = Fastboot("fastboot", FakeRunner())
    for partition in ("userdata", "USERDATA", "metadata", "persist"):
        try:
            fb.flash(partition, "img.img")
        except ValueError as exc:
            assert "erases your personal data" in str(exc)
        else:
            raise AssertionError(f"{partition} should have been refused")


def test_fastboot_allows_system_partitions():
    runner = FakeRunner()
    Fastboot("fastboot", runner).flash("boot_a", "boot.img")
    assert runner.calls[-1][:3] == ["fastboot", "flash", "boot_a"]


def test_fastboot_getvar_parsing():
    runner = FakeRunner()
    runner.responses["fastboot getvar all"] = Result(
        ["fastboot"], 0, stderr="(bootloader) version-bootloader: 1.0\n(bootloader) unlocked: yes\n"
    )
    vars_ = Fastboot("fastboot", runner).getvar_all()
    assert vars_["unlocked"] == "yes"
    assert vars_["version-bootloader"] == "1.0"
