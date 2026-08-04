from __future__ import annotations

import pytest

from fake_phone import FakePhone
from ptransfer import cli
from ptransfer.devices import DeviceManager
from ptransfer.platform_tools import Tools


@pytest.fixture
def patched_manager(monkeypatch):
    phone = FakePhone(files={"/sdcard/DCIM/a.jpg": b"photo"})

    def make(*args, **kwargs):
        return DeviceManager(Tools("adb", "fastboot"), phone)

    monkeypatch.setattr(cli, "DeviceManager", make)
    monkeypatch.setattr("ptransfer.devices.enumerate_usb", lambda runner=None: [])
    return phone


def test_devices_command_lists_the_phone(patched_manager, capsys):
    assert cli.main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "FAKE123" in out
    assert "online" in out


def test_info_command_reports_storage_and_battery(patched_manager, capsys):
    assert cli.main(["info"]) == 0
    out = capsys.readouterr().out
    assert "Battery: 78%" in out
    assert "Storage root: /sdcard" in out


def test_backup_command_creates_a_bundle(patched_manager, tmp_path, capsys):
    code = cli.main(["backup", "-o", str(tmp_path), "--sections", "media", "deviceinfo"])
    out = capsys.readouterr().out

    assert code == 0
    bundles = list(tmp_path.glob("*.ptbundle"))
    assert len(bundles) == 1
    assert (bundles[0] / "media" / "DCIM" / "a.jpg").read_bytes() == b"photo"
    assert "Summary:" in out


def test_verify_command_on_a_fresh_bundle(patched_manager, tmp_path, capsys):
    cli.main(["backup", "-o", str(tmp_path), "--sections", "media"])
    bundle = next(tmp_path.glob("*.ptbundle"))
    capsys.readouterr()

    assert cli.main(["verify", str(bundle)]) == 0
    assert "intact" in capsys.readouterr().out


def test_verify_command_flags_tampering(patched_manager, tmp_path, capsys):
    cli.main(["backup", "-o", str(tmp_path), "--sections", "media"])
    bundle = next(tmp_path.glob("*.ptbundle"))
    (bundle / "media" / "DCIM" / "a.jpg").write_bytes(b"changed")
    capsys.readouterr()

    assert cli.main(["verify", str(bundle)]) == 1
    assert "corrupt" in capsys.readouterr().out


def test_rescue_command_prints_a_plan(patched_manager, capsys):
    assert cli.main(["rescue"]) == 0
    out = capsys.readouterr().out
    assert "Situation:" in out
    assert "What to do, in order:" in out


def test_rescue_extract_refuses_outside_recovery(patched_manager, tmp_path, capsys):
    code = cli.main(["rescue", "--extract", str(tmp_path)])
    assert code == 1
    assert "only works while the phone is in recovery" in capsys.readouterr().out


def test_guide_lists_brands_then_details(capsys):
    assert cli.main(["guide"]) == 0
    assert "nokia" in capsys.readouterr().out

    assert cli.main(["guide", "sony"]) == 0
    out = capsys.readouterr().out
    assert "Sony Xperia" in out
    assert "flash" in out
    assert "ERASES DATA" in out  # the destructive tool is labelled as such


def test_guide_nokia_does_not_link_unlicensed_flashers(capsys):
    cli.main(["guide", "nokia"])
    out = capsys.readouterr().out.lower()
    assert "authorised service" in out or "service point" in out
    assert "nokia.com" in out


def test_missing_tools_exits_with_guidance(monkeypatch, capsys):
    monkeypatch.setattr(cli, "DeviceManager", lambda *a, **k: DeviceManager(Tools("", ""), FakePhone()))
    assert cli.main(["devices"]) == 2
    assert "ptransfer setup" in capsys.readouterr().out


def test_gui_entry_point_explains_a_missing_pyside(monkeypatch, capsys):
    """Double-clicking the shortcut must not produce a raw traceback."""
    import builtins

    import ptransfer_gui.app as app_mod

    real_import = builtins.__import__

    def no_pyside(name, *args, **kwargs):
        if name.startswith("PySide6"):
            raise ImportError("No module named 'PySide6'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyside)
    code = app_mod.run()

    assert code == 2
    err = capsys.readouterr().err
    assert "pip install PySide6" in err
    assert "command line" in err  # tells them what still works
