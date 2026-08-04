# How it fits together

```
ptransfer_gui/           PySide6 window - pages + a thread pool, no logic of its own
    app.py               entry point
    main_window.py       wires page signals to engine calls via Job
    worker.py            QRunnable that turns engine events into Qt signals
    pages/               devices, backup, restore, screen, rescue, guides

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
    authorize.py         triggers the phone's 'Allow USB debugging?' prompt
    nokia.py             Nokia rescue: A/B slots, log analysis, on-device OTA, boot-loop repair
    screen.py            screenshot fallback + remote input (tap/swipe/type/unlock)
    mirror.py            scrcpy: the real mirroring engine, and its failure messages
    menu.py              the stock recovery menu, annotated with what each option costs
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
python -m pytest            # 245 tests, no hardware, no display
```

GUI tests run under `QT_QPA_PLATFORM=offscreen` and skip if PySide6 is absent. They build real
widgets and assert on rendered output — which is how the `<file.zip>` escaping bug was caught:
Qt's rich text was swallowing the filename as an unknown tag.

## Reporting a repair honestly

`repair_boot_loop` runs two experiments and has to distinguish three outcomes, not two: it
worked, it did not work, and *it could not be tried*. An option skipped because the phone was
in the wrong mode is not evidence against it, and reporting it as failure would push someone
toward a factory reset with a repair still untried — so `_advice_after_failure` separates
`performed` from `skipped` and says which is which. There is a fourth case too: adb cannot see
a booted phone when USB debugging was never enabled, so `wait_for_boot` returns `None` rather
than `False` and says to look at the handset.

Anything reversible is recorded before it happens. The slot switch writes the original slot to
the user data directory, so `--undo-slot` works in a later session — the phone may not come
back for hours, and by then the terminal that made the change is long gone.

## Two things the screen code has to get right

**Binary output.** `screencap -p` returns a PNG, so it goes through `Runner.run_bytes`; the
normal text path decodes as UTF-8 with replacement and destroys the image. There is also a
CRLF-repair fallback for old builds whose shell rewrites `\n` on the way out.

**Coordinates.** The view scales the phone's screenshot to fit, so a click has to be mapped
back through that scale before `input tap` sees it — `map_to_device` does that and clamps to
the panel, and a drag past a threshold becomes a swipe rather than a tap.
