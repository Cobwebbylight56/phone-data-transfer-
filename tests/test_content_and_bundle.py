from __future__ import annotations

import json

import pytest

from ptransfer.content import (
    ProviderError,
    check_provider_output,
    normalise_calls,
    normalise_sms,
    parse_rows,
    rows_to_vcard,
)
from ptransfer.manifest import Bundle, SectionResult, SourceDevice, safe_bundle_name


def test_row_parsing_keeps_commas_inside_values():
    text = "Row: 0 _id=1, address=+441234, body=Hi Bob, are you free at 5, ok?, type=1\n"
    rows = parse_rows(text)
    assert len(rows) == 1
    assert rows[0]["body"] == "Hi Bob, are you free at 5, ok?"
    assert rows[0]["type"] == "1"


def test_row_parsing_turns_null_into_empty():
    rows = parse_rows("Row: 0 _id=1, subject=NULL, body=hello\n")
    assert rows[0]["subject"] == ""


def test_row_parsing_ignores_noise():
    assert parse_rows("No result found.\nrandom text\n") == []


def test_security_exception_becomes_a_useful_error():
    with pytest.raises(ProviderError) as exc:
        check_provider_output("java.lang.SecurityException: Permission Denial: reading SmsProvider")
    assert "companion app" in str(exc.value)


def test_missing_provider_is_distinguished():
    with pytest.raises(ProviderError) as exc:
        check_provider_output("Error: Unable to resolve content://sms")
    assert "no provider" in str(exc.value)


def test_clean_output_passes():
    check_provider_output("Row: 0 _id=1")  # no exception


def test_vcard_fallback_escapes_special_characters():
    text = rows_to_vcard([{"display_name": "Smith, John;Jr", "number": "+441234"}])
    assert "FN:Smith\\, John\\;Jr" in text
    assert "TEL;TYPE=CELL:+441234" in text
    assert text.startswith("BEGIN:VCARD")


def test_sms_normalisation_sorts_and_types():
    rows = [
        {"address": "+1", "date": "200", "type": "2", "body": "second"},
        {"address": "+1", "date": "100", "type": "1", "body": "first"},
    ]
    out = normalise_sms(rows)
    assert [m["body"] for m in out] == ["first", "second"]
    assert out[0]["date"] == 100


def test_call_normalisation_handles_bad_numbers():
    out = normalise_calls([{"number": "+1", "date": "not-a-number", "duration": "", "type": "3"}])
    assert out[0]["date"] == 0
    assert out[0]["duration"] == 0
    assert out[0]["type"] == 3


# --- bundle -----------------------------------------------------------
def test_bundle_round_trip(tmp_path):
    source = SourceDevice(serial="S", manufacturer="Nokia", model="8.3", android_release="11", sdk=30)
    bundle = Bundle.create(tmp_path / "b.ptbundle", source, app_version="0.1.0")
    bundle.record(SectionResult("media", "ok", files=3, bytes=999))
    bundle.warn("something to know")
    bundle.finish()

    reopened = Bundle.open(tmp_path / "b.ptbundle")
    assert reopened.manifest.source.model == "8.3"
    assert reopened.manifest.sections["media"].files == 3
    assert reopened.manifest.warnings == ["something to know"]
    assert reopened.manifest.finished_at


def test_bundle_open_rejects_a_random_folder(tmp_path):
    (tmp_path / "notabundle").mkdir()
    with pytest.raises(FileNotFoundError):
        Bundle.open(tmp_path / "notabundle")


def test_checksums_detect_a_missing_file(tmp_path):
    bundle = Bundle.create(tmp_path / "b.ptbundle", SourceDevice())
    (bundle.media_dir / "a.txt").write_text("hello")
    bundle.write_checksums()
    (bundle.media_dir / "a.txt").unlink()

    problems = bundle.verify()
    assert any("missing" in p for p in problems)


def test_verify_without_checksums_says_so(tmp_path):
    bundle = Bundle.create(tmp_path / "b.ptbundle", SourceDevice())
    assert "cannot verify" in bundle.verify()[0]


def test_manifest_survives_an_interrupted_write(tmp_path):
    bundle = Bundle.create(tmp_path / "b.ptbundle", SourceDevice(model="X"))
    bundle.save()
    assert not (bundle.root / "manifest.json.tmp").exists()
    data = json.loads((bundle.root / "manifest.json").read_text())
    assert data["source"]["model"] == "X"


@pytest.mark.parametrize(
    "label,prefix",
    [("Sony XQ-CT54", "Sony-XQ-CT54-"), ("Nokia  8.3 / TA-1243", "Nokia-8.3-TA-1243-"), ("", "phone-")],
)
def test_bundle_names_are_filesystem_safe(label, prefix):
    name = safe_bundle_name(label)
    assert name.startswith(prefix)
    assert not set(name) & set('\\/:*?"<>|')
