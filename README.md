# Phone Data Transfer

A Windows app that copies everything off an Android phone, puts it on the new one, and
helps you rescue a phone that will not boot.

Works with any Android brand. Nokia (HMD) and Sony Xperia get dedicated rescue guides,
because those are the two you asked for and they are the two where brand-specific knowledge
makes the biggest difference.

---

## What it copies

| | Copied | How it comes back |
|---|---|---|
| Photos, videos, music, documents, downloads | ✅ everything in internal storage | copied straight back |
| Contacts | ✅ as a standard `.vcf` | one tap to import on the new phone |
| Text messages | ✅ as JSON | needs the companion app (see below) |
| Call log | ✅ as JSON | needs the companion app |
| Installed apps | ✅ the APKs, including split APKs | installed automatically |
| App *private* data (game saves, chat history inside apps) | ❌ | see below |
| Settings | ✅ as a readable snapshot | reference only |

### Why app data is not in that list

Android deliberately walls off each app's private storage. Without rooting the phone,
nothing on a PC can read it — not this app, and not the paid ones that imply otherwise.
What actually gets your data back is signing into each app on the new phone. WhatsApp is
the usual exception people care about: use its own chat backup before you switch.

This app tells you that up front rather than quietly skipping it.

---

## Rescuing a phone that will not boot

This is the part worth reading before you touch anything.

**Since Android 10, your files are encrypted with a key that is unwrapped by your screen
lock, after Android boots.** A phone stuck at the logo has never unwrapped it. So a PC
plugged into a dead phone is looking at ciphertext. No tool can read your photos off it in
that state.

Which means the route to your data is always the same:

> repair the boot **without wiping userdata** → let Android start → back up immediately

The Rescue tab is built around exactly that order. It:

- checks adb, fastboot **and the raw USB layer**, so it can identify a phone that ordinary
  tools cannot see at all (Sony flash mode, Qualcomm 9008, MediaTek preloader, Odin mode);
- reads Windows' own device errors and translates them ("code 28" → "no driver installed");
- gives you the button combinations for your brand;
- ranks every repair by whether it keeps your data, and colours the destructive ones red;
- **tries** to pull your files straight out of recovery mode, and tells you honestly what it
  found — including when the file names come back as encrypted gibberish;
- refuses to flash `userdata`, `metadata` or `persist`. On purpose. Those are the partitions
  that destroy what you are trying to save.

It will never unlock your bootloader. On a locked phone that forces a factory reset, which
is the single most common way people permanently lose the data they were trying to rescue.

### Nokia (HMD) — the guided rescue

```powershell
ptransfer nokia
```

This does real work rather than giving advice. Everything it does is read-only or
non-destructive:

**1. Reads the phone's own crash log.** Stock recovery writes *why* the boot or update failed
to `last_log`, and it is readable over adb whenever the phone reaches recovery. It usually
names the problem outright — "failed to mount /data", "dm-verity verification failed",
"package is for product TA-1234 but expected TA-1243" — and the app translates each into what
it means and what to do. Almost nobody looks at this file; it is the most informative thing on
a phone that will not start.

**2. Undoes the update, or finishes it — and checks whether that fixed the boot loop.**

```powershell
ptransfer nokia --fix-bootloop
```

This runs the experiment rather than describing it:

1. **Undo the update.** Nearly every Nokia since the Android One line carries two complete
   system slots. A failed update leaves the new slot unbootable while the previous, working
   system sits untouched in the other — so switching back *is* undoing the update. Nothing is
   written and userdata is not touched.
2. **Restart and watch.** It waits for the phone to come back and checks `sys.boot_completed`,
   so you get a real answer. A phone that appears and vanishes repeatedly is reported as still
   looping rather than as an ambiguous timeout.
3. **Roll back if it didn't help.** The original slot is restored automatically once the phone
   returns to fastboot, and recorded to disk either way so `ptransfer nokia --undo-slot` works
   later, even in a new session.
4. **Then try the other direction** — finish the interrupted update: put the phone into
   sideload, apply the package in full, reboot and check again.

Reverting goes first deliberately: it writes nothing, takes two minutes and is trivially
undone, whereas applying an update rewrites system partitions.

```powershell
ptransfer nokia --fix-bootloop --strategy revert            # only undo
ptransfer nokia --fix-bootloop --strategy update --ota u.zip
ptransfer nokia --switch-slot        # just the switch, no reboot-and-check
ptransfer nokia --undo-slot          # put the slot back
```

If the locked bootloader refuses the switch, that is still not a dead end: it falls back to
the other slot by itself after several failed boot attempts, so let the phone keep trying.

One thing it is careful about: an option **skipped** because the phone was in the wrong mode is
reported as untried, not as ruled out — otherwise you'd be pushed toward a factory reset with a
repair still on the table.

**3. Finds an update package the phone already downloaded.** If an OTA finished downloading
before the phone broke, the signed zip is still in `/data/ota_package` or `/cache`. HMD signed
it, so recovery accepts it — and it is the one legitimate route to a signed Nokia OTA, since
HMD does not publish them:

```powershell
ptransfer nokia --apply-ota
```

**4. Identifies the chipset** (MediaTek / Qualcomm / Unisoc) from `ro.board.platform`, or from
the USB vendor ID when the phone is too broken to read properties — so you know which
low-level mode to expect.

Other commands:

```powershell
ptransfer nokia --logs                  # the full crash log, plus what it means
ptransfer nokia --report nokia.txt      # everything, written to a file
```

In the window, the Nokia panel appears on the Rescue tab automatically when a Nokia is
detected.

**What it still won't do:** HMD's flashing tools (OST LA / NOST) go to authorised service
points only, and forum copies are unlicensed and routinely repackaged with malware — so the
app neither links to nor drives them. If the steps above don't get it booting, it is a Nokia
care point; ask them explicitly for a reflash that **preserves userdata**, because the default
service procedure often wipes.

Run `ptransfer guide nokia` for the full write-up.

### Sony Xperia

Sony is the best-case brand for this. Flash mode (green LED) and fastboot (blue LED) work
even when Android is dead, Sony publishes signed firmware, and an FTF can be flashed **with
the userdata image deleted from the folder** — which repairs the system and leaves your files
untouched. That is the most useful data-preserving repair available on any brand.

One correction to the folklore: Xperia Companion's *Software repair* erases personal data.
Its *Software update* path does not. Try update first.

Run `ptransfer guide sony`.

---

## Phone to phone, both plugged in

Plug both phones into the PC. **Phone to phone** tab: pick the old one on the left, the new
one on the right, press Start.

```powershell
ptransfer transfer                       # picks the pair, asks nothing else
ptransfer transfer --from R58M1 --to R58M2
ptransfer transfer --check               # run the checks only, move nothing
```

This is the route to use **when a screen is broken**, because the tap-to-tap transfer apps —
Smart Switch, Xperia Transfer, Google's cable copy — all need you to tap through a wizard on
the phone itself. With the PC as the hub there is nothing to tap: each side has a **Show this
phone's screen** button that mirrors it so you can see and drive it from here.

What it does:

- reads the old phone into a staging bundle on the PC, then writes that to the new one
- **never writes to the old phone** — it is only ever read
- **keeps the bundle** by default, so you still hold a full verified backup afterwards, and an
  interrupted transfer resumes instead of restarting
- checks before it starts: both phones ready, not the same phone twice, and enough disk space
- lists what still needs a tap on the new phone (contacts import, messages via the companion
  app) rather than quietly finishing

---

## Installing on Windows

Pick **one** of these. Option A needs nothing installed; option B is fastest if you already
have Python.

### A. Download the built app (no Python needed)

Every push builds a Windows `.exe` automatically.

1. Go to the repo on GitHub → **Actions** tab
2. Click the newest **build-windows** run (green tick = finished)
3. Scroll to **Artifacts** at the bottom and download **PhoneDataTransfer-windows**
4. Unzip it anywhere — Desktop is fine
5. Double-click **PhoneDataTransfer.exe**

`adb` and `fastboot` are already inside that folder, so there is nothing else to install.

Windows will likely show a blue **"Windows protected your PC"** box, because the build isn't
code-signed (signing certificates cost money). Click **More info** → **Run anyway**. If you'd
rather not, use option B, which builds it on your own machine from source you can read.

### B. Install from source (one click)

1. Install Python 3.10+ if you don't have it — in PowerShell:
   `winget install Python.Python.3.12`
   (or from [python.org](https://www.python.org/downloads/) — tick **Add python.exe to PATH**)
2. Download this repo: green **Code** button → **Download ZIP** → unzip it
   (or `git clone` it if you have git)
3. Double-click **`install.bat`**

It creates a private `.venv` folder, installs everything, downloads `adb`/`fastboot`, and puts
a **Phone Data Transfer** shortcut on your Desktop. Nothing is scattered around your system —
deleting the folder removes it completely.

Then use either:

- **`Phone Data Transfer.bat`** (or the Desktop shortcut) for the window
- **`ptransfer.bat`** for the command line, e.g. `ptransfer.bat nokia --fix-bootloop`

### C. Manual, if you prefer to see each step

```powershell
git clone https://github.com/Cobwebbylight56/phone-data-transfer-.git
cd phone-data-transfer-
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[gui]"
ptransfer setup      # fetches adb + fastboot
ptransfer gui        # or: ptransfer devices
```

Python 3.10+. The core library has **no dependencies** — PySide6 is only needed for the
window, and every command works without it.

### Building the .exe yourself

```powershell
.\build_windows.ps1              # add -WithTools to bundle adb/fastboot inside
```

Output lands in `dist\PhoneDataTransfer\`.

### Checking it works

```powershell
ptransfer devices       # should list your phone, or explain why not
ptransfer guide nokia   # works with no phone attached
```

---

## Using it

### The window

1. **Phones** — plug the phone in, unlock it, allow the USB debugging prompt.
2. **Back up** — tick what you want, choose a folder, start. Leave it running; a full phone
   takes a while and it can be stopped and resumed.
3. **Restore** — plug the *new* phone in, point it at the bundle.
4. **Screen** — see the phone's display on the PC and drive it from there.
5. **Rescue** — for a phone that will not start.
6. **Brand guides** — button combinations and vendor tools per brand.

### Screen mirroring — the PC as the controller

Press **Start mirroring** on the Screen tab. That's it.

Mirroring runs on **scrcpy**, which captures the phone's display directly, so:

- **the lock screen shows** — you can see the keypad and type your PIN into it
- full frame rate and low latency, not a slideshow
- mouse and keyboard go straight through, including modifier keys

scrcpy ships inside the app, so there's nothing to install. (If a build ever lacks it, the
app downloads it the first time you press Start mirroring — no button to hunt for.) Its window
is embedded inside this app so you get one window, not two.

If scrcpy can't run at all, the app falls back to a screenshot loop and says so. That fallback
is slower and Android returns a **black rectangle for the lock screen**, because a screenshot
of a secure surface is blocked — the app explains that when it happens and lets you type the
PIN blind, which still works.

From the command line:

```powershell
ptransfer screen --mirror        # live mirror in its own window
ptransfer screen --shot s.png    # one screenshot
ptransfer screen --tap 540 1200
ptransfer screen --text "hello"
ptransfer screen --unlock        # prompts for the passcode, never echoes it
```

**The one hard requirement:** the phone must have USB debugging on and have trusted this PC.
That's an Android security control — it's what stops anyone with a cable reading a phone that
isn't theirs, and no tool bypasses it. `ptransfer authorize` gets you through it as fast as
possible (see below). If the screen is dead *and* debugging was never enabled, mirroring isn't
available to any software; a USB-C hub with a mouse plugged into the phone is the usual route.

### The recovery menu helper

Recovery is the one screen that can't be mirrored, so the app explains it instead. When a
phone in recovery is detected, the Rescue tab shows the annotated menu; from the command line:

```powershell
ptransfer rescue --menu
```

It covers the button navigation (Volume Up/Down to move, Power to select, and the
hold-Power-then-Volume-Up trick for the "No command" droid), the safe sequence to work
through, and what every option actually costs you.

**One correction to the guides you'll find elsewhere.** Nearly all of them end with "if the
cache wipe doesn't work, do a factory reset". That's right if you want a working phone and
don't need what's on it. It's the wrong move if you're trying to save your data: the reset
discards the encryption key, so afterwards nothing — no tool, no paid recovery service — can
get your photos or messages back. The app marks those entries in red and says so.

### The command line

```powershell
ptransfer devices                              # what's connected, including phones adb can't see
ptransfer info                                 # detail on the selected phone
ptransfer backup -o D:\Transfers               # full backup
ptransfer backup -o D:\T --sections media      # just the photos and files
ptransfer verify D:\T\Nokia-8.3-20260804.ptbundle
ptransfer restore D:\T\Nokia-8.3-20260804.ptbundle
ptransfer rescue                               # diagnose a phone that won't boot
ptransfer rescue --brand sony
ptransfer rescue --extract D:\Rescued          # try to save files from recovery mode
ptransfer rescue --sideload ota.zip            # data-safe system repair
ptransfer guide nokia
```

Everything the window does is available here, so it can be scripted.

---

## Turning on USB debugging

Let the app walk you through it:

```powershell
ptransfer authorize            # or: ptransfer debug
ptransfer authorize --check    # just tell me what's wrong, change nothing
```

In the window there's an **Ask the phone for permission** button on the Phones tab.

It first works out *which* of two look-alike problems you have, because they need
opposite advice:

- **Phone shows as `unauthorized`** — debugging is on, this PC just isn't trusted yet.
  The app restarts the adb daemon, which is what actually makes the phone ask again
  (a daemon that was already refused won't re-prompt), then waits for you to tap Allow
  and confirms when it worked.
- **adb sees nothing but Windows sees the phone** — debugging is off. No program on a PC
  can switch it on; it lives behind developer options by design. The app gives the exact
  menu path **for your brand** (Samsung's route goes via *Software information*, most
  others don't).

Manually, if you prefer:

Settings → About phone → tap **Build number** seven times → back → System → Developer
options → **USB debugging** on. Plug in, then tap **Allow** on the phone.

If nothing is detected: it is far more often the cable than the phone. Charge-only USB
cables look identical to data ones. Use a port on the back of the PC, not a hub.

---

## The bundle format

A folder, not a single archive — a 200 GB zip that fails at 99% is worthless.

```
Nokia-8.3-20260804-1420.ptbundle/
  manifest.json        what's in here, from which phone, when
  checksums.sha256     SHA-256 of every file
  media/               mirror of internal storage
  apps/<package>/      APKs, plus apps.json
  data/                contacts.vcf, sms.json, calls.json, settings.json, device.json
  logs/                transfer log, and a list of anything that could not be copied
```

Re-running a backup into the same folder skips what is already there, so an interrupted
transfer resumes. `ptransfer verify` re-hashes the lot.

---

## The companion app

Android only lets the *current default SMS app* write to the message store. That is a
platform rule, so restoring texts from a PC is impossible without an app on the phone.
`companion/` holds an Android app that does the writing. It is optional: everything except
SMS and call-log restore works without it.

---

## What this app will not do

- Bypass a lock screen, PIN, or factory reset protection.
- Unlock a bootloader.
- Flash `userdata`, `metadata` or `persist`.
- Drive leaked service tools, or link you to them.

Those are the things that either destroy the data you came here for, or exist to get into
a phone that is not yours.

---

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

The test suite runs against a simulated phone (`tests/fake_phone.py`) that implements enough
of adb's behaviour — storage tree, packages, content providers, and real file writes on
`pull` — to exercise backup, restore and rescue end to end without hardware.

## Licence

MIT.
