from __future__ import annotations

import json

import pytest

from fake_phone import FakePhone
from ptransfer.adb import Adb
from ptransfer.backup import BackupEngine, BackupOptions
from ptransfer.devices import Device, State
from ptransfer.manifest import Bundle
from ptransfer.progress import Event, Reporter

CONTACT_ROWS = (
    "Row: 0 _id=1, lookup=0r1-key, display_name=Ada Lovelace, has_phone_number=1\n"
    "Row: 1 _id=2, lookup=0r2-key, display_name=Alan Turing, has_phone_number=1\n"
)
SMS_ROWS = (
    "Row: 0 _id=1, thread_id=4, address=+447700900123, date=1700000000000, type=1, "
    "read=1, subject=NULL, body=Hello, how are you?\n"
    "Row: 1 _id=2, thread_id=4, address=+447700900123, date=1700000100000, type=2, "
    "read=1, subject=NULL, body=Fine, thanks\n"
)
CALL_ROWS = "Row: 0 _id=1, number=+447700900123, date=1700000000000, duration=61, type=2, name=Ada\n"

VCARDS = {
    "0r1-key": "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ada Lovelace\r\nTEL:+447700900123\r\nEND:VCARD\r\n",
    "0r2-key": "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Alan Turing\r\nTEL:+447700900999\r\nEND:VCARD\r\n",
}


@pytest.fixture
def phone() -> FakePhone:
    return FakePhone(
        files={
            "/sdcard/DCIM/Camera/IMG_0001.jpg": b"jpeg-bytes-1",
            "/sdcard/DCIM/Camera/IMG_0002.jpg": b"jpeg-bytes-22",
            "/sdcard/Download/manual.pdf": b"pdf",
            "/sdcard/Android/data/com.app/cache/junk.bin": b"junk-should-be-skipped",
        },
        packages={"com.example.app": ["/data/app/com.example.app/base.apk"]},
        contacts=CONTACT_ROWS,
        sms=SMS_ROWS,
        calls=CALL_ROWS,
        vcards=VCARDS,
    )


@pytest.fixture
def device() -> Device:
    return Device(
        serial="FAKE123",
        state=State.ONLINE,
        manufacturer="Sony",
        model="XQ-CT54",
        android_release="13",
        sdk=33,
    )


def run_backup(phone: FakePhone, device: Device, tmp_path, **kw):
    engine = BackupEngine(Adb("adb", phone), device)
    options = BackupOptions(**kw)
    return engine.run(tmp_path / "out.ptbundle", options)


def test_full_backup_copies_media_and_skips_app_cache(phone, device, tmp_path):
    report = run_backup(phone, device, tmp_path)
    bundle = Bundle.open(report.bundle_path)

    assert report.sections["media"].status == "ok"
    assert (bundle.media_dir / "DCIM/Camera/IMG_0001.jpg").read_bytes() == b"jpeg-bytes-1"
    assert (bundle.media_dir / "Download/manual.pdf").exists()
    # Android/data is excluded by default - it is app-private and mostly cache.
    assert not (bundle.media_dir / "Android").exists()
    assert report.sections["media"].files == 3


def test_contacts_exported_as_real_vcards(phone, device, tmp_path):
    report = run_backup(phone, device, tmp_path, sections=("contacts",))
    bundle = Bundle.open(report.bundle_path)
    text = (bundle.data_dir / "contacts.vcf").read_text()

    assert text.count("BEGIN:VCARD") == 2
    assert "Ada Lovelace" in text and "Alan Turing" in text
    assert report.sections["contacts"].items == 2
    assert report.sections["contacts"].status == "ok"


def test_contacts_fall_back_to_names_and_numbers(device, tmp_path):
    phone = FakePhone(contacts=CONTACT_ROWS, vcards={})  # provider gives no vCards
    report = run_backup(phone, device, tmp_path, sections=("contacts",))
    bundle = Bundle.open(report.bundle_path)

    section = report.sections["contacts"]
    assert section.status == "partial"
    assert "names and numbers only" in section.message
    assert "Ada Lovelace" in (bundle.data_dir / "contacts.vcf").read_text()


def test_sms_and_calls_are_normalised(phone, device, tmp_path):
    report = run_backup(phone, device, tmp_path, sections=("sms", "calls"))
    bundle = Bundle.open(report.bundle_path)

    messages = json.loads((bundle.data_dir / "sms.json").read_text())
    assert len(messages) == 2
    assert messages[0]["body"] == "Hello, how are you?"  # comma inside the body survives
    assert messages[0]["date"] < messages[1]["date"]     # sorted oldest first

    calls = json.loads((bundle.data_dir / "calls.json").read_text())
    assert calls[0]["number"] == "+447700900123"
    assert calls[0]["duration"] == 61


def test_provider_refusal_is_reported_not_swallowed(device, tmp_path):
    phone = FakePhone(provider_denied=True)
    report = run_backup(phone, device, tmp_path, sections=("sms",))
    section = report.sections["sms"]

    assert section.status == "failed"
    assert "companion app" in section.message


def test_apps_section_saves_apks_and_warns_about_app_data(phone, device, tmp_path):
    phone.files["/data/app/com.example.app/base.apk"] = b"APK-CONTENT"
    report = run_backup(phone, device, tmp_path, sections=("apps",))
    bundle = Bundle.open(report.bundle_path)

    assert (bundle.apps_dir / "com.example.app" / "base.apk").read_bytes() == b"APK-CONTENT"
    index = json.loads((bundle.apps_dir / "apps.json").read_text())
    assert index["packages"]["com.example.app"] == ["base.apk"]
    assert any("private data" in w for w in bundle.manifest.warnings)


def test_missing_files_are_reported_as_partial(device, tmp_path):
    phone = FakePhone(
        files={"/sdcard/DCIM/a.jpg": b"a", "/sdcard/DCIM/b.jpg": b"b"},
        unpullable={"/sdcard/DCIM/b.jpg"},
    )
    report = run_backup(phone, device, tmp_path, sections=("media",))
    section = report.sections["media"]

    assert section.status == "partial"
    assert "1 files unavailable" in section.message
    listing = (tmp_path / "out.ptbundle" / "logs" / "media-not-copied.txt").read_text()
    assert "/sdcard/DCIM/b.jpg" in listing


def test_second_run_skips_files_already_copied(phone, device, tmp_path):
    run_backup(phone, device, tmp_path, sections=("media",))
    before = len([c for c in phone.calls_made if c[1:2] == ["pull"] or "pull" in c])

    phone.calls_made.clear()
    report = run_backup(phone, device, tmp_path, sections=("media",))
    after = len([c for c in phone.calls_made if "pull" in c])

    assert report.sections["media"].status == "ok"
    assert after < before  # nothing re-copied


def test_checksums_written_and_verify_clean(phone, device, tmp_path):
    report = run_backup(phone, device, tmp_path, sections=("media", "deviceinfo"))
    bundle = Bundle.open(report.bundle_path)

    assert (bundle.root / "checksums.sha256").exists()
    assert bundle.verify() == []


def test_verify_detects_corruption(phone, device, tmp_path):
    report = run_backup(phone, device, tmp_path, sections=("media",))
    bundle = Bundle.open(report.bundle_path)
    target = bundle.media_dir / "DCIM/Camera/IMG_0001.jpg"
    target.write_bytes(b"tampered")

    problems = bundle.verify()
    assert any("corrupt" in p and "IMG_0001" in p for p in problems)


def test_progress_events_reach_the_reporter(phone, device, tmp_path):
    seen: list[Event] = []
    engine = BackupEngine(Adb("adb", phone), device, Reporter(seen.append))
    engine.run(tmp_path / "b.ptbundle", BackupOptions(sections=("media",), verify=False))

    kinds = {e.kind for e in seen}
    assert "section" in kinds and "status" in kinds
    assert any(e.section == "media" for e in seen)


def test_one_failing_section_does_not_abort_the_rest(device, tmp_path):
    phone = FakePhone(files={"/sdcard/DCIM/a.jpg": b"a"}, provider_denied=True)
    report = run_backup(phone, device, tmp_path, sections=("sms", "media", "deviceinfo"))

    assert report.sections["sms"].status == "failed"
    assert report.sections["media"].status == "ok"
    assert report.sections["deviceinfo"].status == "ok"


def test_manifest_records_the_source_phone(phone, device, tmp_path):
    report = run_backup(phone, device, tmp_path, sections=("deviceinfo",))
    bundle = Bundle.open(report.bundle_path)

    assert bundle.manifest.source.model == "XQ-CT54"
    assert bundle.manifest.source.manufacturer == "Sony"
    assert bundle.manifest.created_at and bundle.manifest.finished_at


def test_byte_counter_includes_the_last_file_of_each_folder(device, tmp_path):
    """The in-flight file must be counted when a folder's pull ends.

    Without that, every folder silently loses its final file from the running
    total and the progress bar drifts further below reality as it goes.
    """
    phone = FakePhone(
        files={
            "/sdcard/DCIM/a.jpg": b"a" * 100,
            "/sdcard/DCIM/b.jpg": b"b" * 100,
            "/sdcard/Download/c.pdf": b"c" * 100,
        }
    )
    seen: list[Event] = []
    engine = BackupEngine(Adb("adb", phone), device, Reporter(seen.append))
    engine.run(tmp_path / "b.ptbundle", BackupOptions(sections=("media",), verify=False))

    progress = [e for e in seen if e.kind == "progress" and e.total_bytes]
    assert progress, "expected byte-level progress events"
    assert progress[-1].done_bytes == progress[-1].total_bytes == 300
