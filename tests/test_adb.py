from __future__ import annotations

from ptransfer.adb import (
    Adb,
    RemoteFile,
    filter_inventory,
    parse_ls_recursive,
    parse_stat_inventory,
)
from ptransfer.proc import FakeRunner, Result


def test_devices_parses_states_and_props(runner: FakeRunner):
    runner.responses["adb devices -l"] = Result(
        ["adb"],
        0,
        stdout=(
            "List of devices attached\n"
            "R58M1234ABC            device product:beyond1lte model:SM_G973F device:beyond1lte\n"
            "0123456789ABCDEF       unauthorized usb:1-3\n"
            "emulator-5554          offline\n"
            "XPERIA123              recovery\n"
        ),
    )
    adb = Adb("adb", runner)
    devices = adb.devices()

    assert [d.serial for d in devices] == ["R58M1234ABC", "0123456789ABCDEF", "emulator-5554", "XPERIA123"]
    assert devices[0].state == "device"
    assert devices[0].model == "SM G973F"
    assert devices[1].state == "unauthorized"
    assert devices[3].state == "recovery"
    assert devices[0].usable and not devices[1].usable


def test_devices_ignores_daemon_chatter(runner: FakeRunner):
    runner.responses["adb devices -l"] = Result(
        ["adb"], 0, stdout="* daemon not running; starting now at tcp:5037\nList of devices attached\n"
    )
    assert Adb("adb", runner).devices() == []


def test_parse_stat_inventory():
    text = (
        "1024|1700000000|/sdcard/DCIM/Camera/IMG_0001.jpg\n"
        "2048|1700000100|/sdcard/Download/notes with spaces.pdf\n"
        "garbage line\n"
        "notanumber|x|/sdcard/bad\n"
    )
    files = parse_stat_inventory(text)
    assert len(files) == 2
    assert files[0] == RemoteFile("/sdcard/DCIM/Camera/IMG_0001.jpg", 1024, 1700000000)
    assert files[1].path.endswith("notes with spaces.pdf")


def test_parse_ls_recursive_fallback():
    text = (
        "/sdcard/DCIM:\n"
        "total 16\n"
        "drwxrwx--- 2 root sdcard_rw 4096 2023-01-01 10:00 Camera\n"
        "-rw-rw---- 1 root sdcard_rw 512 2023-01-01 10:00 note.txt\n"
    )
    files = parse_ls_recursive(text)
    assert [f.path for f in files] == ["/sdcard/DCIM/note.txt"]
    assert files[0].size == 512


def test_filter_inventory_excludes_app_private_dirs():
    files = [
        RemoteFile("/sdcard/DCIM/a.jpg", 1, 0),
        RemoteFile("/sdcard/Android/data/com.x/cache/b.bin", 1, 0),
        RemoteFile("/sdcard/Pictures/.thumbnails/c.jpg", 1, 0),
    ]
    kept = filter_inventory(files, "/sdcard", ("Android/data/*", "*/.thumbnails/*"))
    assert [f.path for f in kept] == ["/sdcard/DCIM/a.jpg"]


def test_list_files_falls_back_when_stat_unsupported(runner: FakeRunner):
    def fake_run(args, timeout=60.0, input_text=None, cwd=None):
        joined = " ".join(args)
        if "-exec stat" in joined:
            return Result(args, 1, stdout="", stderr="find: unknown option -exec")
        if "find" in joined:
            return Result(args, 0, stdout="/sdcard/DCIM/a.jpg\n/sdcard/DCIM/b.jpg\n")
        return Result(args, 0, "")

    runner.run = fake_run  # type: ignore[assignment]
    files, method = Adb("adb", runner).list_files("/sdcard")
    assert method == "find"
    assert len(files) == 2
    assert files[0].size == -1  # size unknown via this path


def test_third_party_packages(runner: FakeRunner):
    runner.responses["adb shell"] = Result(
        ["adb"], 0, stdout="package:com.whatsapp\npackage:com.example.app\n"
    )
    assert Adb("adb", runner).third_party_packages() == ["com.example.app", "com.whatsapp"]


def test_storage_root_prefers_sdcard(runner: FakeRunner):
    runner.default = Result(["adb"], 0, stdout="yes\n")
    assert Adb("adb", runner).storage_root() == "/sdcard"


def test_relative_to():
    f = RemoteFile("/sdcard/DCIM/Camera/x.jpg", 10, 0)
    assert f.relative_to("/sdcard") == "DCIM/Camera/x.jpg"
