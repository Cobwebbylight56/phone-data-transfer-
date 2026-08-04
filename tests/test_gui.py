"""GUI smoke tests.

They run headless (offscreen platform) and are skipped when PySide6 is not
installed, so the suite still passes on a machine with only the CLI deps.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ptransfer.devices import Device, State  # noqa: E402
from ptransfer.progress import Event  # noqa: E402
from ptransfer.recovery import diagnose  # noqa: E402
from ptransfer.usb import UsbDevice  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_devices_page_renders_states(app):
    from ptransfer_gui.pages.devices_page import DevicesPage

    page = DevicesPage()
    page.set_devices(
        [
            Device(serial="S1", state=State.ONLINE, manufacturer="Sony", model="XQ-CT54", android_release="13"),
            Device(serial="", state=State.LOW_LEVEL, usb=UsbDevice("0fce", "adde")),
        ]
    )
    assert page.list.count() == 2
    assert page.current_device().serial == "S1"
    assert "XQ-CT54" in page.details.toHtml()


def test_devices_page_empty_state_explains_itself(app):
    from ptransfer_gui.pages.devices_page import DevicesPage

    page = DevicesPage()
    page.set_devices([])
    text = page.details.toPlainText()
    assert "cable" in text.lower()
    assert "Rescue tab" in text


def test_backup_page_disables_start_for_an_unreadable_phone(app):
    from ptransfer_gui.pages.backup_page import BackupPage

    page = BackupPage()
    page.set_device(Device(serial="S", state=State.RECOVERY))
    assert not page.start_button.isEnabled()

    page.set_device(Device(serial="S", state=State.ONLINE))
    assert page.start_button.isEnabled()


def test_backup_page_handles_progress_events(app):
    from ptransfer_gui.pages.backup_page import BackupPage

    page = BackupPage()
    page.on_event(Event("section", "media", "media"))
    page.on_event(Event("progress", "media", "copying", 0.5, 500, 1000))
    page.on_event(Event("warning", "media", "something odd"))

    assert page.progress.value() == 500
    assert "500" in page.status.text() or "KB" in page.status.text()
    assert "something odd" in page.log.toPlainText()


def test_rescue_page_renders_a_diagnosis_with_warnings(app):
    from ptransfer_gui.pages.rescue_page import RescuePage

    page = RescuePage()
    page.show_diagnosis(diagnose([Device(serial="S", state=State.RECOVERY, manufacturer="HMD Global")]))

    html = page.report.toHtml()
    assert "What to do, in order" in html
    assert "Warning" in html
    assert "Nokia" in page.headline.text() or "Nokia" in html


def test_rescue_page_marks_destructive_tools_in_red(app):
    from ptransfer_gui.pages.rescue_page import RescuePage

    page = RescuePage()
    page.show_diagnosis(diagnose([Device(serial="S", state=State.RECOVERY, manufacturer="Sony")]))
    html = page.report.toHtml()
    assert "ERASES DATA" in html


def test_guide_page_covers_every_brand(app):
    from ptransfer.oem import ALL_PROFILES
    from ptransfer_gui.pages.guide_page import GuidePage

    page = GuidePage()
    assert page.combo.count() == len(ALL_PROFILES)

    page.select_brand("sony")
    html = page.view.toHtml()
    assert "flash mode" in html.lower()
    assert "XperiFirm" in html


def test_restore_page_reads_a_bundle(app, tmp_path):
    from ptransfer.manifest import Bundle, SectionResult, SourceDevice
    from ptransfer_gui.pages.restore_page import RestorePage

    bundle = Bundle.create(tmp_path / "b.ptbundle", SourceDevice(manufacturer="Nokia", model="8.3", android_release="11"))
    bundle.record(SectionResult("media", "ok", files=10, bytes=2048))

    page = RestorePage()
    page._describe(str(bundle.root))
    text = page.bundle_info.text()
    assert "Nokia" in text and "8.3" in text
    assert "media" in text


def test_restore_page_reports_a_bad_folder(app, tmp_path):
    from ptransfer_gui.pages.restore_page import RestorePage

    page = RestorePage()
    page._describe(str(tmp_path))
    assert "not a transfer bundle" in page.bundle_info.text()


def test_main_window_builds(app, monkeypatch):
    monkeypatch.setattr("ptransfer.devices.enumerate_usb", lambda runner=None: [])
    from ptransfer_gui.main_window import PAGES, MainWindow

    window = MainWindow()
    assert window.stack.count() == len(PAGES)
    assert window.nav.count() == len(PAGES)
    window.close()


def test_nokia_panel_appears_only_for_a_nokia(app):
    from ptransfer_gui.pages.rescue_page import RescuePage

    page = RescuePage()
    assert not page.nokia_box.isVisible()

    page.show_diagnosis(diagnose([Device(serial="S", state=State.RECOVERY, manufacturer="Sony")]))
    assert not page.nokia_box.isVisibleTo(page)

    page.show_diagnosis(diagnose([Device(serial="S", state=State.RECOVERY, manufacturer="HMD Global")]))
    assert page.nokia_box.isVisibleTo(page)


def test_nokia_buttons_emit_their_action(app):
    from ptransfer_gui.pages.rescue_page import RescuePage

    page = RescuePage()
    page.show_diagnosis(diagnose([Device(serial="S", state=State.BOOTLOADER, manufacturer="HMD Global")]))

    seen: list[tuple] = []
    page.nokia_requested.connect(lambda d, a: seen.append((d, a)))
    page._nokia_switch_slot()
    page._nokia_guided()

    assert [a for _, a in seen] == ["switch-slot", "guided"]


def test_nokia_rescue_plan_names_the_slot_switch(app):
    from ptransfer_gui.pages.rescue_page import RescuePage

    page = RescuePage()
    page.show_diagnosis(diagnose([Device(serial="S", state=State.BOOTLOADER, manufacturer="HMD Global")]))
    html = page.report.toHtml()
    assert "slot" in html.lower()
