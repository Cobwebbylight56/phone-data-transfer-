"""adb wrapper.

Only user-authorised operations are used: the phone must accept the USB
debugging prompt. Nothing here attempts to defeat a lock screen, factory reset
protection, or any other security control - if the phone says no, we report it.
"""

from __future__ import annotations

import logging
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .proc import Cancel, Result, Runner, ToolError

log = logging.getLogger(__name__)

# `adb devices -l` line: "R58M1234ABC  device product:x model:SM_G930F ..."
_DEVICE_LINE = re.compile(r"^(?P<serial>\S+)\s+(?P<state>device|recovery|sideload|unauthorized|offline|bootloader|rescue|host|no permissions.*)\b(?P<rest>.*)$")
_KV = re.compile(r"(\w+):(\S+)")


@dataclass
class AdbDevice:
    serial: str
    state: str
    props: dict[str, str] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return self.props.get("model", "").replace("_", " ")

    @property
    def usable(self) -> bool:
        return self.state in ("device", "recovery", "sideload", "rescue")


class Adb:
    def __init__(self, path: str, runner: Runner | None = None) -> None:
        if not path:
            raise ToolError("adb was not found. Install platform-tools first.")
        self.path = path
        self.runner = runner or Runner()

    # --- plumbing -----------------------------------------------------
    def _base(self, serial: str | None) -> list[str]:
        args = [self.path]
        if serial:
            args += ["-s", serial]
        return args

    def raw(self, args: Sequence[str], serial: str | None = None, timeout: float | None = 60.0) -> Result:
        return self.runner.run(self._base(serial) + list(args), timeout=timeout)

    def shell(self, command: str, serial: str | None = None, timeout: float | None = 60.0) -> Result:
        return self.raw(["shell", command], serial, timeout)

    def stream(
        self,
        args: Sequence[str],
        on_line: Callable[[str], None],
        serial: str | None = None,
        timeout: float | None = None,
        cancel: Cancel | None = None,
    ) -> Result:
        return self.runner.stream(self._base(serial) + list(args), on_line, timeout=timeout, cancel=cancel)

    # --- discovery ----------------------------------------------------
    def start_server(self) -> Result:
        return self.raw(["start-server"], timeout=30)

    def kill_server(self) -> Result:
        return self.raw(["kill-server"], timeout=30)

    def devices(self) -> list[AdbDevice]:
        res = self.raw(["devices", "-l"], timeout=30)
        out: list[AdbDevice] = []
        for line in res.lines():
            if line.lower().startswith("list of devices"):
                continue
            if line.startswith("*"):  # "* daemon started successfully *"
                continue
            m = _DEVICE_LINE.match(line)
            if not m:
                continue
            props = dict(_KV.findall(m.group("rest") or ""))
            out.append(AdbDevice(m.group("serial"), m.group("state").split()[0], props))
        return out

    def wait_for(self, state: str = "device", serial: str | None = None, timeout: float = 120) -> bool:
        res = self.raw([f"wait-for-{state}"], serial, timeout=timeout)
        return res.ok

    # --- device facts -------------------------------------------------
    def getprop(self, name: str, serial: str | None = None) -> str:
        res = self.shell(f"getprop {shlex.quote(name)}", serial, timeout=20)
        return res.stdout.strip() if res.ok else ""

    def all_props(self, serial: str | None = None) -> dict[str, str]:
        res = self.shell("getprop", serial, timeout=30)
        props: dict[str, str] = {}
        for line in res.stdout.splitlines():
            m = re.match(r"\[(.+?)\]: \[(.*)\]", line.strip())
            if m:
                props[m.group(1)] = m.group(2)
        return props

    def sdk_int(self, serial: str | None = None) -> int:
        try:
            return int(self.getprop("ro.build.version.sdk", serial) or 0)
        except ValueError:
            return 0

    def storage_root(self, serial: str | None = None) -> str:
        """Where the user's files live. /sdcard is a symlink on every modern build."""
        for candidate in ("/sdcard", "/storage/emulated/0", "/mnt/sdcard"):
            if self.exists(candidate, serial):
                return candidate
        return "/sdcard"

    def exists(self, path: str, serial: str | None = None) -> bool:
        res = self.shell(f"[ -e {shlex.quote(path)} ] && echo yes || echo no", serial, timeout=20)
        return res.stdout.strip().endswith("yes")

    def battery_level(self, serial: str | None = None) -> int | None:
        res = self.shell("dumpsys battery | grep level", serial, timeout=20)
        m = re.search(r"level:\s*(\d+)", res.output)
        return int(m.group(1)) if m else None

    # --- files --------------------------------------------------------
    def list_files(self, root: str, serial: str | None = None, timeout: float = 300) -> tuple[list["RemoteFile"], str]:
        """Inventory of every regular file under ``root``.

        Returns (files, method). Toybox on Android has no ``find -printf``, so
        the primary strategy is ``find ... -exec stat``; two fallbacks follow
        for older/stripped shells.
        """
        q = shlex.quote(root)

        cmd = f"find {q} -type f -exec stat -c '%s|%Y|%n' {{}} + 2>/dev/null"
        res = self.shell(cmd, serial, timeout=timeout)
        files = parse_stat_inventory(res.stdout)
        if files:
            return files, "stat"

        # Fallback 1: find + `ls -l` per batch is far too slow; use `du`-free
        # listing that at least gives us paths, sizes filled in later.
        res = self.shell(f"find {q} -type f 2>/dev/null", serial, timeout=timeout)
        paths = [ln.strip() for ln in res.stdout.splitlines() if ln.strip().startswith("/")]
        if paths:
            return [RemoteFile(p, -1, 0) for p in paths], "find"

        # Fallback 2: recursive ls (very old shells).
        res = self.shell(f"ls -laR {q} 2>/dev/null", serial, timeout=timeout)
        return parse_ls_recursive(res.stdout), "ls"

    def pull(
        self,
        remote: str,
        local: str,
        serial: str | None = None,
        on_line: Callable[[str], None] | None = None,
        cancel: Cancel | None = None,
        timeout: float | None = None,
    ) -> Result:
        args = ["pull", "-a", remote, local]
        if on_line is None:
            return self.raw(args, serial, timeout=timeout)
        return self.stream(args, on_line, serial, timeout=timeout, cancel=cancel)

    def push(
        self,
        local: str,
        remote: str,
        serial: str | None = None,
        on_line: Callable[[str], None] | None = None,
        cancel: Cancel | None = None,
        timeout: float | None = None,
    ) -> Result:
        args = ["push", local, remote]
        if on_line is None:
            return self.raw(args, serial, timeout=timeout)
        return self.stream(args, on_line, serial, timeout=timeout, cancel=cancel)

    # --- packages -----------------------------------------------------
    def third_party_packages(self, serial: str | None = None) -> list[str]:
        res = self.shell("pm list packages -3", serial, timeout=60)
        return sorted(ln.split(":", 1)[1] for ln in res.lines() if ln.startswith("package:"))

    def package_paths(self, package: str, serial: str | None = None) -> list[str]:
        res = self.shell(f"pm path {shlex.quote(package)}", serial, timeout=30)
        return [ln.split(":", 1)[1] for ln in res.lines() if ln.startswith("package:")]

    def install_multiple(self, apks: Sequence[str], serial: str | None = None, timeout: float = 600) -> Result:
        if len(apks) == 1:
            return self.raw(["install", "-r", apks[0]], serial, timeout=timeout)
        return self.raw(["install-multiple", "-r", *apks], serial, timeout=timeout)

    # --- content providers -------------------------------------------
    def content_query(self, uri: str, serial: str | None = None, timeout: float = 180) -> Result:
        return self.shell(f"content query --uri {shlex.quote(uri)}", serial, timeout=timeout)

    # --- recovery / sideload -----------------------------------------
    def reboot(self, target: str = "", serial: str | None = None) -> Result:
        args = ["reboot"] + ([target] if target else [])
        return self.raw(args, serial, timeout=30)

    def sideload(
        self,
        zip_path: str,
        serial: str | None = None,
        on_line: Callable[[str], None] | None = None,
        cancel: Cancel | None = None,
    ) -> Result:
        args = ["sideload", zip_path]
        if on_line is None:
            return self.raw(args, serial, timeout=3600)
        return self.stream(args, on_line, serial, timeout=3600, cancel=cancel)


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    mtime: int

    def relative_to(self, root: str) -> str:
        rel = posixpath.relpath(self.path, root)
        return rel


def parse_stat_inventory(text: str) -> list[RemoteFile]:
    files: list[RemoteFile] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        size_s, _, rest = line.partition("|")
        mtime_s, _, path = rest.partition("|")
        if not path.startswith("/"):
            continue
        try:
            files.append(RemoteFile(path, int(size_s), int(mtime_s)))
        except ValueError:
            continue
    return files


def parse_ls_recursive(text: str) -> list[RemoteFile]:
    """Parse `ls -laR` output into an inventory (last-resort fallback)."""
    files: list[RemoteFile] = []
    current = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.endswith(":") and line.startswith("/"):
            current = line[:-1]
            continue
        if line.startswith(("total ", "d", "l")):
            continue
        parts = line.split(maxsplit=7)
        if len(parts) < 8 or not parts[0].startswith("-"):
            continue
        try:
            size = int(parts[4])
        except ValueError:
            continue
        name = parts[7]
        if current:
            files.append(RemoteFile(posixpath.join(current, name), size, 0))
    return files


def filter_inventory(
    files: Iterable[RemoteFile],
    root: str,
    exclude_globs: Sequence[str] = (),
) -> list[RemoteFile]:
    import fnmatch

    out = []
    for f in files:
        rel = f.relative_to(root)
        if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(f.path, g) for g in exclude_globs):
            continue
        out.append(f)
    return out
