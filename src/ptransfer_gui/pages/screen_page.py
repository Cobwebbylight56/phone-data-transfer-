"""Live view of the phone's screen, with click and keyboard control."""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, Qt, QThread, Signal
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ptransfer.devices import Device
from ptransfer.screen import (
    SECURE_SCREEN_HINT,
    PhoneScreen,
    ScreenUnavailable,
    map_to_device,
    scrcpy_path,
)

log = logging.getLogger(__name__)

# A tap and a drag look identical until you see how far the mouse moved.
DRAG_THRESHOLD_PX = 12


class FrameStreamer(QThread):
    """Pulls frames off the phone without blocking the window."""

    frame = Signal(bytes, int, int)
    failed = Signal(str)

    def __init__(self, screen: PhoneScreen, interval_ms: int = 400) -> None:
        super().__init__()
        self.screen = screen
        self.interval_ms = interval_ms
        self._running = True

    def run(self) -> None:  # pragma: no cover - thread body
        while self._running:
            try:
                f = self.screen.capture()
            except ScreenUnavailable as exc:
                self.failed.emit(str(exc))
                return
            except Exception as exc:
                self.failed.emit(f"{type(exc).__name__}: {exc}")
                return
            if not self._running:
                return
            self.frame.emit(f.png, f.width, f.height)
            self.msleep(self.interval_ms)

    def stop(self) -> None:
        self._running = False


def is_mostly_black(image: QImage, samples: int = 24, threshold: int = 12) -> bool:
    """Is this frame effectively blank?

    Android returns an all-black screenshot for a secure surface rather than
    refusing outright, so a black frame is the only signal that the lock screen
    keypad is up. Sampled on a grid - reading every pixel of a 1440p frame
    several times a second would be wasteful.
    """
    if image.isNull() or image.width() < 2 or image.height() < 2:
        return False
    step_x = max(1, image.width() // samples)
    step_y = max(1, image.height() // samples)
    for y in range(0, image.height(), step_y):
        for x in range(0, image.width(), step_x):
            colour = image.pixelColor(x, y)
            if colour.red() > threshold or colour.green() > threshold or colour.blue() > threshold:
                return False
    return True


def embed_foreign_window(hwnd: int, parent: QWidget):
    """Adopt scrcpy's window into our own so there is one window, not two.

    Best-effort by nature - it reaches into the Windows API. Any failure just
    means scrcpy keeps its own window, which still works, so this never raises.
    """
    if not hwnd:
        return None
    try:
        from PySide6.QtGui import QWindow

        foreign = QWindow.fromWinId(hwnd)
        if foreign is None:
            return None
        container = QWidget.createWindowContainer(foreign, parent)
        container.setMinimumSize(300, 400)
        return container
    except Exception:  # pragma: no cover - platform dependent
        log.exception("could not embed the scrcpy window")
        return None


class ScreenView(QLabel):
    """The image itself. Turns mouse gestures into taps and swipes."""

    tapped = Signal(int, int)
    swiped = Signal(int, int, int, int)
    key_typed = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(300, 500)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet("background: #111; color: #999;")
        self.setText("Not connected")
        self.device_size = (0, 0)
        self.blank = False  # last frame came back entirely black (secure screen)
        self._press: QPoint | None = None
        self._pixmap_rect = (0, 0, 0, 0)  # x, y, w, h of the image inside the label

    def show_frame(self, png: bytes, width: int, height: int) -> None:
        image = QImage.fromData(png, "PNG")
        if image.isNull():
            return
        self.blank = is_mostly_black(image)
        self.device_size = (width or image.width(), height or image.height())
        pixmap = QPixmap.fromImage(image).scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(pixmap)
        x = (self.width() - pixmap.width()) // 2
        y = (self.height() - pixmap.height()) // 2
        self._pixmap_rect = (x, y, pixmap.width(), pixmap.height())

    def _to_device(self, pos: QPoint) -> tuple[int, int] | None:
        x0, y0, w, h = self._pixmap_rect
        if w <= 0 or h <= 0:
            return None
        rel_x, rel_y = pos.x() - x0, pos.y() - y0
        if not (0 <= rel_x < w and 0 <= rel_y < h):
            return None
        return map_to_device(rel_x, rel_y, w, h, *self.device_size)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        self._press = event.pos()
        self.setFocus()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press is None:
            return
        start, end = self._press, event.pos()
        self._press = None
        a = self._to_device(start)
        b = self._to_device(end)
        if a is None or b is None:
            return
        moved = (start - end).manhattanLength()
        if moved < DRAG_THRESHOLD_PX:
            self.tapped.emit(*a)
        else:
            self.swiped.emit(a[0], a[1], b[0], b[1])

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        self.key_typed.emit(event)


class ScreenPage(QWidget):
    tap_requested = Signal(int, int)
    swipe_requested = Signal(int, int, int, int)
    key_requested = Signal(str)
    text_requested = Signal(str)
    unlock_requested = Signal(str, bool)
    start_requested = Signal(object)
    stop_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.device: Device | None = None
        self.embedded: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Phone screen</h2>"))

        self.hint = QLabel(
            "Click to tap, drag to swipe, and type to send keystrokes. Needs a phone that is "
            "booted with USB debugging allowed - a phone in recovery or fastboot has no screen "
            "to show."
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #57606a;")
        layout.addWidget(self.hint)

        top = QHBoxLayout()
        self.connect_button = QPushButton("Start mirroring")
        self.connect_button.clicked.connect(self._toggle)
        top.addWidget(self.connect_button)

        self.smooth_check = QCheckBox("Smoother (0.15s refresh, more USB traffic)")
        self.smooth_check.setToolTip("Only used by the screenshot fallback; scrcpy is always full speed.")
        self.smooth_check.setChecked(False)
        top.addWidget(self.smooth_check)

        self.scrcpy_button = QPushButton("Open in scrcpy")
        self.scrcpy_button.setToolTip(
            "scrcpy mirrors the display itself, so it is smoother and it can show the lock "
            "screen, which a screenshot cannot."
        )
        top.addWidget(self.scrcpy_button)

        self.get_scrcpy_button = QPushButton("Get scrcpy")
        self.get_scrcpy_button.setToolTip("Download scrcpy (about 30 MB) so the lock screen can be seen.")
        top.addWidget(self.get_scrcpy_button)
        self.refresh_scrcpy_state()

        save = QPushButton("Save screenshot…")
        save.clicked.connect(self._save_shot)
        top.addWidget(save)
        top.addStretch(1)
        layout.addLayout(top)

        # Shown only when the phone returns a black frame, which is the one
        # case where the mirror looks broken but is working correctly.
        self.secure_notice = QLabel(SECURE_SCREEN_HINT)
        self.secure_notice.setWordWrap(True)
        self.secure_notice.setStyleSheet(
            "background:#3b2f00; color:#f0e0a0; padding:10px; border:1px solid #9a6700;"
        )
        self.secure_notice.setVisible(False)
        layout.addWidget(self.secure_notice)

        self.view = ScreenView()
        self.view.tapped.connect(self.tap_requested.emit)
        self.view.swiped.connect(self.swipe_requested.emit)
        self.view.key_typed.connect(self._on_key)
        layout.addWidget(self.view, 1)

        buttons = QGroupBox("Phone buttons")
        row = QHBoxLayout(buttons)
        for label, key in (
            ("◀ Back", "back"),
            ("● Home", "home"),
            ("■ Recents", "recents"),
            ("Power", "power"),
            ("Vol +", "volume_up"),
            ("Vol −", "volume_down"),
            ("Wake", "wakeup"),
        ):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self.key_requested.emit(k))
            row.addWidget(b)
        layout.addWidget(buttons)

        unlock_box = QGroupBox("Unlock and typing")
        unlock_row = QHBoxLayout(unlock_box)
        self.passcode = QLineEdit()
        self.passcode.setEchoMode(QLineEdit.EchoMode.Password)
        self.passcode.setPlaceholderText("PIN or password")
        self.passcode.returnPressed.connect(self._unlock)
        unlock_row.addWidget(self.passcode, 1)

        unlock = QPushButton("Unlock phone")
        unlock.clicked.connect(self._unlock)
        unlock_row.addWidget(unlock)

        type_button = QPushButton("Type text…")
        type_button.clicked.connect(self._type_text)
        unlock_row.addWidget(type_button)
        layout.addWidget(unlock_box)

        note = QLabel(
            "The passcode is sent to the phone and never stored. This only works on a phone that "
            "already trusts this computer for USB debugging — which you can only grant by "
            "unlocking the phone in your hand, so it is not a way past a lock screen."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #57606a; font-size: 11px;")
        layout.addWidget(note)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self._streaming = False

    # --- state ---------------------------------------------------------
    def set_device(self, device: Device | None) -> None:
        self.device = device
        if device is None:
            self.hint.setText("No phone selected.")
            self.connect_button.setEnabled(False)
            return
        self.connect_button.setEnabled(True)
        try:
            PhoneScreen.check_supported(device)
        except ScreenUnavailable as exc:
            self.connect_button.setEnabled(False)
            self.view.setText("No screen to mirror")
            self.status.setText(f"<span style='color:#9a6700'>{exc}</span>")
            return
        self.status.setText("")
        self.hint.setText(
            f"Ready to mirror <b>{device.label}</b>. Click to tap, drag to swipe, type to send keys."
        )

    def set_streaming(self, streaming: bool) -> None:
        self._streaming = streaming
        self.connect_button.setText("Stop mirroring" if streaming else "Start mirroring")

    def _toggle(self) -> None:
        if self._streaming:
            self.stop_requested.emit()
        elif self.device is not None:
            self.start_requested.emit(self.device)

    # --- input ---------------------------------------------------------
    def _on_key(self, event: QKeyEvent) -> None:
        key = event.key()
        special = {
            Qt.Key.Key_Backspace: "delete",
            Qt.Key.Key_Return: "enter",
            Qt.Key.Key_Enter: "enter",
            Qt.Key.Key_Escape: "back",
            Qt.Key.Key_Home: "home",
        }
        if key in special:
            self.key_requested.emit(special[key])
            return
        text = event.text()
        if text and text.isprintable():
            self.text_requested.emit(text)

    def _unlock(self) -> None:
        code = self.passcode.text()
        self.passcode.clear()
        self.unlock_requested.emit(code, code.isdigit())

    def _type_text(self) -> None:
        text, ok = QInputDialog.getText(self, "Type on the phone", "Text to send:")
        if ok and text:
            self.text_requested.emit(text)

    def _save_shot(self) -> None:
        pixmap = self.view.pixmap()
        if pixmap is None or pixmap.isNull():
            QMessageBox.information(self, "Nothing to save", "Start mirroring first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", "screen.png", "PNG image (*.png)")
        if path:
            pixmap.save(path, "PNG")
            self.status.setText(f"Saved to {path}")

    # --- feedback -------------------------------------------------------
    def show_frame(self, png: bytes, width: int, height: int) -> None:
        self.view.show_frame(png, width, height)
        # The secure-screen notice only matters on the screenshot fallback;
        # scrcpy shows the lock screen for real.
        self.secure_notice.setVisible(self.view.blank and self.embedded is None)
        if self.view.blank and self.embedded is None:
            self.passcode.setFocus()

    def refresh_scrcpy_state(self) -> None:
        installed = scrcpy_path() is not None
        self.scrcpy_button.setEnabled(installed)
        self.get_scrcpy_button.setVisible(not installed)

    # --- the embedded scrcpy view -------------------------------------
    def embed(self, hwnd: int) -> bool:
        """Swap the screenshot view for scrcpy's own window."""
        container = embed_foreign_window(hwnd, self)
        if container is None:
            return False
        self.clear_embedded()
        self.view.hide()
        self.secure_notice.setVisible(False)
        self.layout().insertWidget(self.layout().indexOf(self.view), container, 1)
        self.embedded = container
        self.hint.setText(
            "Live mirror through scrcpy. The lock screen shows here, and your mouse and keyboard "
            "go straight to the phone."
        )
        return True

    def clear_embedded(self) -> None:
        if self.embedded is not None:
            self.embedded.setParent(None)
            self.embedded.deleteLater()
            self.embedded = None
        self.view.show()

    def show_error(self, message: str) -> None:
        self.status.setText(f"<span style='color:#cf222e'>{message}</span>")

    def show_status(self, message: str) -> None:
        self.status.setText(message)

    def on_event(self, event) -> None:  # keeps the Job wiring uniform
        if getattr(event, "message", ""):
            self.status.setText(event.message)
