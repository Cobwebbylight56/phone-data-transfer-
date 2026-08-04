"""Command line interface.

Everything the GUI can do is available here, which makes the app scriptable and
makes the engine testable without Qt.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .backup import SECTIONS, BackupEngine, BackupOptions
from .devices import Device, DeviceManager, State
from .logging_setup import setup_logging
from .manifest import Bundle, safe_bundle_name
from .oem import ALL_PROFILES, profile_for
from .platform_tools import download_platform_tools, discover, user_data_dir
from .proc import Cancel, ToolError
from .progress import Event, Reporter, human_bytes
from .recovery import RescueEngine, diagnose
from .restore import RestoreEngine, RestoreOptions

log = logging.getLogger(__name__)


def console_sink(verbose: bool):
    last_len = 0
    # Redrawing one line with \r is right for a terminal and unreadable in a
    # redirected log, so only do it when someone is actually watching.
    interactive = sys.stdout.isatty()

    def sink(e: Event) -> None:
        nonlocal last_len
        if e.kind == "section":
            _line("")
            _line(f"== {e.message}")
        elif e.kind == "progress":
            if not interactive:
                return
            pct = f"{e.fraction * 100:5.1f}%" if e.fraction >= 0 else "  ... "
            extra = ""
            if e.total_bytes:
                extra = f" {human_bytes(e.done_bytes)}/{human_bytes(e.total_bytes)}"
            elif e.total_items:
                extra = f" {e.done_items}/{e.total_items}"
            text = f"   {pct}{extra}  {e.message}"[:110]
            sys.stdout.write("\r" + text.ljust(last_len))
            sys.stdout.flush()
            last_len = len(text)
        elif e.kind == "status":
            _flush_progress(last_len)
            last_len = 0
            _line(f"   {e.message}")
        elif e.kind == "warning":
            _flush_progress(last_len)
            last_len = 0
            _line(f"   ! {e.message}")
        elif e.kind == "error":
            _flush_progress(last_len)
            last_len = 0
            _line(f"   ERROR {e.message}")
        elif e.kind == "done":
            _flush_progress(last_len)
            last_len = 0
            if e.message:
                _line(e.message)

    return sink


def _line(text: str = "") -> None:
    print(text, flush=True)


def _flush_progress(last_len: int) -> None:
    if last_len:
        sys.stdout.write("\r" + " " * last_len + "\r")
        sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ptransfer",
        description="Copy everything off an Android phone, put it on the new one, and rescue a phone that will not boot.",
    )
    p.add_argument("--version", action="version", version=f"ptransfer {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("-s", "--serial", default=None, help="target a specific device serial")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list everything connected, including phones adb cannot see")
    sub.add_parser("info", help="detailed report on the selected phone")
    sub.add_parser("setup", help="download Google's platform-tools (adb/fastboot)")
    sub.add_parser("gui", help="launch the desktop app")

    b = sub.add_parser("backup", help="copy the phone into a transfer bundle")
    b.add_argument("-o", "--out", required=True, help="folder to create the bundle in")
    b.add_argument("--name", default="", help="bundle name (default: model + timestamp)")
    b.add_argument("--sections", nargs="+", choices=SECTIONS, default=list(SECTIONS))
    b.add_argument("--no-verify", action="store_true", help="skip checksum pass")
    b.add_argument("--include-app-data-note", action="store_true", help=argparse.SUPPRESS)
    b.add_argument("--exclude", nargs="+", default=None, help="extra media glob excludes")
    b.add_argument("--fresh", action="store_true", help="re-copy files even if already present")

    r = sub.add_parser("restore", help="write a bundle onto the new phone")
    r.add_argument("bundle", help="path to the .ptbundle folder")
    r.add_argument("--sections", nargs="+", default=["media", "apps", "contacts", "sms", "calls"])
    r.add_argument("--no-apps", action="store_true", help="do not install APKs")

    v = sub.add_parser("verify", help="check a bundle against its checksums")
    v.add_argument("bundle")

    rescue = sub.add_parser("rescue", help="diagnose a phone that will not boot")
    rescue.add_argument("--brand", default="", help="force a brand guide, e.g. nokia or sony")
    rescue.add_argument("--extract", metavar="DIR", help="try to copy storage out of recovery mode")
    rescue.add_argument("--sideload", metavar="ZIP", help="apply a signed OTA zip from recovery")
    rescue.add_argument("--restart-adb", action="store_true", help="restart the adb server and rescan")

    g = sub.add_parser("guide", help="print the rescue guide for a brand")
    g.add_argument("brand", nargs="?", default="", help="nokia, sony, samsung, ... (blank lists them)")

    rb = sub.add_parser("reboot", help="reboot the phone")
    rb.add_argument("target", nargs="?", default="", choices=["", "recovery", "bootloader", "fastboot", "sideload"])

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose)

    if args.command == "gui":
        return _launch_gui()
    if args.command == "setup":
        return cmd_setup()
    if args.command == "guide":
        return cmd_guide(args)
    if args.command == "verify":
        return cmd_verify(args)

    manager = DeviceManager()
    ready, message = manager.tools_ready()
    if not ready:
        _line(message)
        _line("Run 'ptransfer setup' to fetch them automatically.")
        return 2

    handlers = {
        "devices": cmd_devices,
        "info": cmd_info,
        "backup": cmd_backup,
        "restore": cmd_restore,
        "rescue": cmd_rescue,
        "reboot": cmd_reboot,
    }
    try:
        return handlers[args.command](args, manager)
    except ToolError as exc:
        _line(f"Tool problem: {exc}")
        return 2
    except KeyboardInterrupt:
        _line("\nStopped.")
        return 130


# --- commands ---------------------------------------------------------
def cmd_setup() -> int:
    _line("Downloading Google platform-tools ...")
    try:
        out = download_platform_tools(progress=lambda f: sys.stdout.write(f"\r  {f * 100:5.1f}%"))
    except Exception as exc:
        _line(f"\nDownload failed: {exc}")
        _line("You can also unzip platform-tools yourself next to the app.")
        return 2
    _line(f"\nInstalled to {out}")
    return 0


def cmd_devices(args, manager: DeviceManager) -> int:
    devices = manager.scan()
    if not devices:
        _line("Nothing connected.")
        _line("If a phone is plugged in: try a different (data-capable) cable and a rear USB port.")
        return 1
    for d in devices:
        _line(d.summary())
        if d.usb is not None and d.usb.mode():
            _line(f"    USB: {d.usb}")
        if d.note:
            _line(f"    {d.note}")
    return 0


def cmd_info(args, manager: DeviceManager) -> int:
    device = _select(manager, args.serial)
    if device is None:
        return 1
    _line(device.summary())
    profile = profile_for(device.vendor_key)
    _line(f"Brand guide: {profile.display_name}")
    if device.state.can_transfer:
        adb = manager.adb
        root = adb.storage_root(device.serial)
        _line(f"Storage root: {root}")
        battery = adb.battery_level(device.serial)
        if battery is not None:
            _line(f"Battery: {battery}%")
            if battery < 30:
                _line("  Charge above 50% before a long transfer.")
        packages = adb.third_party_packages(device.serial)
        _line(f"User-installed apps: {len(packages)}")
        res = adb.shell(f"df -h {root} | tail -n 1", device.serial)
        if res.ok:
            _line(f"Storage: {res.stdout.strip()}")
    else:
        _line("Phone is not in a state where its contents can be read.")
        _line("Run 'ptransfer rescue' for the recovery plan.")
    return 0


def cmd_backup(args, manager: DeviceManager) -> int:
    device = _select(manager, args.serial)
    if device is None:
        return 1
    if not device.state.can_transfer:
        _line(f"Phone is {device.state.value}, so its files cannot be read yet.")
        _line("Run 'ptransfer rescue' first.")
        return 1

    name = args.name or safe_bundle_name(device.label)
    dest = Path(args.out) / (name if name.endswith(".ptbundle") else name + ".ptbundle")
    _line(f"Backing up {device.label} -> {dest}")

    options = BackupOptions(
        sections=tuple(args.sections),
        verify=not args.no_verify,
        skip_existing=not args.fresh,
    )
    if args.exclude:
        options.media_excludes = options.media_excludes + tuple(args.exclude)

    engine = BackupEngine(manager.adb, device, Reporter(console_sink(args.verbose)), Cancel())
    report = engine.run(dest, options)

    _line("")
    _line("Summary:")
    for line in report.summary_lines():
        _line(line)
    if report.warnings:
        _line("")
        _line("Worth knowing:")
        for w in report.warnings:
            _line(f"  ! {w}")
    _line("")
    _line(f"Bundle: {report.bundle_path}")
    return 0 if report.ok else 1


def cmd_restore(args, manager: DeviceManager) -> int:
    device = _select(manager, args.serial)
    if device is None:
        return 1
    if not device.state.can_transfer:
        _line(f"The new phone is {device.state.value}; it needs to be booted with USB debugging on.")
        return 1

    options = RestoreOptions(sections=tuple(args.sections), install_apps=not args.no_apps)
    engine = RestoreEngine(manager.adb, device.serial or None, Reporter(console_sink(args.verbose)), Cancel())
    report = engine.run(args.bundle, options)

    _line("")
    _line("Summary:")
    for line in report.summary_lines():
        _line(line)
    pending = report.needs_user
    if pending:
        _line("")
        _line("Finish these on the phone itself:")
        for step in pending:
            _line(f"  {step.name}: {step.message}")
    return 0


def cmd_verify(args) -> int:
    try:
        bundle = Bundle.open(args.bundle)
    except FileNotFoundError as exc:
        _line(str(exc))
        return 2
    _line(f"Verifying {bundle.root} ({human_bytes(bundle.size_on_disk())})")
    show = (
        (lambda f, rel: sys.stdout.write(f"\r  {f * 100:5.1f}%  {rel[:60]:<60}"))
        if sys.stdout.isatty()
        else None
    )
    problems = bundle.verify(progress=show)
    _line("")
    if not problems:
        _line("Bundle is intact - every file matches its checksum.")
        return 0
    _line(f"{len(problems)} problems:")
    for p in problems[:40]:
        _line(f"  {p}")
    return 1


def cmd_rescue(args, manager: DeviceManager) -> int:
    reporter = Reporter(console_sink(args.verbose))
    engine = RescueEngine(manager, reporter, Cancel())

    if args.restart_adb:
        _line(engine.restart_adb())

    devices = manager.scan()
    result = diagnose(devices, hint_brand=args.brand)
    _line(result.as_text())

    if args.extract:
        if result.device is None:
            _line("\nNothing connected to extract from.")
            return 1
        if result.device.state not in (State.RECOVERY, State.SIDELOAD):
            _line("\n--extract only works while the phone is in recovery mode.")
            _line("Use the key combination above to get there first.")
            return 1
        _line("")
        outcome = engine.extract_from_recovery(result.device, args.extract)
        _line("")
        _line(outcome.message)
        return 0 if outcome.succeeded else 1

    if args.sideload:
        if result.device is None:
            _line("\nNothing connected.")
            return 1
        _line("")
        _line(engine.sideload(result.device, args.sideload))
    return 0


def cmd_guide(args) -> int:
    if not args.brand:
        _line("Brand guides available:")
        for p in ALL_PROFILES:
            _line(f"  {p.key:<12} {p.display_name}")
        return 0
    profile = profile_for(args.brand)
    _line(profile.display_name)
    _line("=" * len(profile.display_name))
    if profile.key_combos:
        _line("\nGetting into each mode:")
        for c in profile.key_combos:
            _line(f"\n  {c.mode}")
            _line(f"    {c.steps}")
            if c.note:
                _line(f"    ({c.note})")
    if profile.tools:
        _line("\nTools:")
        for t in profile.tools:
            safety = {"safe": "data-safe", "usually": "usually keeps data", "wipes": "ERASES DATA"}[t.data_safety.value]
            _line(f"\n  {t.name}  [{safety}]")
            _line(f"    {t.purpose}")
            if t.url:
                _line(f"    {t.url}")
            if t.note:
                _line(f"    {t.note}")
    if profile.rescue_steps:
        _line("\nIf it will not boot:")
        for i, s in enumerate(profile.rescue_steps, 1):
            flag = "  [WIPES DATA]" if s.warns else ""
            _line(f"\n  {i}. {s.title}{flag}")
            _line(f"     {s.detail}")
    if profile.notes:
        _line("\nWorth knowing:")
        for n in profile.notes:
            _line(f"  - {n}")
    return 0


def cmd_reboot(args, manager: DeviceManager) -> int:
    device = _select(manager, args.serial)
    if device is None:
        return 1
    target = "bootloader" if args.target == "fastboot" else args.target
    engine = RescueEngine(manager)
    _line(engine.reboot(device, target))
    return 0


# --- helpers ----------------------------------------------------------
def _select(manager: DeviceManager, serial: str | None) -> Device | None:
    devices = manager.scan()
    if not devices:
        _line("No phone detected.")
        _line("Check the cable is a data cable, and that USB debugging is on.")
        return None
    if serial:
        for d in devices:
            if d.serial == serial:
                return d
        _line(f"No device with serial {serial}. Connected: {', '.join(d.serial or '?' for d in devices)}")
        return None
    if len(devices) > 1:
        online = [d for d in devices if d.state.can_transfer]
        if len(online) == 1:
            return online[0]
        _line("More than one device connected - pick one with -s SERIAL:")
        for d in devices:
            _line(f"  {d.summary()}")
        return None
    return devices[0]


def _launch_gui() -> int:
    try:
        from ptransfer_gui.app import run
    except ImportError as exc:
        _line(f"The desktop app needs PySide6: pip install PySide6  ({exc})")
        return 2
    return run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
