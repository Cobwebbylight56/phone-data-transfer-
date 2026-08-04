from __future__ import annotations

import json

import pytest

from fake_phone import FakePhone
from ptransfer.adb import Adb
from ptransfer.backup import BackupEngine, BackupOptions
from ptransfer.devices import Device, State
from ptransfer.restore import RestoreEngine, RestoreOptions


@pytest.fixture
def bundle_path(tmp_path):
    source = FakePhone(
        files={
            "/sdcard/DCIM/Camera/IMG_1.jpg": b"photo",
            "/sdcard/Download/doc.pdf": b"document",
            "/data/app/com.example.app/base.apk": b"APK",
        },
        packages={"com.example.app": ["/data/app/com.example.app/base.apk"]},
        contacts="Row: 0 _id=1, lookup=k1, display_name=Ada Lovelace\n",
        sms="Row: 0 _id=1, address=+441234, date=1700000000000, type=1, body=hi\n",
        calls="Row: 0 _id=1, number=+441234, date=1700000000000, duration=5, type=1\n",
        vcards={"k1": "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ada Lovelace\r\nEND:VCARD\r\n"},
    )
    device = Device(serial="SRC", state=State.ONLINE, manufacturer="Nokia", model="Nokia 8.3")
    engine = BackupEngine(Adb("adb", source), device)
    report = engine.run(tmp_path / "src.ptbundle", BackupOptions(verify=False))
    return report.bundle_path


def test_media_and_apps_go_onto_the_new_phone(bundle_path):
    target = FakePhone()
    report = RestoreEngine(Adb("adb", target), "NEW").run(bundle_path, RestoreOptions())

    assert report.steps["media"].status == "ok"
    assert target.files["/sdcard/DCIM/Camera/IMG_1.jpg"] == b"photo"
    assert target.files["/sdcard/Download/doc.pdf"] == b"document"
    assert report.steps["apps"].status == "ok"
    assert "1 installed" in report.steps["apps"].message


def test_contacts_are_staged_and_flagged_for_the_user(bundle_path):
    target = FakePhone()
    report = RestoreEngine(Adb("adb", target), "NEW").run(bundle_path, RestoreOptions())

    step = report.steps["contacts"]
    assert step.status == "needs-user"
    assert "/sdcard/Download/ptransfer/contacts.vcf" in target.files
    assert any("Import" in t for t in step.todo)


def test_messages_say_plainly_that_a_pc_cannot_write_them(bundle_path):
    target = FakePhone()  # no companion app installed
    report = RestoreEngine(Adb("adb", target), "NEW").run(bundle_path, RestoreOptions())

    step = report.steps["sms"]
    assert step.status == "needs-user"
    assert "will not let a PC write them" in step.message
    assert "/sdcard/Download/ptransfer/sms.json" in target.files


def test_companion_app_changes_the_sms_advice(bundle_path):
    target = FakePhone(packages={"com.ptransfer.companion": ["/data/app/companion/base.apk"]})
    report = RestoreEngine(Adb("adb", target), "NEW").run(bundle_path, RestoreOptions())

    step = report.steps["sms"]
    assert step.status == "needs-user"
    assert "Companion app opened" in step.message
    assert any("default SMS app" in t for t in step.todo)


def test_sections_can_be_limited(bundle_path):
    target = FakePhone()
    report = RestoreEngine(Adb("adb", target), "NEW").run(
        bundle_path, RestoreOptions(sections=("media",))
    )

    assert report.steps["media"].status == "ok"
    assert report.steps["apps"].status == "skipped"
    assert report.steps["contacts"].status == "skipped"


def test_restore_survives_a_failing_install(bundle_path, monkeypatch):
    target = FakePhone()

    def refuse(args, timeout=60.0, input_text=None, cwd=None):
        if "install" in args:
            from ptransfer.proc import Result

            return Result(args, 1, stdout="Failure [INSTALL_FAILED_OLDER_SDK]")
        return FakePhone.run(target, args, timeout, input_text, cwd)

    monkeypatch.setattr(target, "run", refuse)
    report = RestoreEngine(Adb("adb", target), "NEW").run(bundle_path, RestoreOptions())

    assert report.steps["apps"].status == "failed"
    assert any("INSTALL_FAILED_OLDER_SDK" in t for t in report.steps["apps"].todo)
    assert report.steps["media"].status == "ok"  # unaffected


def test_round_trip_keeps_contact_content(bundle_path):
    target = FakePhone()
    RestoreEngine(Adb("adb", target), "NEW").run(bundle_path, RestoreOptions())
    vcf = target.files["/sdcard/Download/ptransfer/contacts.vcf"].decode()
    assert "Ada Lovelace" in vcf

    sms = json.loads(target.files["/sdcard/Download/ptransfer/sms.json"].decode())
    assert sms[0]["body"] == "hi"
