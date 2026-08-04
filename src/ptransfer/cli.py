"""Command line interface.

Everything the GUI can do is available here, which makes the app scriptable and
makes the engine testable without Qt.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from . import __version__
from .authorize import Authorizer
from .backup import SECTIONS, BackupEngine, BackupOptions
from .devices import Device, DeviceManager, State
from .logging_setup import setup_logging
from .manifest import Bundle, safe_bundle_name
from .menu import as_text as menu_as_text
from .mirror import ScrcpySession, choose_backend, explain_failure
from .nokia import OTA_SEARCH_DIRS, NokiaRescue, is_nokia
from .oem import ALL_PROFILES, profile_for
from .platform_tools import discover, download_platform_tools, download_scrcpy, user_data_dir
from .proc import Cancel, ToolError
from .progress import Event, Reporter, human_bytes
from .recovery import RescueEngine, diagnose
from .restore import RestoreEngine, RestoreOptions
from .screen import PhoneScreen, ScreenUnavailable, scrcpy_path
from .transfer import TransferEngine, TransferOptions, pick_pair

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

    tr = sub.add_parser("transfer", help="copy one phone straight onto another, both plugged in")
    tr.add_argument("--from", dest="source", default="", metavar="SERIAL", help="the old phone")
    tr.add_argument("--to", dest="target", default="", metavar="SERIAL", help="the new phone")
    tr.add_argument("--staging", default=".", help="where to hold the copy on the way through")
    tr.add_argument("--sections", nargs="+", choices=SECTIONS, default=list(SECTIONS))
    tr.add_argument("--no-apps", action="store_true", help="do not install apps on the new phone")
    tr.add_argument("--delete-bundle", action="store_true",
                    help="remove the staging copy afterwards (it is kept by default)")
    tr.add_argument("--check", action="store_true", help="run the checks only, transfer nothing")

    v = sub.add_parser("verify", help="check a bundle against its checksums")
    v.add_argument("bundle")

    rescue = sub.add_parser("rescue", help="diagnose a phone that will not boot")
    rescue.add_argument("--brand", default="", help="force a brand guide, e.g. nokia or sony")
    rescue.add_argument("--extract", metavar="DIR", help="try to copy storage out of recovery mode")
    rescue.add_argument("--sideload", metavar="ZIP", help="apply a signed OTA zip from recovery")
    rescue.add_argument("--restart-adb", action="store_true", help="restart the adb server and rescan")
    rescue.add_argument("--menu", action="store_true",
                        help="explain the recovery menu on the phone's screen and what each option costs")

    au = sub.add_parser("authorize", aliases=["debug"],
                        help="make the phone show its 'Allow USB debugging?' prompt")
    au.add_argument("--check", action="store_true", help="only report the situation, ask for nothing")
    au.add_argument("--brand", default="", help="tailor the on-phone steps, e.g. samsung or nokia")
    au.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for you to tap Allow")

    sc = sub.add_parser("screen", help="see the phone's screen and control it from here")
    sc.add_argument("--shot", metavar="FILE", help="save one screenshot and exit")
    sc.add_argument("--tap", nargs=2, type=int, metavar=("X", "Y"), help="tap a point")
    sc.add_argument("--swipe", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"), help="swipe")
    sc.add_argument("--key", help="send a key: home, back, power, enter, volume_up ...")
    sc.add_argument("--text", help="type text on the phone")
    sc.add_argument("--unlock", action="store_true",
                    help="wake, dismiss the lock screen and enter a passcode (prompted, not echoed)")
    sc.add_argument("--mirror", action="store_true",
                    help="open the live mirror (scrcpy), installing it first if needed")

    n = sub.add_parser("nokia", help="Nokia-specific rescue: slot switching, log analysis, on-device OTA")
    n.add_argument("--switch-slot", nargs="?", const="", metavar="A|B",
                   help="boot from the other system slot (data-safe, writes nothing)")
    n.add_argument("--logs", action="store_true", help="dump the full recovery log")
    n.add_argument("--apply-ota", action="store_true", help="use an update package already on the phone")
    n.add_argument("--fix-bootloop", action="store_true",
                   help="undo the update, or finish it, and check whether the phone boots")
    n.add_argument("--strategy", choices=["auto", "revert", "update", "both"], default="auto",
                   help="auto (default) reverts first, then tries finishing the update")
    n.add_argument("--ota", metavar="ZIP", default="", help="update package to use with --strategy update")
    n.add_argument("--timeout", type=float, default=240.0, help="seconds to wait for a boot (default 240)")
    n.add_argument("--undo-slot", action="store_true", help="put the active slot back after a switch")
    n.add_argument("--no-rollback", action="store_true",
                   help="leave the new slot active even if it did not boot")
    n.add_argument("--workdir", default=".", help="where to save pulled packages")
    n.add_argument("--report", metavar="FILE", help="write the full report to a file")

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
    # The recovery-menu explainer is just text - someone reading it is looking
    # at a phone that adb cannot reach anyway.
    if args.command == "rescue" and args.menu:
        _line(menu_as_text())
        return 0

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
        "nokia": cmd_nokia,
        "screen": cmd_screen,
        "transfer": cmd_transfer,
        "authorize": cmd_authorize,
        "debug": cmd_authorize,
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
    if args.menu:
        _line(menu_as_text())
        return 0

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


def cmd_transfer(args, manager: DeviceManager) -> int:
    devices = manager.scan()
    ready = [d for d in devices if d.state.can_transfer]

    source = _find(devices, args.source) if args.source else None
    target = _find(devices, args.target) if args.target else None

    if source is None or target is None:
        guess_source, guess_target = pick_pair(devices)
        source = source or guess_source
        target = target or guess_target

    if source is None or target is None:
        _line("Two phones need to be connected and ready; found " f"{len(ready)}.")
        for d in devices:
            _line(f"  {d.summary()}")
        _line("")
        _line("Both phones must be booted with USB debugging allowed.")
        _line("Run 'ptransfer authorize' for whichever one is not ready.")
        return 1

    _line(f"From: {source.summary()}")
    _line(f"To:   {target.summary()}")
    _line("")

    engine = TransferEngine(manager, source, target, Reporter(console_sink(args.verbose)), Cancel())

    problems = engine.preflight(args.staging)
    if problems:
        _line("Cannot start:")
        for p in problems:
            _line(f"  {p}")
        return 1
    if args.check:
        _line("Checks passed - both phones are ready and there is room to work.")
        return 0

    options = TransferOptions(
        sections=tuple(args.sections),
        keep_bundle=not args.delete_bundle,
        install_apps=not args.no_apps,
    )
    report = engine.run(Path(args.staging), options)
    _line("")
    _line(report.as_text())
    return 0 if report.ok else 1


def _find(devices: list[Device], serial: str) -> Device | None:
    for d in devices:
        if d.serial == serial:
            return d
    return None


def cmd_authorize(args, manager: DeviceManager) -> int:
    auth = Authorizer(manager, Reporter(console_sink(args.verbose)), Cancel())

    situation = auth.situation(args.brand)
    _line(situation.as_text())

    if situation.ready:
        return 0
    if args.check:
        return 1

    if not situation.can_prompt:
        # Nothing to trigger - the steps above are the whole answer.
        return 1

    _line("")
    result = auth.request(timeout=args.timeout, brand_hint=args.brand)
    _line("")
    _line(result.message)
    return 0 if result.authorised else 1


def cmd_screen(args, manager: DeviceManager) -> int:
    device = _select(manager, args.serial)
    if device is None:
        return 1
    try:
        PhoneScreen.check_supported(device)
    except ScreenUnavailable as exc:
        _line(str(exc))
        return 1

    screen = PhoneScreen(manager.adb, device.serial or None)

    if args.shot:
        try:
            frame = screen.capture()
        except ScreenUnavailable as exc:
            _line(str(exc))
            return 1
        Path(args.shot).write_bytes(frame.png)
        _line(f"Saved {args.shot} ({frame.width}x{frame.height})")
        return 0

    if args.mirror:
        plan = choose_backend()
        if plan.needs_download:
            _line("Installing scrcpy (about 30 MB, one time only) ...")
            try:
                download_scrcpy(progress=lambda f: sys.stdout.write(f"\r  {f * 100:5.1f}%") if sys.stdout.isatty() else None)
            except Exception as exc:
                _line(f"\nCould not install scrcpy: {exc}")
                return 2
            _line("")
        path = scrcpy_path()
        if not path:
            _line("scrcpy is not available on this system.")
            return 2
        session = ScrcpySession(
            path=path, serial=device.serial or None, borderless=False, tools=manager.tools
        )
        if not session.start():
            _line(session.error)
            return 2
        _line("Live mirror open. Close its window to stop.")
        if session.process is not None:
            session.process.wait()
        output = session.read_output()
        if output:
            _line(explain_failure(output))
        return 0

    did_something = False
    if args.tap:
        did_something = True
        _line("tapped" if screen.tap(*args.tap) else "tap failed")
    if args.swipe:
        did_something = True
        _line("swiped" if screen.swipe(*args.swipe) else "swipe failed")
    if args.key:
        did_something = True
        _line("sent" if screen.key(args.key) else "key failed")
    if args.text:
        did_something = True
        _line("typed" if screen.type_text(args.text) else "typing failed")
    if args.unlock:
        did_something = True
        # Prompted rather than passed as an argument, so it stays out of the
        # shell history and the process list.
        code = getpass.getpass("Passcode (not shown): ")
        _line(screen.unlock(code, code.isdigit()))

    if not did_something:
        size = screen.size()
        _line(f"{device.label}: screen is {size.width}x{size.height}")
        locked = screen.is_locked()
        if locked is not None:
            _line("Lock screen is " + ("up" if locked else "not showing"))
        _line("")
        _line("Use --shot, --tap, --swipe, --key, --text or --unlock.")
        _line("For a live view, run 'ptransfer gui' and open the Screen tab.")
    return 0


def cmd_nokia(args, manager: DeviceManager) -> int:
    reporter = Reporter(console_sink(args.verbose))
    rescue = NokiaRescue(manager, reporter, Cancel())

    devices = manager.scan()
    if not devices:
        _line("No phone detected.")
        _line("")
        _line("For a Nokia that will not boot, get it into one of these first:")
        _line("  recovery  - power off, hold Volume Up, then press and hold Power")
        _line("  fastboot  - power off, hold Volume Down, then connect the USB cable")
        _line("")
        _line("Then run this again. Recovery gives the log; fastboot gives the slot switch.")
        return 1

    device = _select_nokia(devices)
    if device is None:
        return 1

    if args.undo_slot:
        outcome = rescue.undo_slot_switch(device)
        _line(f"{outcome.status}: {outcome.message}")
        if outcome.detail:
            _line(f"  {outcome.detail}")
        return 0 if outcome.helped else 1

    if args.fix_bootloop:
        _line("Trying to get the phone booting again. Nothing here erases anything.")
        _line("Leave the cable connected - this takes a few minutes per attempt.")
        _line("")
        session = rescue.repair_boot_loop(
            device,
            strategy=args.strategy,
            zip_path=args.ota,
            workdir=Path(args.workdir),
            timeout=args.timeout,
        )
        _line("")
        _line(session.as_text())
        return 0 if session.fixed else 1

    # A single action was asked for.
    if args.switch_slot is not None:
        outcome = rescue.switch_slot(device, args.switch_slot or "")
        _line(f"{outcome.status}: {outcome.message}")
        if outcome.detail:
            _line(f"  {outcome.detail}")
        if outcome.helped:
            _line("")
            _line("Now reboot the phone:  ptransfer reboot")
        return 0 if outcome.helped else 1

    if args.logs:
        text = rescue.read_recovery_log(device)
        if not text:
            _line("No recovery log reachable. Put the phone in recovery mode and try again.")
            return 1
        _line(text)
        findings = rescue.analyse_log(text)
        if findings:
            _line("")
            _line("Recognised problems:")
            for f in findings:
                _line(f"  [{f.severity}] {f.meaning}")
                _line(f"      -> {f.next_step}")
        return 0

    if args.apply_ota:
        packages = rescue.find_ota_packages(device)
        if not packages:
            _line("No update package found on the phone.")
            _line("Checked: " + ", ".join(OTA_SEARCH_DIRS))
            return 1
        _line("Update packages on the phone:")
        for p in packages:
            _line(f"  {p}")
        outcome = rescue.apply_ota_from_device(device, packages[0], Path(args.workdir))
        _line("")
        _line(f"{outcome.status}: {outcome.message}")
        if outcome.detail:
            _line(f"  {outcome.detail}")
        return 0 if outcome.status in ("ok", "needs-user") else 1

    # Default: the full guided run.
    report = rescue.guided_rescue(device, Path(args.workdir))
    _line("")
    _line(report.as_text())

    if args.report:
        path = Path(args.report)
        body = report.as_text()
        if report.log_excerpt:
            body += "\n\n===== recovery log =====\n" + report.log_excerpt
        path.write_text(body, encoding="utf-8")
        _line("")
        _line(f"Full report written to {path}")
    return 0


def _select_nokia(devices: list[Device]) -> Device | None:
    nokias = [d for d in devices if is_nokia(d)]
    if nokias:
        return nokias[0]
    if len(devices) == 1:
        device = devices[0]
        _line(
            f"Note: {device.label} does not identify as a Nokia, but it is the only phone "
            "connected, so continuing with it."
        )
        _line("(A phone in fastboot or a low-level mode often reports no brand at all.)")
        _line("")
        return device
    _line("More than one phone connected and none identifies as a Nokia. Use -s SERIAL.")
    for d in devices:
        _line(f"  {d.summary()}")
    return None


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
