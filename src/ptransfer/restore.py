"""Putting a bundle onto the new phone.

Three different kinds of restore, because Android treats them differently:

* **Direct** - files and APKs. adb pushes them, done.
* **Assisted** - contacts. The vCard is placed on the phone and the Contacts
  app imports it; one tap from the user.
* **Companion-only** - SMS/MMS and call logs. Android only lets the current
  default SMS app write to the message store, so a PC cannot do it at all.
  The companion app can, once you make it the default SMS app for a minute.

Anything claiming to restore messages over USB without an app on the phone is
either rooting it or lying.
"""

from __future__ import annotations

import json
import logging
import posixpath
import time
from dataclasses import dataclass, field
from pathlib import Path

from .adb import Adb
from .backup import COMPANION_PACKAGE
from .manifest import Bundle
from .proc import Cancel, Cancelled
from .progress import Reporter, human_bytes

log = logging.getLogger(__name__)

STAGING_DIR = "/sdcard/Download/ptransfer"


@dataclass
class RestoreOptions:
    sections: tuple[str, ...] = ("media", "apps", "contacts", "sms", "calls")
    media_target: str = "/sdcard"
    install_apps: bool = True
    overwrite: bool = False


@dataclass
class RestoreStep:
    name: str
    status: str = "pending"  # ok | partial | failed | skipped | needs-user
    message: str = ""
    files: int = 0
    bytes: int = 0
    todo: list[str] = field(default_factory=list)


@dataclass
class RestoreReport:
    steps: dict[str, RestoreStep] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def needs_user(self) -> list[RestoreStep]:
        return [s for s in self.steps.values() if s.status == "needs-user"]

    def summary_lines(self) -> list[str]:
        out = []
        for name, s in self.steps.items():
            extra = f" - {s.message}" if s.message else ""
            out.append(f"  {name:<10} {s.status:<10}{extra}")
            for t in s.todo:
                out.append(f"      * {t}")
        return out


class RestoreEngine:
    def __init__(
        self,
        adb: Adb,
        serial: str | None,
        reporter: Reporter | None = None,
        cancel: Cancel | None = None,
    ) -> None:
        self.adb = adb
        self.serial = serial
        self.report = reporter or Reporter()
        self.cancel = cancel or Cancel()

    def run(self, bundle_path: str | Path, options: RestoreOptions | None = None) -> RestoreReport:
        options = options or RestoreOptions()
        started = time.time()
        bundle = Bundle.open(bundle_path)
        result = RestoreReport()

        src = bundle.manifest.source
        self.report.status(
            f"Restoring from {src.manufacturer} {src.model} (Android {src.android_release})"
        )

        handlers = {
            "media": self._restore_media,
            "apps": self._restore_apps,
            "contacts": self._restore_contacts,
            "sms": self._restore_messages,
            "calls": self._restore_calls,
        }
        for name in ("media", "apps", "contacts", "sms", "calls"):
            if name not in options.sections:
                result.steps[name] = RestoreStep(name, "skipped")
                continue
            self.report.start_section(name)
            try:
                self.cancel.raise_if_cancelled()
                step = handlers[name](bundle, options)
            except Cancelled:
                result.steps[name] = RestoreStep(name, "failed", "cancelled by user")
                break
            except Exception as exc:
                log.exception("restore step %s failed", name)
                step = RestoreStep(name, "failed", f"{type(exc).__name__}: {exc}")
            result.steps[name] = step

        result.seconds = time.time() - started
        self.report.done(f"Restore finished in {result.seconds:.0f}s")
        return result

    # --- steps --------------------------------------------------------
    def _restore_media(self, bundle: Bundle, options: RestoreOptions) -> RestoreStep:
        if not bundle.media_dir.exists():
            return RestoreStep("media", "skipped", "no media in this bundle")

        tops = sorted(p for p in bundle.media_dir.iterdir())
        if not tops:
            return RestoreStep("media", "skipped", "no media in this bundle")

        total = sum(p.stat().st_size for p in bundle.media_dir.rglob("*") if p.is_file())
        self.report.status(f"Copying {human_bytes(total)} to the phone")
        pushed_bytes = 0
        failures: list[str] = []

        for top in tops:
            self.cancel.raise_if_cancelled()
            size = (
                top.stat().st_size
                if top.is_file()
                else sum(p.stat().st_size for p in top.rglob("*") if p.is_file())
            )
            self.report.status(f"Copying {top.name} ({human_bytes(size)})")
            remote = posixpath.join(options.media_target, top.name)

            def on_line(line: str) -> None:
                if "%]" in line:
                    self.report.progress(
                        min(1.0, pushed_bytes / total) if total else -1,
                        line.strip()[-70:],
                        done_bytes=pushed_bytes,
                        total_bytes=total,
                    )

            res = self.adb.push(str(top), remote, self.serial, on_line=on_line, cancel=self.cancel)
            if not res.ok:
                failures.append(f"{top.name}: {res.output.strip()[:120]}")
            pushed_bytes += size

        # On Android 10+ files pushed through adb are indexed automatically;
        # on older builds the gallery needs a nudge.
        self.adb.shell(
            "am broadcast -a android.intent.action.MEDIA_MOUNTED -d file:///sdcard "
            "--receiver-include-background",
            self.serial,
            timeout=30,
        )

        if failures:
            return RestoreStep(
                "media",
                "partial",
                f"{len(failures)} folders had problems",
                bytes=pushed_bytes,
                todo=failures[:10],
            )
        return RestoreStep("media", "ok", bytes=pushed_bytes)

    def _restore_apps(self, bundle: Bundle, options: RestoreOptions) -> RestoreStep:
        index_path = bundle.apps_dir / "apps.json"
        if not index_path.exists():
            return RestoreStep("apps", "skipped", "no apps in this bundle")
        if not options.install_apps:
            return RestoreStep("apps", "skipped", "app install turned off")

        index = json.loads(index_path.read_text(encoding="utf-8")).get("packages", {})
        installed, failed = 0, []
        for i, (pkg, files) in enumerate(sorted(index.items()), 1):
            self.cancel.raise_if_cancelled()
            self.report.progress(i / max(1, len(index)), f"{pkg} ({i}/{len(index)})", done_items=i, total_items=len(index))
            apks = [str(bundle.apps_dir / pkg / f) for f in files]
            apks = [a for a in apks if Path(a).exists()]
            if not apks:
                failed.append(f"{pkg}: APK missing from bundle")
                continue
            res = self.adb.install_multiple(apks, self.serial)
            if res.ok and "Success" in res.output:
                installed += 1
            else:
                failed.append(f"{pkg}: {_short_install_error(res.output)}")

        status = "ok" if not failed else ("partial" if installed else "failed")
        msg = f"{installed} installed"
        if failed:
            msg += f", {len(failed)} refused by the new phone"
        return RestoreStep("apps", status, msg, files=installed, todo=failed[:15])

    def _restore_contacts(self, bundle: Bundle, options: RestoreOptions) -> RestoreStep:
        vcf = bundle.data_dir / "contacts.vcf"
        if not vcf.exists():
            return RestoreStep("contacts", "skipped", "no contacts in this bundle")

        self.adb.shell(f"mkdir -p {STAGING_DIR}", self.serial)
        remote = f"{STAGING_DIR}/contacts.vcf"
        res = self.adb.push(str(vcf), remote, self.serial)
        if not res.ok:
            return RestoreStep("contacts", "failed", res.output.strip()[:200])

        if self._companion_present():
            r = self.adb.shell(
                "am start -n {}/.ImportActivity --es file {} --es kind contacts".format(COMPANION_PACKAGE, remote),
                self.serial,
                timeout=30,
            )
            if r.ok and "Error" not in r.output:
                return RestoreStep("contacts", "needs-user", "Confirm the import in the companion app on the phone.")

        return RestoreStep(
            "contacts",
            "needs-user",
            "vCard copied to the phone - import it from the Contacts app.",
            files=1,
            bytes=vcf.stat().st_size,
            todo=[
                "On the phone: Contacts > Settings (or the three-dot menu) > Import",
                f"Choose 'from .vcf file' and pick Download/ptransfer/contacts.vcf",
            ],
        )

    def _restore_messages(self, bundle: Bundle, options: RestoreOptions) -> RestoreStep:
        sms = bundle.data_dir / "sms.json"
        if not sms.exists():
            return RestoreStep("sms", "skipped", "no messages in this bundle")

        self.adb.shell(f"mkdir -p {STAGING_DIR}", self.serial)
        remote = f"{STAGING_DIR}/sms.json"
        self.adb.push(str(sms), remote, self.serial)

        if self._companion_present():
            self.adb.shell(
                f"am start -n {COMPANION_PACKAGE}/.ImportActivity --es file {remote} --es kind sms",
                self.serial,
                timeout=30,
            )
            return RestoreStep(
                "sms",
                "needs-user",
                "Companion app opened on the phone.",
                files=1,
                todo=[
                    "Make the companion app your default SMS app when it asks (Android requires this)",
                    "Tap Import, then switch your normal messaging app back",
                ],
            )

        return RestoreStep(
            "sms",
            "needs-user",
            "Messages copied to the phone as sms.json, but Android will not let a PC write them "
            "into the messaging app.",
            files=1,
            todo=[
                "Install the companion app from this bundle's tools folder, then re-run restore",
                "The JSON stays readable on its own if you only want a record of the messages",
            ],
        )

    def _restore_calls(self, bundle: Bundle, options: RestoreOptions) -> RestoreStep:
        calls = bundle.data_dir / "calls.json"
        if not calls.exists():
            return RestoreStep("calls", "skipped", "no call log in this bundle")
        self.adb.shell(f"mkdir -p {STAGING_DIR}", self.serial)
        remote = f"{STAGING_DIR}/calls.json"
        self.adb.push(str(calls), remote, self.serial)
        if self._companion_present():
            self.adb.shell(
                f"am start -n {COMPANION_PACKAGE}/.ImportActivity --es file {remote} --es kind calls",
                self.serial,
                timeout=30,
            )
            return RestoreStep("calls", "needs-user", "Confirm the call-log import in the companion app.")
        return RestoreStep(
            "calls",
            "needs-user",
            "Call log copied to the phone; the companion app is needed to write it back.",
            files=1,
        )

    def _companion_present(self) -> bool:
        res = self.adb.shell(f"pm list packages {COMPANION_PACKAGE}", self.serial, timeout=20)
        return COMPANION_PACKAGE in res.output


def _short_install_error(output: str) -> str:
    for line in output.splitlines():
        if "Failure" in line or "INSTALL_FAILED" in line:
            return line.strip()[:160]
    return output.strip()[:160] or "install failed"
