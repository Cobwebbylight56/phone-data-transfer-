"""Screen mirroring, remote input, and the annotated recovery menu."""

from __future__ import annotations

import struct
import zlib

import pytest

from fake_phone import FakePhone
from ptransfer.adb import Adb
from ptransfer.devices import Device, State
from ptransfer.menu import MENU, SAFE_ORDER, as_text, destructive_entries, entry, safe_entries
from ptransfer.proc import FakeRunner, Result
from ptransfer.screen import (
    PNG_MAGIC,
    PhoneScreen,
    ScreenUnavailable,
    escape_input_text,
    map_to_device,
    parse_lock_state,
    parse_wm_size,
)


def tiny_png() -> bytes:
    """A real 1x1 PNG - the parsers must accept genuine bytes, not a stub."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xff\xff")
    return PNG_MAGIC + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def online(**kw) -> Device:
    return Device(serial="S1", state=State.ONLINE, manufacturer="HMD Global", **kw)


# --- what can be mirrored at all --------------------------------------
def test_a_booted_phone_is_supported():
    PhoneScreen.check_supported(online())  # no exception


@pytest.mark.parametrize(
    "state,expected",
    [
        (State.RECOVERY, "physical buttons"),
        (State.BOOTLOADER, "no screen to mirror"),
        (State.UNAUTHORIZED, "tap 'Allow'"),
        (State.OFFLINE, "booted phone"),
    ],
)
def test_unmirrorable_states_explain_themselves(state, expected):
    with pytest.raises(ScreenUnavailable) as exc:
        PhoneScreen.check_supported(Device(serial="S", state=state))
    assert expected in str(exc.value)


def test_recovery_message_points_at_the_menu_helper():
    with pytest.raises(ScreenUnavailable) as exc:
        PhoneScreen.check_supported(Device(serial="S", state=State.RECOVERY))
    message = str(exc.value)
    assert "Volume Up/Down" in message and "Power to select" in message


# --- capture -----------------------------------------------------------
def test_capture_returns_the_png():
    runner = FakeRunner()
    runner.binary_responses["adb -s S1 exec-out screencap -p"] = tiny_png()
    runner.responses["adb -s S1 shell wm size"] = Result(["adb"], 0, "Physical size: 1080x2400")

    frame = PhoneScreen(Adb("adb", runner), "S1").capture()

    assert frame.ok
    assert frame.width == 1080 and frame.height == 2400


def test_capture_repairs_crlf_mangling():
    """Some old builds turn \\n into \\r\\n on the way out, breaking the PNG."""
    runner = FakeRunner()
    runner.binary_responses["adb exec-out screencap -p"] = tiny_png().replace(b"\n", b"\r\n")
    runner.responses["adb shell wm size"] = Result(["adb"], 0, "Physical size: 720x1280")

    frame = PhoneScreen(Adb("adb", runner)).capture()
    assert frame.ok
    assert frame.png.startswith(PNG_MAGIC)


def test_empty_capture_blames_secure_screens():
    runner = FakeRunner()
    runner.binary_responses["adb exec-out screencap -p"] = b""

    with pytest.raises(ScreenUnavailable) as exc:
        PhoneScreen(Adb("adb", runner)).capture()
    assert "secure screens" in str(exc.value)


def test_non_png_output_is_surfaced_not_swallowed():
    runner = FakeRunner()
    runner.binary_responses["adb exec-out screencap -p"] = b"error: closed"

    with pytest.raises(ScreenUnavailable) as exc:
        PhoneScreen(Adb("adb", runner)).capture()
    assert "error: closed" in str(exc.value)


# --- screen size -------------------------------------------------------
def test_override_size_beats_physical():
    size = parse_wm_size("Physical size: 1440x3120\nOverride size: 1080x2340")
    assert (size.width, size.height) == (1080, 2340)


def test_physical_size_used_when_there_is_no_override():
    size = parse_wm_size("Physical size: 1080x2400")
    assert (size.width, size.height) == (1080, 2400)
    assert size.valid


def test_unparseable_size_is_not_valid():
    assert not parse_wm_size("something went wrong").valid


def test_size_is_cached_until_refreshed():
    runner = FakeRunner()
    runner.responses["adb shell wm size"] = Result(["adb"], 0, "Physical size: 1080x2400")
    screen = PhoneScreen(Adb("adb", runner))

    screen.size()
    screen.size()
    assert len([c for c in runner.calls if "wm" in " ".join(c)]) == 1


# --- coordinate mapping ------------------------------------------------
def test_click_maps_onto_the_phone_resolution():
    # A click in the middle of a 300x650 preview of a 1080x2340 screen.
    assert map_to_device(150, 325, 300, 650, 1080, 2340) == (540, 1170)


def test_mapping_clamps_to_the_screen():
    x, y = map_to_device(999, 999, 300, 650, 1080, 2340)
    assert x == 1079 and y == 2339


def test_mapping_survives_a_zero_sized_view():
    assert map_to_device(10, 10, 0, 0, 1080, 2340) == (0, 0)


# --- input -------------------------------------------------------------
def test_tap_and_swipe_issue_the_right_commands():
    phone = FakePhone()
    screen = PhoneScreen(Adb("adb", phone), "S1")

    screen.tap(100, 200)
    screen.swipe(10, 20, 30, 40, 150)

    issued = [" ".join(c) for c in phone.calls_made]
    assert any("input tap 100 200" in c for c in issued)
    assert any("input swipe 10 20 30 40 150" in c for c in issued)


def test_named_keys_become_keycodes():
    phone = FakePhone()
    screen = PhoneScreen(Adb("adb", phone))

    screen.key("home")
    screen.key("back")
    issued = [" ".join(c) for c in phone.calls_made]
    assert any("input keyevent 3" in c for c in issued)
    assert any("input keyevent 4" in c for c in issued)


def test_digits_are_sent_as_keycodes_not_text():
    """The lock screen accepts keycodes reliably; 'input text' often not."""
    phone = FakePhone()
    PhoneScreen(Adb("adb", phone)).type_digits("1902")

    issued = [" ".join(c) for c in phone.calls_made]
    codes = [c.split("keyevent ")[1].strip("'\" ") for c in issued if "keyevent" in c]
    assert codes == ["8", "16", "7", "9"]  # KEYCODE_0 is 7


def test_type_digits_refuses_non_numeric():
    assert PhoneScreen(Adb("adb", FakePhone())).type_digits("12a4") is False


@pytest.mark.parametrize(
    "raw,escaped",
    [
        ("hello world", "hello%sworld"),
        ("a&b", "a\\&b"),
        ("quote'it", "quote\\'it"),
        ("plain", "plain"),
    ],
)
def test_input_text_escaping(raw, escaped):
    assert escape_input_text(raw) == escaped


def test_typing_sends_escaped_text():
    phone = FakePhone()
    PhoneScreen(Adb("adb", phone)).type_text("hello world")
    assert any("hello%sworld" in " ".join(c) for c in phone.calls_made)


def test_empty_text_is_a_no_op():
    phone = FakePhone()
    assert PhoneScreen(Adb("adb", phone)).type_text("") is True
    assert phone.calls_made == []


# --- lock screen -------------------------------------------------------
@pytest.mark.parametrize(
    "dump,expected",
    [
        ("mDreamingLockscreen=true", True),
        ("mDreamingLockscreen=false", False),
        ("isStatusBarKeyguard=true", True),
        ("nothing useful here", None),
    ],
)
def test_lock_state_parsing(dump, expected):
    assert parse_lock_state(dump) is expected


def test_unlock_wakes_swipes_and_enters_the_pin():
    runner = FakeRunner()
    runner.responses["adb shell wm size"] = Result(["adb"], 0, "Physical size: 1000x2000")
    runner.responses["adb shell dumpsys window"] = Result(["adb"], 0, "mDreamingLockscreen=false")

    message = PhoneScreen(Adb("adb", runner)).unlock("1234")

    issued = [" ".join(c) for c in runner.calls]
    assert any("keyevent 224" in c for c in issued)          # wake
    assert any("input swipe 500 1500 500 400" in c for c in issued)  # dismiss
    assert any("keyevent 8" in c for c in issued)            # the '1'
    assert any("keyevent 66" in c for c in issued)           # enter
    assert message == "Unlocked."


def test_unlock_reports_a_rejected_passcode_honestly():
    runner = FakeRunner()
    runner.responses["adb shell wm size"] = Result(["adb"], 0, "Physical size: 1000x2000")
    runner.responses["adb shell dumpsys window"] = Result(["adb"], 0, "mDreamingLockscreen=true")

    message = PhoneScreen(Adb("adb", runner)).unlock("0000")
    assert "still locked" in message
    assert "pattern" in message


def test_unlock_without_a_passcode_only_wakes_the_phone():
    runner = FakeRunner()
    runner.responses["adb shell wm size"] = Result(["adb"], 0, "Physical size: 1000x2000")

    message = PhoneScreen(Adb("adb", runner)).unlock("")
    assert "No passcode was given" in message
    assert not any("keyevent 66" in " ".join(c) for c in runner.calls)


def test_alphanumeric_passcode_goes_through_input_text():
    runner = FakeRunner()
    runner.responses["adb shell wm size"] = Result(["adb"], 0, "Physical size: 1000x2000")

    PhoneScreen(Adb("adb", runner)).unlock("hunter2")
    assert any("input text" in " ".join(c) for c in runner.calls)


# --- the recovery menu -------------------------------------------------
def test_factory_reset_is_marked_destructive_and_the_rest_are_not():
    labels = {e.label for e in destructive_entries()}
    assert "Wipe data/factory reset" in labels
    assert "Format data / Format userdata" in labels
    assert "Wipe cache partition" in {e.label for e in safe_entries()}
    assert "Apply update from ADB" in {e.label for e in safe_entries()}


def test_the_safe_sequence_never_includes_a_wipe():
    for label in SAFE_ORDER:
        e = entry(label)
        assert e is not None, label
        assert not e.destroys_data


def test_cache_wipe_explains_that_a_missing_entry_is_normal():
    e = entry("Wipe cache partition")
    assert "no cache partition" in e.advice
    assert "normal, not a fault" in e.advice


def test_factory_reset_warns_that_the_data_is_gone_for_good():
    e = entry("Wipe data/factory reset")
    assert e.destroys_data
    assert "encryption key" in e.advice
    assert "no tool" in e.advice.lower() or "nothing is recoverable" in e.advice.lower()


def test_menu_lookup_is_forgiving():
    assert entry("wipe cache") is not None
    assert entry("REBOOT SYSTEM NOW").label == "Reboot system now"
    assert entry("nonsense") is None


def test_menu_text_leads_with_the_buttons_and_ends_with_the_warning():
    text = as_text()
    assert "Volume Up and Volume Down" in text
    assert "Power button selects" in text
    assert "ERASES EVERYTHING" in text
    # The safe sequence must appear before the full option list.
    assert text.index("Try these, in this order") < text.index("Every option")


def test_every_menu_entry_says_what_it_does():
    for e in MENU:
        assert e.does
        if e.destroys_data:
            assert e.advice, f"{e.label} must carry a warning"
