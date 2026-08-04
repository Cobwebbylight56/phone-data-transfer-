"""A fake phone: enough of adb's behaviour to drive the engines end to end.

It simulates a storage tree, packages, and content providers, and it actually
writes files when ``adb pull`` is called - so the backup engine's verification
pass is exercised for real rather than mocked away.
"""

from __future__ import annotations

import posixpath
import shlex
from pathlib import Path

from ptransfer.proc import Result, Runner


class FakePhone(Runner):
    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        packages: dict[str, list[str]] | None = None,
        props: dict[str, str] | None = None,
        contacts: str = "",
        sms: str = "",
        calls: str = "",
        vcards: dict[str, str] | None = None,
        provider_denied: bool = False,
        unpullable: set[str] | None = None,
    ) -> None:
        self.files = files or {}
        self.packages = packages or {}
        self.props = props or {
            "ro.product.brand": "Sony",
            "ro.product.manufacturer": "Sony",
            "ro.product.model": "XQ-CT54",
            "ro.product.device": "pdx223",
            "ro.build.version.release": "13",
            "ro.build.version.sdk": "33",
            "ro.build.display.id": "68.1.A.2.55",
        }
        self.contacts = contacts
        self.sms = sms
        self.calls = calls
        self.vcards = vcards or {}
        self.provider_denied = provider_denied
        self.unpullable = unpullable or set()
        self.calls_made: list[list[str]] = []

    # --- Runner API ---------------------------------------------------
    def run(self, args, timeout=60.0, input_text=None, cwd=None) -> Result:
        self.calls_made.append(list(args))
        args = list(args)
        # strip "adb" and any "-s SERIAL"
        rest = args[1:]
        if rest[:1] == ["-s"]:
            rest = rest[2:]
        if not rest:
            return Result(args, 1, stderr="no command")

        verb, params = rest[0], rest[1:]
        handler = getattr(self, f"_do_{verb.replace('-', '_')}", None)
        if handler is None:
            return Result(args, 0, "")
        return handler(args, params)

    def stream(self, args, on_line, timeout=None, cancel=None) -> Result:
        res = self.run(args)
        for line in res.stdout.splitlines():
            on_line(line)
        return res

    # --- commands -----------------------------------------------------
    def _do_devices(self, args, params) -> Result:
        return Result(args, 0, "List of devices attached\nFAKE123  device product:x model:XQ_CT54\n")

    def _do_shell(self, args, params) -> Result:
        command = " ".join(params)
        return self._shell(args, command)

    def _do_exec_out(self, args, params) -> Result:
        if params[:2] == ["content", "read"]:
            uri = params[-1]
            key = uri.rsplit("/", 1)[-1]
            return Result(args, 0, self.vcards.get(key, ""))
        return self._shell(args, " ".join(params))

    def _do_pull(self, args, params) -> Result:
        params = [p for p in params if p != "-a"]
        remote, local = params[0], params[1]
        matches = {p: d for p, d in self.files.items() if p == remote or p.startswith(remote.rstrip("/") + "/")}
        matches = {p: d for p, d in matches.items() if p not in self.unpullable}
        if not matches:
            return Result(args, 1, stderr=f"adb: error: remote object '{remote}' does not exist")

        lines = []
        for path, data in sorted(matches.items()):
            if path == remote:
                dest = Path(local)
            else:
                rel = posixpath.relpath(path, remote)
                dest = Path(local) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            lines.append(f"[ 50%] {path}")
        lines.append(f"{len(matches)} files pulled")
        return Result(args, 0, "\n".join(lines))

    def _do_push(self, args, params) -> Result:
        local, remote = params[0], params[1]
        p = Path(local)
        if not p.exists():
            return Result(args, 1, stderr="no such local file")
        if p.is_file():
            self.files[remote] = p.read_bytes()
        else:
            for f in p.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(p).as_posix()
                    self.files[posixpath.join(remote, rel)] = f.read_bytes()
        return Result(args, 0, "[100%] pushed\n1 file pushed")

    def _do_install(self, args, params) -> Result:
        return Result(args, 0, "Success")

    def _do_install_multiple(self, args, params) -> Result:
        return Result(args, 0, "Success")

    def _do_reboot(self, args, params) -> Result:
        return Result(args, 0, "")

    def _do_start_server(self, args, params) -> Result:
        return Result(args, 0, "")

    def _do_kill_server(self, args, params) -> Result:
        return Result(args, 0, "")

    # --- the shell ----------------------------------------------------
    def _shell(self, args, command: str) -> Result:
        if command.startswith("getprop") and command.strip() == "getprop":
            return Result(args, 0, "\n".join(f"[{k}]: [{v}]" for k, v in self.props.items()))
        if command.startswith("getprop "):
            name = shlex.split(command)[1]
            return Result(args, 0, self.props.get(name, ""))
        if command.startswith("[ -e "):
            path = shlex.split(command)[2]
            hit = any(p == path or p.startswith(path.rstrip("/") + "/") for p in self.files)
            return Result(args, 0, "yes" if hit or path == "/sdcard" else "no")
        if "-exec stat" in command:
            root = shlex.split(command)[1]
            rows = [
                f"{len(data)}|1700000000|{path}"
                for path, data in sorted(self.files.items())
                if path.startswith(root.rstrip("/") + "/")
            ]
            return Result(args, 0, "\n".join(rows))
        if command.startswith("pm list packages -3"):
            return Result(args, 0, "\n".join(f"package:{p}" for p in sorted(self.packages)))
        if command.startswith("pm list packages"):
            wanted = command.split()[-1]
            found = [p for p in self.packages if p == wanted]
            return Result(args, 0, "\n".join(f"package:{p}" for p in found))
        if command.startswith("pm path"):
            pkg = shlex.split(command)[2]
            return Result(args, 0, "\n".join(f"package:{p}" for p in self.packages.get(pkg, [])))
        if command.startswith("content query"):
            return self._content_query(args, command)
        if command.startswith("settings list"):
            ns = command.split()[-1]
            return Result(args, 0, f"{ns}_key=1\nanother_{ns}=hello")
        if command.startswith("dumpsys battery"):
            return Result(args, 0, "  level: 78")
        if command.startswith("dumpsys package"):
            return Result(args, 0, "    versionName=1.2.3")
        if command.startswith("mkdir"):
            return Result(args, 0, "")
        if command.startswith("echo ok"):
            return Result(args, 0, "ok")
        if command.startswith("df "):
            return Result(args, 0, "/dev/fuse  118G  61G  57G  52% /storage/emulated")
        return Result(args, 0, "")

    def _content_query(self, args, command: str) -> Result:
        if self.provider_denied:
            return Result(
                args,
                0,
                "Error while accessing provider:sms\njava.lang.SecurityException: Permission Denial",
            )
        uri = command.split("--uri", 1)[1].strip().strip("'\"")
        if "contacts" in uri:
            return Result(args, 0, self.contacts)
        if "sms" in uri:
            return Result(args, 0, self.sms)
        if "call_log" in uri:
            return Result(args, 0, self.calls)
        return Result(args, 0, "")
