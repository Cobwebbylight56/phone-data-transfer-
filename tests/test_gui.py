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


def test_screen_page_refuses_a_phone_in_recovery(app):
    from ptransfer_gui.pages.screen_page import ScreenPage

    page = ScreenPage()
    page.set_device(Device(serial="S", state=State.RECOVERY))

    assert not page.connect_button.isEnabled()
    assert "physical buttons" in page.status.text()


def test_screen_page_enables_for_a_booted_phone(app):
    from ptransfer_gui.pages.screen_page import ScreenPage

    page = ScreenPage()
    page.set_device(Device(serial="S", state=State.ONLINE, manufacturer="HMD Global", model="Nokia 8.3"))
    assert page.connect_button.isEnabled()
    assert "Nokia 8.3" in page.hint.text()


def test_screen_view_turns_a_click_into_a_tap(app):
    from PySide6.QtCore import QPoint
    from ptransfer_gui.pages.screen_page import ScreenView

    view = ScreenView()
    view.device_size = (1080, 2340)
    view._pixmap_rect = (0, 0, 300, 650)

    taps: list[tuple] = []
    view.tapped.connect(lambda x, y: taps.append((x, y)))
    view._press = QPoint(150, 325)

    class E:
        def pos(self):
            return QPoint(150, 325)

    view.mouseReleaseEvent(E())
    assert taps == [(540, 1170)]


def test_screen_view_turns_a_drag_into_a_swipe(app):
    from PySide6.QtCore import QPoint
    from ptransfer_gui.pages.screen_page import ScreenView

    view = ScreenView()
    view.device_size = (1000, 2000)
    view._pixmap_rect = (0, 0, 100, 200)

    swipes: list[tuple] = []
    view.swiped.connect(lambda *a: swipes.append(a))
    view._press = QPoint(50, 180)

    class E:
        def pos(self):
            return QPoint(50, 20)

    view.mouseReleaseEvent(E())
    assert swipes == [(500, 1800, 500, 200)]


def test_passcode_field_is_masked_and_cleared_after_use(app):
    from PySide6.QtWidgets import QLineEdit
    from ptransfer_gui.pages.screen_page import ScreenPage

    page = ScreenPage()
    assert page.passcode.echoMode() == QLineEdit.EchoMode.Password

    sent: list[tuple] = []
    page.unlock_requested.connect(lambda c, n: sent.append((c, n)))
    page.passcode.setText("1234")
    page._unlock()

    assert sent == [("1234", True)]
    assert page.passcode.text() == ""  # never left on screen


def test_recovery_menu_panel_appears_for_a_phone_in_recovery(app):
    from ptransfer_gui.pages.rescue_page import RescuePage

    page = RescuePage()
    page.show_diagnosis(diagnose([Device(serial="S", state=State.RECOVERY, manufacturer="HMD Global")]))
    html = page.report.toHtml()

    assert "recovery menu" in html.lower()
    assert "Volume Up" in html
    assert "ERASES EVERYTHING" in html
    assert "factory reset" in html.lower()


def test_recovery_menu_panel_is_absent_in_fastboot(app):
    from ptransfer_gui.pages.rescue_page import RescuePage

    page = RescuePage()
    page.show_diagnosis(diagnose([Device(serial="S", state=State.BOOTLOADER, manufacturer="HMD Global")]))
    assert "You are looking at the recovery menu" not in page.report.toHtml()


def test_angle_brackets_in_advice_survive_the_html_render(app):
    """A filename like <file.zip> must not be swallowed as an HTML tag."""
    from ptransfer.menu import entry
    from ptransfer_gui.pages.rescue_page import RescuePage

    advice = entry("Apply update from ADB").advice
    assert "<file.zip>" in advice  # the source text really does contain one

    page = RescuePage()
    page.show_diagnosis(diagnose([Device(serial="S", state=State.RECOVERY, manufacturer="HMD Global")]))
    text = page.report.toPlainText()
    assert "--sideload <file.zip>" in text


def test_guide_page_renders_the_nokia_rescue_steps(app):
    from ptransfer_gui.pages.guide_page import GuidePage

    page = GuidePage()
    page.select_brand("nokia")
    text = page.view.toPlainText()

    assert "Nokia (HMD Global)" in text
    assert "Switch to the other system slot" in text
    assert "Volume Up" in text
