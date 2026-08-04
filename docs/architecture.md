# How it fits together

```
ptransfer_gui/           PySide6 window - pages + a thread pool, no logic of its own
    app.py               entry point
    main_window.py       wires page signals to engine calls via Job
    worker.py            QRunnable that turns engine events into Qt signals
    pages/               devices, backup, restore, rescue, guides

ptransfer/               the engine - pure stdlib, no Qt, importable and testable
    proc.py              subprocess wrapper + cancellation token
    platform_tools.py    finds/downloads adb + fastboot
    adb.py               adb, and the inventory parsers
    fastboot.py          fastboot, with the destructive-partition guard
    usb.py               raw VID/PID enumeration (PowerShell on Windows, lsusb elsewhere)
    devices.py           merges adb + fastboot + USB into one Device list
    content.py           contacts/SMS/call-log provider parsing
    manifest.py          the bundle format
    backup.py            phone  -> bundle
    restore.py           bundle -> phone
    recovery.py          diagnosis + rescue actions
    nokia.py             Nokia rescue: A/B slots, recovery-log analysis, on-device OTA
    oem/                 per-brand knowledge (nokia, sony, generic)
    progress.py          Event/Reporter shared by CLI and GUI
    cli.py               everything the GUI can do, scriptable
```

## The rules that shaped it

**The engine never imports Qt and never prints.** It emits `Event` objects to a `Reporter`.
The CLI renders them as text, the GUI as signals. That is why the whole engine is testable
without a display, and why the CLI is not a second-class citizen.

**One section failing must not lose the others.** `BackupEngine.run` catches per section,
records the failure in the manifest and continues. Someone whose SMS provider is locked down
still gets their photos.

**Vendor knowledge is data, not code.** `oem/` holds `VendorProfile` records — key combos,
tools, ordered rescue steps, each tagged `SAFE` / `USUALLY_SAFE` / `WIPES`. Adding a brand is
adding a record. The `WIPES` tag is what drives the red warnings in the GUI, so a destructive
step cannot be added without it being labelled. A step may also declare `applies_to` — the
device states it is worth showing in — so a fastboot-only action like the A/B slot switch
leads the plan in fastboot and is absent from the recovery plan, rather than being shown
everywhere or nowhere.

**Brand *actions* get their own module when there is real work to do.** `nokia.py` is the
example: reading the phone's `last_log` and translating known failure signatures, reading and
switching A/B slots, and finding the signed OTA the phone already downloaded. The generic
rescue explains; this executes.

**Refusals live in the layer that knows.** `Fastboot.flash` raises on `userdata`/`metadata`/
`persist` rather than relying on the UI to not offer it.

**Bundles are folders.** A 200 GB archive that fails at 99% is worthless. Folders resume,
can be inspected, and can be copied piecemeal. `manifest.json` records what was attempted and
what actually happened; `checksums.sha256` proves it arrived intact.

## Media copy strategy

Naive `adb pull /sdcard` gives no progress and silently drops files it cannot read. Per-file
pulls are accurate but far too slow across tens of thousands of photos. So:

1. Inventory via `find … -exec stat` (with `find`-only and `ls -laR` fallbacks — toybox has no
   `find -printf`).
2. Filter app-private and cache paths.
3. Bulk-pull each top-level folder — fast path, with progress parsed from adb's `[ nn%]` lines.
4. **Verify every file in the inventory arrived at the right size**, and retry individually
   whatever did not.
5. Anything still missing is written to `logs/media-not-copied.txt` and the section is marked
   `partial` — not `ok`.

Step 4 is the one that matters. It is the difference between "the copy finished" and "your
files are there".

Re-running into the same bundle skips top-level folders whose files are already present at
the right size, so an interrupted overnight transfer resumes instead of restarting.

## Testing

`tests/fake_phone.py` implements enough of adb to be worth trusting: a storage tree, packages,
content providers, and **real file writes on `pull`**. So the backup tests exercise the actual
verification pass, and `test_missing_files_are_reported_as_partial` proves the partial path by
making a file genuinely unpullable rather than mocking the outcome.

```bash
python -m pytest            # 129 tests, no hardware, no display
```

GUI tests run under `QT_QPA_PLATFORM=offscreen` and skip if PySide6 is absent.
