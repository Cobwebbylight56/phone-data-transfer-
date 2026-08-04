"""Pulling everything off the source phone into a bundle.

What is and is not possible without rooting the phone, stated plainly because
it decides the whole design:

* Photos, videos, music, documents, downloads - everything in internal
  storage - copy perfectly. This is the bulk of what people mean by "my data".
* Contacts, SMS/MMS text, and call logs can be read through the content
  providers when the build allows the shell user to (most do), or through the
  companion app when it does not.
* Installed apps: the APKs copy. Their *private data* does not - Android
  deliberately isolates it, and no unrooted PC tool can reach it. The honest
  answer is that app data comes back via each app's own account sign-in or
  Google's backup, and this app says so rather than pretending otherwise.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .adb import Adb, RemoteFile, filter_inventory
from .content import (
    CALLS_URI,
    CONTACTS_URI,
    SMS_URI,
    VCARD_URI,
    ProviderError,
    check_provider_output,
    normalise_calls,
    normalise_sms,
    parse_rows,
    rows_to_vcard,
)
from .devices import Device
from .manifest import Bundle, SectionResult, SourceDevice
from .proc import Cancel, Cancelled
from .progress import Reporter, human_bytes

log = logging.getLogger(__name__)

COMPANION_PACKAGE = "com.ptransfer.companion"

DEFAULT_MEDIA_EXCLUDES = (
    "Android/data/*",
    "Android/obb/*",
    "*/.thumbnails/*",
    ".thumbnails/*",
    "*/cache/*",
    "*/.trashed-*",
    "LOST.DIR/*",
)

SECTIONS = ("deviceinfo", "media", "contacts", "sms", "calls", "apps", "settings")

_PULL_PROGRESS = re.compile(r"\[\s*(\d+)%\]\s+(\S.*)$")


@dataclass
class BackupOptions:
    sections: tuple[str, ...] = SECTIONS
    media_excludes: tuple[str, ...] = DEFAULT_MEDIA_EXCLUDES
    include_apk_splits: bool = True
    verify: bool = True
    skip_existing: bool = True
    max_media_bytes: int = 0  # 0 = no limit


@dataclass
class BackupReport:
    bundle_path: str = ""
    sections: dict[str, SectionResult] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return all(s.status in ("ok", "skipped", "partial") for s in self.sections.values())

    def summary_lines(self) -> list[str]:
        lines = []
        for name, s in self.sections.items():
            detail = []
            if s.items:
                detail.append(f"{s.items} items")
            if s.files:
                detail.append(f"{s.files} files")
            if s.bytes:
                detail.append(human_bytes(s.bytes))
            tail = ", ".join(detail)
            msg = f" - {s.message}" if s.message else ""
            lines.append(f"  {name:<10} {s.status:<8} {tail}{msg}")
        return lines


class BackupEngine:
    def __init__(
        self,
        adb: Adb,
        device: Device,
        reporter: Reporter | None = None,
        cancel: Cancel | None = None,
    ) -> None:
        self.adb = adb
        self.device = device
        self.serial = device.serial or None
        self.report = reporter or Reporter()
        self.cancel = cancel or Cancel()

    # --- entry point --------------------------------------------------
    def run(self, dest: str | Path, options: BackupOptions | None = None) -> BackupReport:
        options = options or BackupOptions()
        started = time.time()
        bundle = Bundle.create(dest, self._source(), app_version=__version__)
        result = BackupReport(bundle_path=str(bundle.root))

        handlers = {
            "deviceinfo": self._do_deviceinfo,
            "media": self._do_media,
            "contacts": self._do_contacts,
            "sms": self._do_sms,
            "calls": self._do_calls,
            "apps": self._do_apps,
            "settings": self._do_settings,
        }

        for name in SECTIONS:
            if name not in options.sections:
                bundle.record(SectionResult(name, "skipped", message="not selected"))
                continue
            self.report.start_section(name)
            try:
                self.cancel.raise_if_cancelled()
                section = handlers[name](bundle, options)
            except Cancelled:
                bundle.record(SectionResult(name, "failed", message="cancelled by user"))
                bundle.warn("Backup cancelled - the bundle is incomplete.")
                break
            except ProviderError as exc:
                section = SectionResult(name, "failed", message=str(exc))
            except Exception as exc:  # keep going: one bad section must not lose the rest
                log.exception("section %s failed", name)
                section = SectionResult(name, "failed", message=f"{type(exc).__name__}: {exc}")
            bundle.record(section)
            result.sections[name] = section
            if section.status == "failed":
                self.report.warn(f"{name}: {section.message}")

        if options.verify:
            self.report.start_section("verify", "Hashing the bundle")
            count = bundle.write_checksums(
                progress=lambda f, rel: self.report.progress(f, f"hashing {rel}")
            )
            self.report.status(f"Wrote checksums for {count} files")

        bundle.finish()
        result.warnings = list(bundle.manifest.warnings)
        result.seconds = time.time() - started
        self.report.done(f"Backup finished in {result.seconds:.0f}s -> {bundle.root}")
        return result

    # --- sections -----------------------------------------------------
    def _source(self) -> SourceDevice:
        d = self.device
        return SourceDevice(
            serial=d.serial,
            brand=d.brand,
            manufacturer=d.manufacturer,
            model=d.model,
            device=d.device_name,
            android_release=d.android_release,
            sdk=d.sdk,
            build_id=d.build_id,
        )

    def _do_deviceinfo(self, bundle: Bundle, options: BackupOptions) -> SectionResult:
        props = self.device.props or self.adb.all_props(self.serial)
        path = bundle.data_dir / "device.json"
        payload = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "serial": self.device.serial,
            "props": props,
            "storage_root": self.adb.storage_root(self.serial),
            "battery": self.adb.battery_level(self.serial),
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return SectionResult("deviceinfo", "ok", files=1, bytes=path.stat().st_size, items=len(props))

    def _do_media(self, bundle: Bundle, options: BackupOptions) -> SectionResult:
        root = self.adb.storage_root(self.serial)
        self.report.status(f"Listing files under {root} (this takes a minute on a full phone)")
        inventory, method = self.adb.list_files(root, self.serial)
        inventory = filter_inventory(inventory, root, options.media_excludes)
        if not inventory:
            return SectionResult("media", "failed", message=f"Could not list {root} (method={method}).")

        total_bytes = sum(max(0, f.size) for f in inventory)
        self.report.status(
            f"{len(inventory)} files, {human_bytes(total_bytes)} to copy (listed via {method})"
        )
        if options.max_media_bytes and total_bytes > options.max_media_bytes:
            bundle.warn(
                f"Media is {human_bytes(total_bytes)}, above the {human_bytes(options.max_media_bytes)} limit set."
            )

        local_root = bundle.media_dir
        local_root.mkdir(parents=True, exist_ok=True)

        by_top: dict[str, list[RemoteFile]] = {}
        for f in inventory:
            rel = f.relative_to(root)
            top = rel.split("/", 1)[0]
            by_top.setdefault(top, []).append(f)

        pulled_bytes = 0
        pulled_files = 0
        tracker = _PullTracker(root, {f.path: max(0, f.size) for f in inventory})

        for top, files in sorted(by_top.items()):
            self.cancel.raise_if_cancelled()
            target = local_root / top
            if options.skip_existing and _already_complete(files, root, local_root):
                self.report.status(f"{top}: already copied, skipping")
                pulled_bytes += sum(max(0, f.size) for f in files)
                pulled_files += len(files)
                tracker.done_bytes = pulled_bytes
                continue

            self.report.status(f"Copying {top} ({len(files)} files, {human_bytes(sum(max(0, f.size) for f in files))})")
            remote = posixpath.join(root, top)
            target.parent.mkdir(parents=True, exist_ok=True)

            base_done = pulled_bytes
            tracker.reset(base_done)

            def on_line(line: str, _t=tracker) -> None:
                m = _PULL_PROGRESS.search(line)
                if m:
                    _t.saw(m.group(2).strip())
                    self.report.progress(
                        min(1.0, _t.done_bytes / total_bytes) if total_bytes else -1,
                        _t.current_label(),
                        done_bytes=_t.done_bytes,
                        total_bytes=total_bytes,
                    )

            res = self.adb.pull(remote, str(target), self.serial, on_line=on_line, cancel=self.cancel)
            if not res.ok and "0 files pulled" in res.output:
                bundle.warn(f"Nothing copied from {top}: {res.output.strip()[:200]}")

            tracker.finish()
            pulled_bytes = tracker.done_bytes
            pulled_files += len(files)
            # Always emit at a folder boundary: adb can go quiet for a long
            # time on big files, and the throttle would otherwise leave the bar
            # showing a stale figure.
            self.report.progress(
                min(1.0, pulled_bytes / total_bytes) if total_bytes else -1,
                f"{top} done",
                done_bytes=pulled_bytes,
                total_bytes=total_bytes,
                throttle=0,
            )

        # Verify against the inventory and repair individual misses.
        missing = _missing_files(inventory, root, local_root)
        if missing:
            self.report.status(f"Retrying {len(missing)} files that did not come across")
            still_missing = []
            for f in missing[:500]:
                self.cancel.raise_if_cancelled()
                rel = f.relative_to(root)
                dest = local_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                res = self.adb.pull(f.path, str(dest), self.serial, timeout=300)
                if not dest.exists():
                    still_missing.append((f.path, res.output.strip()[:120]))
            if still_missing:
                report_path = bundle.logs_dir / "media-not-copied.txt"
                report_path.write_text(
                    "\n".join(f"{p}\t{why}" for p, why in still_missing), encoding="utf-8"
                )
                bundle.warn(
                    f"{len(still_missing)} files could not be copied (listed in "
                    f"logs/{report_path.name}); usually app-private folders Android does not share."
                )
                actual_files = len(inventory) - len(still_missing)
                actual_bytes = _local_bytes(local_root)
                return SectionResult(
                    "media",
                    "partial",
                    files=actual_files,
                    bytes=actual_bytes,
                    message=f"{len(still_missing)} files unavailable",
                )

        return SectionResult("media", "ok", files=len(inventory), bytes=_local_bytes(local_root))

    def _do_contacts(self, bundle: Bundle, options: BackupOptions) -> SectionResult:
        res = self.adb.content_query(CONTACTS_URI, self.serial)
        check_provider_output(res.output)
        rows = parse_rows(res.stdout)
        if not rows:
            return SectionResult("contacts", "ok", items=0, message="no contacts on this phone")

        vcards: list[str] = []
        failures = 0
        for i, row in enumerate(rows, 1):
            self.cancel.raise_if_cancelled()
            lookup = row.get("lookup")
            if not lookup:
                failures += 1
                continue
            uri = VCARD_URI + lookup
            out = self.adb.raw(["exec-out", "content", "read", "--uri", uri], self.serial, timeout=60)
            text = out.stdout.strip()
            if "BEGIN:VCARD" in text:
                vcards.append(text.replace("\r\n", "\n").strip())
            else:
                failures += 1
            self.report.progress(i / len(rows), f"contact {i}/{len(rows)}", done_items=i, total_items=len(rows))

        path = bundle.data_dir / "contacts.vcf"
        if vcards:
            path.write_text("\n".join(vcards) + "\n", encoding="utf-8")
            status = "partial" if failures else "ok"
            msg = f"{failures} contacts could not be exported individually" if failures else ""
            return SectionResult("contacts", status, files=1, bytes=path.stat().st_size, items=len(vcards), message=msg)

        # Fallback: build a basic vCard from the raw rows.
        text = rows_to_vcard(rows)
        path.write_text(text, encoding="utf-8")
        return SectionResult(
            "contacts",
            "partial",
            files=1,
            bytes=path.stat().st_size,
            items=len(rows),
            message="names and numbers only - this phone would not hand over full vCards",
        )

    def _do_sms(self, bundle: Bundle, options: BackupOptions) -> SectionResult:
        res = self.adb.content_query(SMS_URI, self.serial, timeout=600)
        check_provider_output(res.output)
        messages = normalise_sms(parse_rows(res.stdout))
        path = bundle.data_dir / "sms.json"
        path.write_text(json.dumps(messages, indent=1, ensure_ascii=False), encoding="utf-8")
        note = ""
        if not messages:
            note = "no messages readable over USB on this build"
        return SectionResult("sms", "ok", files=1, bytes=path.stat().st_size, items=len(messages), message=note)

    def _do_calls(self, bundle: Bundle, options: BackupOptions) -> SectionResult:
        res = self.adb.content_query(CALLS_URI, self.serial, timeout=300)
        check_provider_output(res.output)
        calls = normalise_calls(parse_rows(res.stdout))
        path = bundle.data_dir / "calls.json"
        path.write_text(json.dumps(calls, indent=1, ensure_ascii=False), encoding="utf-8")
        return SectionResult("calls", "ok", files=1, bytes=path.stat().st_size, items=len(calls))

    def _do_apps(self, bundle: Bundle, options: BackupOptions) -> SectionResult:
        packages = self.adb.third_party_packages(self.serial)
        if not packages:
            return SectionResult("apps", "ok", items=0, message="no user-installed apps found")

        bundle.warn(
            "App APKs are saved, but Android does not let a PC read apps' private data without "
            "rooting the phone. Sign in to each app on the new phone to get its data back."
        )
        total_files = 0
        total_bytes = 0
        failed: list[str] = []
        index: dict[str, list[str]] = {}

        for i, pkg in enumerate(packages, 1):
            self.cancel.raise_if_cancelled()
            self.report.progress(i / len(packages), f"{pkg} ({i}/{len(packages)})", done_items=i, total_items=len(packages))
            paths = self.adb.package_paths(pkg, self.serial)
            if not paths:
                failed.append(pkg)
                continue
            if not options.include_apk_splits:
                paths = [p for p in paths if p.endswith("base.apk")] or paths[:1]
            out_dir = bundle.apps_dir / pkg
            out_dir.mkdir(parents=True, exist_ok=True)
            saved: list[str] = []
            for remote in paths:
                dest = out_dir / posixpath.basename(remote)
                self.adb.pull(remote, str(dest), self.serial, timeout=600)
                if dest.exists():
                    saved.append(dest.name)
                    total_files += 1
                    total_bytes += dest.stat().st_size
            if saved:
                index[pkg] = saved
            else:
                failed.append(pkg)

        labels = self._app_labels(list(index))
        (bundle.apps_dir / "apps.json").write_text(
            json.dumps(
                {"packages": index, "labels": labels, "failed": failed},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        status = "partial" if failed else "ok"
        msg = f"{len(failed)} apps could not be copied (usually system-protected)" if failed else ""
        return SectionResult("apps", status, files=total_files, bytes=total_bytes, items=len(index), message=msg)

    def _app_labels(self, packages: list[str]) -> dict[str, str]:
        """Human names for the packages, best effort - purely cosmetic."""
        labels: dict[str, str] = {}
        for pkg in packages:
            res = self.adb.shell(
                f"dumpsys package {shlex.quote(pkg)} | grep -m1 -i 'application-label\\|versionName'",
                self.serial,
                timeout=20,
            )
            m = re.search(r"versionName=(\S+)", res.output)
            labels[pkg] = m.group(1) if m else ""
        return labels

    def _do_settings(self, bundle: Bundle, options: BackupOptions) -> SectionResult:
        out: dict[str, dict[str, str]] = {}
        for namespace in ("system", "secure", "global"):
            res = self.adb.shell(f"settings list {namespace}", self.serial, timeout=60)
            values: dict[str, str] = {}
            for line in res.lines():
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip()
            out[namespace] = values
        path = bundle.data_dir / "settings.json"
        path.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
        count = sum(len(v) for v in out.values())
        return SectionResult(
            "settings",
            "ok",
            files=1,
            bytes=path.stat().st_size,
            items=count,
            message="reference copy - Android does not allow most of these to be written back",
        )


class _PullTracker:
    """Turns adb pull's per-file progress lines into a byte count."""

    def __init__(self, root: str, sizes: dict[str, int]) -> None:
        self.root = root
        self.sizes = sizes
        self.done_bytes = 0
        self._current = ""
        self._counted: set[str] = set()

    def reset(self, base: int) -> None:
        self.done_bytes = base
        self._current = ""

    def saw(self, remote_path: str) -> None:
        if remote_path == self._current:
            return
        self._count_current()
        self._current = remote_path

    def finish(self) -> None:
        """Count the file that was in flight when the pull ended.

        Without this the last file of every folder is never counted, and the
        totals drift further below reality with each folder copied.
        """
        self._count_current()
        self._current = ""

    def _count_current(self) -> None:
        if self._current and self._current not in self._counted:
            self.done_bytes += self.sizes.get(self._current, 0)
            self._counted.add(self._current)

    def current_label(self) -> str:
        return posixpath.basename(self._current) or "copying"


def _local_path(f: RemoteFile, root: str, local_root: Path) -> Path:
    return local_root / f.relative_to(root)


def _already_complete(files: list[RemoteFile], root: str, local_root: Path) -> bool:
    for f in files:
        p = _local_path(f, root, local_root)
        if not p.exists():
            return False
        if f.size >= 0 and p.stat().st_size != f.size:
            return False
    return True


def _missing_files(files: list[RemoteFile], root: str, local_root: Path) -> list[RemoteFile]:
    missing = []
    for f in files:
        p = _local_path(f, root, local_root)
        if not p.exists() or (f.size >= 0 and p.stat().st_size != f.size):
            missing.append(f)
    return missing


def _local_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def companion_installed(adb: Adb, serial: str | None) -> bool:
    res = adb.shell(f"pm list packages {COMPANION_PACKAGE}", serial, timeout=20)
    return COMPANION_PACKAGE in res.output
