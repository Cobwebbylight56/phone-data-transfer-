"""One view of "what is plugged in", built from adb + fastboot + raw USB."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from .adb import Adb
from .fastboot import Fastboot
from .platform_tools import Tools, discover
from .proc import Runner, ToolError
from .usb import UsbDevice, enumerate_usb, interesting

log = logging.getLogger(__name__)


class State(str, Enum):
    ONLINE = "online"              # booted Android, debugging authorised
    UNAUTHORIZED = "unauthorized"  # cable fine, user hasn't tapped Allow
    OFFLINE = "offline"
    RECOVERY = "recovery"
    SIDELOAD = "sideload"
    BOOTLOADER = "bootloader"      # fastboot / fastbootd
    LOW_LEVEL = "low-level"        # EDL, Sony flash, MTK BROM, Odin...
    UNKNOWN = "unknown"

    @property
    def can_transfer(self) -> bool:
        return self in (State.ONLINE,)


@dataclass
class Device:
    serial: str
    state: State
    brand: str = ""
    manufacturer: str = ""
    model: str = ""
    device_name: str = ""
    android_release: str = ""
    sdk: int = 0
    build_id: str = ""
    usb: UsbDevice | None = None
    props: dict[str, str] = field(default_factory=dict)
    fastboot_vars: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def label(self) -> str:
        bits = [b for b in (self.manufacturer or self.brand, self.model) if b]
        name = " ".join(bits) or self.usb_label() or self.serial or "Unknown device"
        return name

    def usb_label(self) -> str:
        if self.usb and self.usb.mode():
            return self.usb.mode().detail  # type: ignore[union-attr]
        return self.usb.description if self.usb else ""

    @property
    def vendor_key(self) -> str:
        raw = (self.manufacturer or self.brand or "").lower()
        if not raw and self.usb:
            mode = self.usb.mode()
            if mode and mode.name.startswith("sony"):
                raw = "sony"
            elif mode and mode.name.startswith("nokia"):
                raw = "nokia"
        if "hmd" in raw or "nokia" in raw:
            return "nokia"
        return raw

    def summary(self) -> str:
        parts = [self.label, f"[{self.state.value}]"]
        if self.android_release:
            parts.append(f"Android {self.android_release} (SDK {self.sdk})")
        if self.serial:
            parts.append(f"serial={self.serial}")
        return "  ".join(parts)


PROP_KEYS = {
    "brand": "ro.product.brand",
    "manufacturer": "ro.product.manufacturer",
    "model": "ro.product.model",
    "device_name": "ro.product.device",
    "android_release": "ro.build.version.release",
    "build_id": "ro.build.display.id",
}


class DeviceManager:
    def __init__(self, tools: Tools | None = None, runner: Runner | None = None) -> None:
        self.tools = tools or discover()
        self.runner = runner or Runner()

    # --- tool handles -------------------------------------------------
    @property
    def adb(self) -> Adb:
        return Adb(self.tools.adb, self.runner)

    @property
    def fastboot(self) -> Fastboot:
        return Fastboot(self.tools.fastboot, self.runner)

    def tools_ready(self) -> tuple[bool, str]:
        missing = []
        if not self.tools.adb:
            missing.append("adb")
        if not self.tools.fastboot:
            missing.append("fastboot")
        if missing:
            return False, (
                f"Missing {', '.join(missing)}. Use 'Install platform-tools' in Settings, "
                "or drop Google's platform-tools folder next to the app."
            )
        return True, "ok"

    # --- detection ----------------------------------------------------
    def scan(self, deep: bool = True) -> list[Device]:
        devices: list[Device] = []
        seen_usb: set[int] = set()
        usb_devices = enumerate_usb(self.runner)

        if self.tools.adb:
            try:
                for d in self.adb.devices():
                    devices.append(self._from_adb(d, deep=deep))
            except ToolError as exc:
                log.warning("adb unavailable: %s", exc)

        if self.tools.fastboot:
            try:
                for f in self.fastboot.devices():
                    devices.append(self._from_fastboot(f, deep=deep))
            except ToolError as exc:
                log.warning("fastboot unavailable: %s", exc)

        # Anything visible only at the USB layer: the interesting case for a
        # bricked phone that adb/fastboot cannot see.
        for u in interesting(usb_devices):
            mode = u.mode()
            assert mode is not None
            if mode.name in ("android-fastboot", "sony-fastboot") and any(
                d.state is State.BOOTLOADER for d in devices
            ):
                continue
            if mode.name in ("sony-mtp",) and any(d.state.can_transfer for d in devices):
                continue
            if id(u) in seen_usb:
                continue
            devices.append(
                Device(
                    serial="",
                    state=State.LOW_LEVEL,
                    usb=u,
                    note=mode.actionable,
                )
            )

        # Attach driver-problem warnings to whatever we found.
        for d in devices:
            if d.usb is None:
                d.usb = _match_usb(d, usb_devices)
            if d.usb is not None and d.usb.has_driver_problem and not d.note:
                d.note = f"Windows driver problem: {d.usb.problem}"

        return devices

    def _from_adb(self, d, deep: bool) -> Device:
        state = {
            "device": State.ONLINE,
            "recovery": State.RECOVERY,
            "sideload": State.SIDELOAD,
            "unauthorized": State.UNAUTHORIZED,
            "offline": State.OFFLINE,
            "bootloader": State.BOOTLOADER,
        }.get(d.state, State.UNKNOWN)

        dev = Device(serial=d.serial, state=state, model=d.model)
        if state is State.UNAUTHORIZED:
            dev.note = (
                "Unlock the phone and tap 'Allow' on the USB debugging prompt. "
                "If no prompt appears, revoke USB debugging authorisations in "
                "Developer options and replug."
            )
        if deep and state in (State.ONLINE, State.RECOVERY):
            try:
                props = self.adb.all_props(d.serial)
            except ToolError:
                props = {}
            dev.props = props
            for attr, key in PROP_KEYS.items():
                setattr(dev, attr, props.get(key, "") or getattr(dev, attr))
            try:
                dev.sdk = int(props.get("ro.build.version.sdk", 0) or 0)
            except ValueError:
                dev.sdk = 0
        return dev

    def _from_fastboot(self, f, deep: bool) -> Device:
        dev = Device(serial=f.serial, state=State.BOOTLOADER)
        if deep:
            try:
                dev.fastboot_vars = self.fastboot.getvar_all(f.serial)
            except ToolError:
                dev.fastboot_vars = {}
        v = dev.fastboot_vars
        dev.model = v.get("product", "")
        dev.brand = _brand_from_fastboot(v)
        unlocked = (v.get("unlocked") or "").lower()
        if unlocked in ("no", "false"):
            dev.note = (
                "Bootloader is locked. That is fine for repair - stock signed images "
                "still flash. Do not unlock: unlocking wipes all your data."
            )
        return dev


def _brand_from_fastboot(vars_: dict[str, str]) -> str:
    blob = " ".join(vars_.values()).lower()
    for brand in ("sony", "nokia", "hmd", "xiaomi", "samsung", "motorola", "oneplus", "google", "oppo", "vivo", "realme"):
        if brand in blob:
            return "nokia" if brand == "hmd" else brand
    return ""


def _match_usb(dev: Device, usb_devices: list[UsbDevice]) -> UsbDevice | None:
    """Best-effort pairing of an adb/fastboot device with its USB entry."""
    if dev.serial:
        for u in usb_devices:
            if dev.serial.lower() in u.instance_id.lower():
                return u
    return None
