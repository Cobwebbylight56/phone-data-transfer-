"""Phone to phone with the PC in the middle."""

from __future__ import annotations

import pytest

from fake_phone import FakePhone
from ptransfer.devices import Device, DeviceManager, State
from ptransfer.platform_tools import Tools
from ptransfer.proc import Result
from ptransfer.transfer import TransferEngine, TransferOptions, pick_pair

CONTACTS = "Row: 0 _id=1, lookup=k1, display_name=Ada Lovelace\n"
VCARDS = {"k1": "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ada Lovelace\r\nEND:VCARD\r\n"}


def old_phone() -> FakePhone:
    return FakePhone(
        files={
            "/sdcard/DCIM/Camera/IMG_1.jpg": b"photo-one",
            "/sdcard/DCIM/Camera/IMG_2.jpg": b"photo-two",
            "/sdcard/Download/notes.pdf": b"document",
            "/data/app/com.example/base.apk": b"APK",
        },
        packages={"com.example": ["/data/app/com.example/base.apk"]},
        contacts=CONTACTS,
        sms="Row: 0 _id=1, address=+441, date=1700000000000, type=1, body=hello\n",
        calls="Row: 0 _id=1, number=+441, date=1700000000000, duration=7, type=2\n",
        vcards=VCARDS,
    )


class TwoPhones(FakePhone):
    """One adb serving two phones - routes by the -s serial."""

    def __init__(self, source: FakePhone, target: FakePhone) -> None:
        super().__init__()
        self.phones = {"OLD": source, "NEW": target}

    def run(self, args, timeout=60.0, input_text=None, cwd=None):
        args = list(args)
        serial = args[args.index("-s") + 1] if "-s" in args else "OLD"
        phone = self.phones.get(serial)
        if phone is None:
            return Result(args, 1, stderr="device not found")
        return phone.run(args, timeout, input_text, cwd)


def device(serial: str, state=State.ONLINE, **kw) -> Device:
    kw.setdefault("manufacturer", "Nokia")
    kw.setdefault("model", f"Phone-{serial}")
    return Device(serial=serial, state=state, **kw)


def manager(runner) -> DeviceManager:
    return DeviceManager(Tools("adb", "fastboot"), runner)


# --- refusing early -----------------------------------------------------
def test_the_same_phone_on_both_sides_is_refused():
    engine = TransferEngine(manager(FakePhone()), device("A"), device("A"))
    problems = engine.preflight()

    assert problems
    assert "same phone is selected on both sides" in problems[0]


def test_an_unauthorised_phone_is_named_by_role():
    engine = TransferEngine(
        manager(FakePhone()), device("A", State.UNAUTHORIZED), device("B")
    )
    problems = engine.preflight()

    assert any("old phone has not authorised" in p for p in problems)


def test_a_target_in_recovery_is_refused_clearly():
    engine = TransferEngine(manager(FakePhone()), device("A"), device("B", State.RECOVERY))
    problems = engine.preflight()

    assert any("new phone is recovery" in p for p in problems)


def test_not_enough_disk_space_is_caught_before_starting(monkeypatch, tmp_path):
    engine = TransferEngine(manager(FakePhone()), device("A"), device("B"))
    monkeypatch.setattr(engine, "estimate_bytes", lambda: 50 * 1024**3)
    monkeypatch.setattr(TransferEngine, "free_space", staticmethod(lambda path: 1024**3))

    problems = engine.preflight(tmp_path)
    assert any("Not enough room" in p for p in problems)


def test_ready_pair_passes_preflight(monkeypatch, tmp_path):
    engine = TransferEngine(manager(FakePhone()), device("A"), device("B"))
    monkeypatch.setattr(engine, "estimate_bytes", lambda: 0)
    assert engine.preflight(tmp_path) == []


# --- the transfer itself -------------------------------------------------
def test_everything_lands_on_the_new_phone(tmp_path, monkeypatch):
    source, target = old_phone(), FakePhone()
    both = TwoPhones(source, target)
    engine = TransferEngine(manager(both), device("OLD"), device("NEW"))
    monkeypatch.setattr(engine, "estimate_bytes", lambda: 0)

    report = engine.run(tmp_path, TransferOptions(verify=False))

    assert report.ok
    assert target.files["/sdcard/DCIM/Camera/IMG_1.jpg"] == b"photo-one"
    assert target.files["/sdcard/Download/notes.pdf"] == b"document"
    # Contacts are staged on the new phone for the one-tap import.
    assert "/sdcard/Download/ptransfer/contacts.vcf" in target.files
    assert report.restore.steps["apps"].status == "ok"


def test_the_old_phone_is_only_read(tmp_path, monkeypatch):
    """A transfer must never write to the phone being replaced."""
    source, target = old_phone(), FakePhone()
    before = dict(source.files)
    both = TwoPhones(source, target)
    engine = TransferEngine(manager(both), device("OLD"), device("NEW"))
    monkeypatch.setattr(engine, "estimate_bytes", lambda: 0)

    engine.run(tmp_path, TransferOptions(verify=False))

    assert source.files == before
    pushes = [c for c in source.calls_made if "push" in c]
    assert pushes == []


def test_the_staging_copy_is_kept_as_a_backup(tmp_path, monkeypatch):
    source, target = old_phone(), FakePhone()
    engine = TransferEngine(manager(TwoPhones(source, target)), device("OLD"), device("NEW"))
    monkeypatch.setattr(engine, "estimate_bytes", lambda: 0)

    report = engine.run(tmp_path, TransferOptions(verify=False, keep_bundle=True))

    from pathlib import Path

    assert Path(report.bundle_path).exists()
    assert (Path(report.bundle_path) / "manifest.json").exists()
    assert "A full backup is kept at" in report.as_text()


def test_the_staging_copy_can_be_removed(tmp_path, monkeypatch):
    from pathlib import Path

    source, target = old_phone(), FakePhone()
    engine = TransferEngine(manager(TwoPhones(source, target)), device("OLD"), device("NEW"))
    monkeypatch.setattr(engine, "estimate_bytes", lambda: 0)

    report = engine.run(tmp_path, TransferOptions(verify=False, keep_bundle=False))

    assert report.bundle_path == ""
    assert not list(Path(tmp_path).glob("*.ptbundle"))


def test_a_source_that_gives_nothing_stops_before_writing(tmp_path, monkeypatch):
    """Do not touch the new phone when the old one yielded nothing."""
    source = FakePhone(provider_denied=True)  # no files, no providers
    target = FakePhone()
    engine = TransferEngine(manager(TwoPhones(source, target)), device("OLD"), device("NEW"))
    monkeypatch.setattr(engine, "estimate_bytes", lambda: 0)

    report = engine.run(tmp_path, TransferOptions(sections=("sms",), verify=False))

    assert not report.ok
    assert any("nothing to write to the new one" in p for p in report.problems)
    assert report.restore is None
    assert target.files == {}


def test_sections_are_carried_through_to_both_halves(tmp_path, monkeypatch):
    source, target = old_phone(), FakePhone()
    engine = TransferEngine(manager(TwoPhones(source, target)), device("OLD"), device("NEW"))
    monkeypatch.setattr(engine, "estimate_bytes", lambda: 0)

    report = engine.run(tmp_path, TransferOptions(sections=("media",), verify=False))

    assert report.backup.sections["media"].status == "ok"
    assert report.restore.steps["apps"].status == "skipped"
    assert report.restore.steps["contacts"].status == "skipped"


def test_settings_is_not_offered_to_the_restore_half():
    """Android will not let most settings be written back, so do not pretend."""
    options = TransferOptions(sections=("media", "settings", "deviceinfo", "contacts"))
    assert options.restore_sections == ("media", "contacts")


def test_report_lists_what_still_needs_a_tap(tmp_path, monkeypatch):
    source, target = old_phone(), FakePhone()
    engine = TransferEngine(manager(TwoPhones(source, target)), device("OLD"), device("NEW"))
    monkeypatch.setattr(engine, "estimate_bytes", lambda: 0)

    report = engine.run(tmp_path, TransferOptions(verify=False))
    todo = report.todo()

    assert any("contacts" in t for t in todo)
    assert "Finish these on the new phone" in report.as_text()


def test_preflight_failure_is_reported_not_raised(tmp_path):
    engine = TransferEngine(manager(FakePhone()), device("A"), device("A"))
    report = engine.run(tmp_path, TransferOptions(verify=False))

    assert not report.ok
    assert report.problems
    assert "Stopped:" in report.as_text()


# --- guessing the pair ---------------------------------------------------
def test_older_android_is_guessed_as_the_old_phone():
    a = device("A", sdk=30)
    b = device("B", sdk=34)
    source, target = pick_pair([b, a])

    assert source.serial == "A"
    assert target.serial == "B"


def test_one_phone_gives_a_source_and_no_target():
    source, target = pick_pair([device("A")])
    assert source.serial == "A"
    assert target is None


def test_phones_that_are_not_ready_are_not_offered():
    source, target = pick_pair([device("A", State.UNAUTHORIZED), device("B", State.RECOVERY)])
    assert source is None and target is None
