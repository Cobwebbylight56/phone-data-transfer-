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
