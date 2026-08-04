"""Getting the phone to show its "Allow USB debugging?" prompt.

There are two different situations behind "the PC cannot see my phone", and
they need opposite advice:

* **Debugging is on, this PC is not trusted yet.** adb lists the phone as
  ``unauthorized``. The prompt appears on the phone the moment adb tries to
  handshake - so the fix is to force a fresh handshake and wait. That is
  something this app can do.

* **Debugging is off entirely.** adb lists nothing at all, though Windows can
  still see the phone on USB. Nothing on a PC can switch it on: it lives behind
  the developer options, by design. All the app can do is say exactly which
  menu to tap, for the phone in front of you.

Telling those apart is most of the work, because the symptom is identical.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .devices import Device, DeviceManager, State
from .proc import Cancel, ToolError
from .progress import Reporter
from .usb import enumerate_usb, phones_on_usb, vendor_of

log = logging.getLogger(__name__)

# Where USB debugging lives, per brand. The build-number tap is universal; the
# path to it is not.
ENABLE_STEPS = {
    "samsung": [
        "On the phone: Settings > About phone > Software information",
        "Tap 'Build number' seven times. It counts down at the bottom.",
        "Enter your PIN if it asks.",
        "Go back to Settings > Developer options",
        "Turn on 'USB debugging'",
    ],
    "nokia": [
        "On the phone: Settings > About phone",
        "Tap 'Build number' seven times.",
        "Enter your PIN if it asks.",
        "Go back to Settings > System > Developer options",
        "Turn on 'USB debugging'",
    ],
    "": [
        "On the phone: Settings > About phone",
        "Tap 'Build number' seven times ('You are now a developer!' appears).",
        "Enter your PIN if it asks.",
        "Go back, then Settings > System > Developer options",
        "Turn on 'USB debugging'",
    ],
}

PROMPT_STEPS = [
    "Unlock the phone and keep the screen on - the prompt only appears while unlocked.",
    "Look for 'Allow USB debugging?' on the phone.",
    "Tick 'Always allow from this computer', then tap Allow.",
]

USB_MODE_TIP = (
    "If the phone shows 'USB for charging phone' in its notifications, tap that and choose "
    "'File transfer'. Some phones will not start the debug bridge in charging-only mode."
)


@dataclass
class AuthSituation:
    kind: str          # authorised | unauthorised | offline | debugging-off | nothing
    summary: str
    steps: list[str] = field(default_factory=list)
    device: Device | None = None
    can_prompt: bool = False   # is asking the phone worth doing at all?

    @property
    def ready(self) -> bool:
        return self.kind == "authorised"

    def as_text(self) -> str:
        out = [self.summary]
        if self.steps:
            out.append("")
            for i, step in enumerate(self.steps, 1):
                out.append(f"  {i}. {step}")
        return "\n".join(out)


@dataclass
class AuthResult:
    authorised: bool
    message: str
    seconds: float = 0.0


class Authorizer:
    def __init__(
        self,
        manager: DeviceManager,
        reporter: Reporter | None = None,
        cancel: Cancel | None = None,
    ) -> None:
        self.manager = manager
        self.report = reporter or Reporter()
        self.cancel = cancel or Cancel()

    # --- which of the two problems is it? -------------------------------
    def situation(self, brand_hint: str = "") -> AuthSituation:
        try:
            devices = self.manager.scan(deep=False)
        except ToolError as exc:
            return AuthSituation("nothing", f"adb is not usable: {exc}")

        online = [d for d in devices if d.state is State.ONLINE]
        if online:
            return AuthSituation(
                "authorised",
                f"{online[0].label} is connected and this computer is already trusted.",
                device=online[0],
            )

        unauthorised = [d for d in devices if d.state is State.UNAUTHORIZED]
        if unauthorised:
            return AuthSituation(
                "unauthorised",
                "USB debugging is switched on, but the phone has not been told to trust this "
                "computer yet. It will ask - that prompt is what we are after.",
                steps=list(PROMPT_STEPS),
                device=unauthorised[0],
                can_prompt=True,
            )

        offline = [d for d in devices if d.state is State.OFFLINE]
        if offline:
            return AuthSituation(
                "offline",
                "The phone is detected but the debug bridge will not connect. Usually a stale "
                "authorisation or a wedged adb daemon.",
                steps=[
                    "On the phone: Settings > Developer options > 'Revoke USB debugging authorisations'",
                    "Unplug the cable and plug it back in.",
                    "Run this again - the phone should ask for permission afresh.",
                ],
                device=offline[0],
                can_prompt=True,
            )

        # adb sees nothing. Does Windows see a phone at all? Checked against the
        # raw USB list rather than the rescue-mode table: a phone sitting in
        # ordinary file-transfer mode is in no rescue mode, but it is very much
        # plugged in, and that is the whole distinction being drawn here.
        on_usb = phones_on_usb(enumerate_usb(self.manager.runner))
        if on_usb:
            brand = brand_hint or vendor_of(on_usb[0])
            label = on_usb[0].description or "the phone"
            return AuthSituation(
                "debugging-off",
                f"Windows can see {label}, but adb cannot - so USB debugging is switched off. "
                "No program on a PC can turn it on; it has to be done on the phone.",
                steps=self.enable_steps(brand) + ["Then plug the cable back in and run this again.", USB_MODE_TIP],
                device=next((d for d in devices if d.usb is not None), None),
            )

        return AuthSituation(
            "nothing",
            "No phone detected at all - not even as a USB device.",
            steps=[
                "Use a cable you have moved files with. Charge-only cables look identical and "
                "are the most common cause.",
                "Use a USB port on the back of the PC, not a hub or a front panel.",
                "Unlock the phone and check its notifications for a USB option.",
                *self.enable_steps(brand_hint),
            ],
        )

    def enable_steps(self, brand: str = "") -> list[str]:
        key = (brand or "").strip().lower()
        for known in ENABLE_STEPS:
            if known and known in key:
                return list(ENABLE_STEPS[known])
        return list(ENABLE_STEPS[""])

    # --- make the prompt appear -----------------------------------------
    def request(self, timeout: float = 120.0, brand_hint: str = "") -> AuthResult:
        """Force a fresh handshake so the phone asks, then wait for the answer.

        Restarting the adb server is what actually re-triggers the prompt: the
        phone only asks when a client it does not recognise tries to connect,
        and a running daemon that was already refused will not ask again.
        """
        started = time.time()
        situation = self.situation(brand_hint)

        if situation.ready:
            return AuthResult(True, situation.summary, 0.0)

        if situation.kind == "debugging-off":
            return AuthResult(
                False,
                "USB debugging is off, so there is no prompt to trigger.\n\n" + situation.as_text(),
            )

        if situation.kind == "nothing":
            return AuthResult(False, situation.as_text())

        self.report.start_section("authorise", "Asking the phone for permission")
        self.report.status("Restarting the adb server so the phone is asked again")
        adb = self.manager.adb
        adb.kill_server()
        time.sleep(1)
        adb.start_server()

        # Any command that needs the device provokes the handshake.
        adb.raw(["devices"], timeout=30)
        serial = situation.device.serial if situation.device else None
        if serial:
            adb.shell("echo ok", serial, timeout=10)

        self.report.status("Look at the phone now - unlock it and tap Allow")

        while time.time() - started < timeout:
            self.cancel.raise_if_cancelled()
            elapsed = time.time() - started
            try:
                devices = adb.devices()
            except ToolError:
                devices = []

            for d in devices:
                if d.state == "device":
                    return AuthResult(
                        True,
                        "Authorised - the phone now trusts this computer. "
                        "If you ticked 'Always allow', it will not ask again.",
                        elapsed,
                    )

            self.report.progress(
                min(0.99, elapsed / timeout),
                f"waiting for you to tap Allow ({elapsed:.0f}s)",
                throttle=1.0,
            )
            time.sleep(2)

        return AuthResult(
            False,
            "No answer from the phone.\n\n"
            "The prompt only appears while the screen is unlocked, so unlock it and watch for "
            "'Allow USB debugging?'.\n"
            "If it never appears: on the phone go to Settings > Developer options > "
            "'Revoke USB debugging authorisations', unplug, plug back in, and try again.\n\n"
            + USB_MODE_TIP,
            time.time() - started,
        )
