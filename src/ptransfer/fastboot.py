"""fastboot wrapper.

Used read-only for diagnosis (``getvar``) plus the few safe recovery actions:
rebooting, and flashing/booting images the *user* supplies. The app never
unlocks a bootloader by itself - that wipes userdata, which is exactly what
someone trying to rescue their data must not do by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .proc import Cancel, Result, Runner, ToolError

_VAR = re.compile(r"^([\w\-\.:]+):\s*(.*)$")


@dataclass
class FastbootDevice:
    serial: str
    mode: str = "bootloader"  # or "fastbootd"
    vars: dict[str, str] = field(default_factory=dict)

    @property
    def unlocked(self) -> bool | None:
        v = (self.vars.get("unlocked") or "").lower()
        if v in ("yes", "true"):
            return True
        if v in ("no", "false"):
            return False
        return None

    @property
    def product(self) -> str:
        return self.vars.get("product", "")


class Fastboot:
    def __init__(self, path: str, runner: Runner | None = None) -> None:
        if not path:
            raise ToolError("fastboot was not found. Install platform-tools first.")
        self.path = path
        self.runner = runner or Runner()

    def _base(self, serial: str | None) -> list[str]:
        args = [self.path]
        if serial:
            args += ["-s", serial]
        return args

    def raw(self, args: Sequence[str], serial: str | None = None, timeout: float | None = 60.0) -> Result:
        return self.runner.run(self._base(serial) + list(args), timeout=timeout)

    def devices(self) -> list[FastbootDevice]:
        res = self.raw(["devices", "-l"], timeout=30)
        out = []
        for line in res.lines():
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            if state not in ("fastboot", "fastbootd"):
                continue
            out.append(FastbootDevice(serial, mode=state))
        return out

    def getvar_all(self, serial: str | None = None) -> dict[str, str]:
        res = self.raw(["getvar", "all"], serial, timeout=60)
        vars_: dict[str, str] = {}
        for line in res.output.splitlines():
            line = line.strip()
            if not line.startswith("(bootloader)"):
                continue
            m = _VAR.match(line.replace("(bootloader)", "", 1).strip())
            if m:
                vars_[m.group(1)] = m.group(2).strip()
        return vars_

    def getvar(self, name: str, serial: str | None = None) -> str:
        res = self.raw(["getvar", name], serial, timeout=30)
        for line in res.output.splitlines():
            if line.strip().startswith(name + ":"):
                return line.split(":", 1)[1].strip()
        return ""

    def describe(self, serial: str | None = None) -> FastbootDevice:
        dev = FastbootDevice(serial or "", vars=self.getvar_all(serial))
        return dev

    def reboot(self, target: str = "", serial: str | None = None) -> Result:
        args = ["reboot"] + ([target] if target else [])
        return self.raw(args, serial, timeout=60)

    # --- A/B slots ----------------------------------------------------
    def slots(self, serial: str | None = None) -> "SlotInfo":
        """Read the A/B slot state.

        On a device with two system slots, a failed update leaves the new slot
        unbootable while the *old* one still holds a working system. Switching
        back is the most effective data-safe repair there is - it rewrites
        nothing and touches userdata not at all.
        """
        vars_ = self.getvar_all(serial)
        if not vars_:
            # getvar all is refused by some bootloaders; ask for the few we need.
            for name in ("slot-count", "current-slot"):
                value = self.getvar(name, serial)
                if value:
                    vars_[name] = value
            for slot in ("a", "b"):
                for key in ("slot-successful", "slot-unbootable", "slot-retry-count"):
                    value = self.getvar(f"{key}:{slot}", serial)
                    if value:
                        vars_[f"{key}:{slot}"] = value
        return SlotInfo.from_vars(vars_)

    def set_active(self, slot: str, serial: str | None = None) -> Result:
        """Switch the active slot. Non-destructive: no partition is written."""
        slot = slot.lower().lstrip("_")
        if slot not in ("a", "b"):
            raise ValueError("A slot is 'a' or 'b'.")
        return self.raw(["set_active", slot], serial, timeout=60)

    def boot_image(
        self,
        image: str,
        serial: str | None = None,
        on_line: Callable[[str], None] | None = None,
        cancel: Cancel | None = None,
    ) -> Result:
        """Boot a recovery image without writing it. Non-destructive."""
        args = ["boot", image]
        if on_line is None:
            return self.raw(args, serial, timeout=300)
        return self.runner.stream(self._base(serial) + args, on_line, timeout=300, cancel=cancel)

    def flash(
        self,
        partition: str,
        image: str,
        serial: str | None = None,
        on_line: Callable[[str], None] | None = None,
        cancel: Cancel | None = None,
    ) -> Result:
        """Flash a user-supplied image.

        Guard rail: refuse the partitions that destroy user data. Repairing a
        soft-brick means rewriting *system* code, never userdata.
        """
        if partition.lower().split("_")[0] in DESTRUCTIVE_PARTITIONS:
            raise ValueError(
                f"Refusing to flash '{partition}': this erases your personal data. "
                "Back the phone up first, then use the vendor's own tool if you really mean to."
            )
        args = ["flash", partition, image]
        if on_line is None:
            return self.raw(args, serial, timeout=1800)
        return self.runner.stream(self._base(serial) + args, on_line, timeout=1800, cancel=cancel)


DESTRUCTIVE_PARTITIONS = {"userdata", "data", "metadata", "persist", "fsg", "modemst1", "modemst2"}


@dataclass
class SlotInfo:
    """State of an A/B device's two system slots."""

    count: int = 0
    current: str = ""
    successful: dict[str, bool] = field(default_factory=dict)
    unbootable: dict[str, bool] = field(default_factory=dict)
    retry_count: dict[str, int] = field(default_factory=dict)

    @property
    def is_ab(self) -> bool:
        return self.count >= 2

    @property
    def other(self) -> str:
        if not self.is_ab or self.current not in ("a", "b"):
            return ""
        return "b" if self.current == "a" else "a"

    def looks_broken(self, slot: str) -> bool:
        return self.unbootable.get(slot, False) or not self.successful.get(slot, True)

    @property
    def switch_is_promising(self) -> bool:
        """True when the current slot is bad and the other one looks healthy."""
        if not self.is_ab or not self.other:
            return False
        return self.looks_broken(self.current) and not self.looks_broken(self.other)

    def describe(self) -> str:
        if not self.is_ab:
            return "This phone has a single system slot, so there is no slot to switch to."
        bits = [f"Two system slots; currently booting from slot {self.current.upper() or '?'}."]
        for slot in ("a", "b"):
            if slot not in self.successful and slot not in self.unbootable:
                continue
            state = "unbootable" if self.unbootable.get(slot) else (
                "marked good" if self.successful.get(slot) else "not yet marked good"
            )
            retries = self.retry_count.get(slot)
            extra = f", {retries} boot attempts left" if retries is not None else ""
            bits.append(f"  slot {slot.upper()}: {state}{extra}")
        return "\n".join(bits)

    @classmethod
    def from_vars(cls, vars_: dict[str, str]) -> "SlotInfo":
        def flag(name: str) -> bool:
            return (vars_.get(name) or "").strip().lower() in ("yes", "true", "1")

        info = cls()
        try:
            info.count = int((vars_.get("slot-count") or "0").strip())
        except ValueError:
            info.count = 0
        info.current = (vars_.get("current-slot") or "").strip().lower().lstrip("_")
        for slot in ("a", "b"):
            if f"slot-successful:{slot}" in vars_:
                info.successful[slot] = flag(f"slot-successful:{slot}")
            if f"slot-unbootable:{slot}" in vars_:
                info.unbootable[slot] = flag(f"slot-unbootable:{slot}")
            raw = vars_.get(f"slot-retry-count:{slot}")
            if raw:
                try:
                    info.retry_count[slot] = int(raw.strip())
                except ValueError:
                    pass
        # Some bootloaders report has-slot:system instead of slot-count.
        if not info.count and (vars_.get("has-slot:system") or "").lower() in ("yes", "true"):
            info.count = 2
        return info
