# Rescuing a phone that will not boot

Read the first section before you do anything else. It decides what is worth trying.

## The one thing that determines everything

Since Android 10, every phone uses **file-based encryption**. The key that decrypts your
photos and messages is wrapped by your screen lock and unwrapped after Android boots and you
unlock once. A phone stuck at the logo has never unwrapped it.

So when a PC is connected to a phone that will not boot, it is looking at ciphertext. Not
"hard to read" — mathematically unreadable without the key.

This is why the order below is always the same:

> **repair the boot without wiping userdata → let Android start → back up immediately**

Anything that inverts that order — "just factory reset it and we'll recover the files after" —
destroys the data. A factory reset does not carefully erase your files; it throws away the
encryption key, which makes every remaining byte permanently meaningless.

**If someone offers to "unbrick" your phone, ask one question first: does userdata get
erased?** If the answer is yes, or vague, that service cannot get your data back. It can only
get you a working phone.

## Is it actually bricked?

Most phones people call bricked are not.

| What you see | What it usually is |
|---|---|
| Black screen, nothing at all | flat battery, or a charge-only cable |
| Logo, then reboot, forever | corrupted update — **very recoverable** |
| Logo and stays there | broken system partition — recoverable |
| Boots to recovery on its own | recoverable |
| Black screen but the PC makes the USB sound | soft-bricked, bootloader alive — recoverable |
| Nothing on any PC, any cable, no LED, no vibration | possibly hard-bricked or a hardware fault |

Only the last line is genuinely bad, and even then it is often a dead battery that needs
30 minutes on a wall charger before it shows any sign of life.

## Step 0: rule out the cable

More "dead phones" are dead cables than anything else. USB cables that only carry power look
identical to data cables and are extremely common — most cables bundled with accessories are
charge-only.

Use the cable that came with the phone, or one you have moved files with. Use a port on the
**back** of the PC, not a front panel or a hub — flash mode draws sustained current that
front panels often cannot hold.

## Step 1: find out what mode it is in

```
ptransfer rescue
```

This checks three layers, which is the point:

- **adb** — sees the phone if Android or recovery is running
- **fastboot** — sees it if the bootloader is running
- **raw USB VID/PID** — sees it even when neither of the above can

That third layer is what turns "nothing is detected" into a real answer. Modes it recognises:

| USB ID | Mode | What it means |
|---|---|---|
| `0fce:adde` | Sony flash mode (green LED) | very recoverable |
| `0fce:0dde` | Sony S1Boot fastboot (blue LED) | very recoverable |
| `05c6:9008` | Qualcomm EDL | alive, but needs signed vendor loaders |
| `0e8d:0003` / `0e8d:2000` | MediaTek BROM / preloader | alive at chip level |
| `04e8:685d` | Samsung download mode | very recoverable |
| `18d1:4ee0` | Android fastboot | very recoverable |

On Windows it also reads the driver error code, so "device not recognised" becomes
"code 28: no driver installed" — a different problem with a different fix.

## Step 2: the data-safe repairs, in order

**Force restart.** Power + Volume Up (Sony, Nokia) or Power + Volume Down, held 10–20 seconds.
Clears a hung kernel. Fixes a real share of "bricked" phones outright, costs nothing.

**Wipe cache partition.** From recovery. Does not touch your files. Fixes boot loops caused
by a failed update — the single most common cause.

**Sideload a full OTA.** From recovery, "Apply update from ADB":

```
ptransfer rescue --sideload ota.zip
```

A full OTA rebuilds the system partitions and leaves userdata alone. It is the best repair
there is. The catch is getting a signed zip — Google publishes them for Pixels, most other
vendors do not.

**Reflash system partitions only.** Every vendor tool has a mode that reinstalls the system
without erasing userdata. Find that mode. Use that mode.

## Step 3: try to read the storage anyway

```
ptransfer rescue --extract D:\Rescued
```

With the phone in recovery, this mounts `/data` and tries to copy `/data/media/0`.

Expect it to fail on a modern phone, and expect it to tell you *why*. If the file names come
back as long random strings, that is FBE and the answer is unambiguous: no PC tool can read
them, and one claiming to can is selling you something.

It sometimes works on: pre-Android-10 phones, phones with no screen lock set, and devices with
a custom recovery already installed. Worth the one attempt.

## Step 4: the moment it boots

Do not sign in. Do not let it update. Do not "just check something quickly".

```
ptransfer backup -o D:\Transfers
```

A phone that booted once may not boot twice.

---

# Nokia (HMD Global)

**Button combinations**

- Recovery: power off; hold **Volume Up**, then press and hold Power; release Power at the
  logo, keep Volume Up held. At the "No command" droid, hold Power and tap Volume Up once.
- Fastboot: power off; hold **Volume Down** while connecting USB.
- Force restart: **Power + Volume Up**, ~10 seconds.
- MediaTek BROM (MTK models): phone off and unplugged, hold **both** volume keys, connect USB.

**The awkward truth**

HMD's flashing tools (OST LA, NOST) are distributed to authorised service points only. The
copies circulating on forums are unlicensed and routinely repackaged with malware. This app
does not link to them or drive them, and neither should you.

HMD also does not publish signed OTA zips, so recovery sideload usually has nothing legitimate
to feed it.

**So for a Nokia:** the recovery-menu repairs are your data-safe options, and past that it is a
Nokia care point. Ask them explicitly for a software reflash that **preserves userdata** — the
default service procedure often wipes, and nobody will ask you first.

Nokia models split between MediaTek (`ro.board.platform` starts with `mt`) and Qualcomm
(`msm`/`sdm`/`sm`). It changes which low-level mode you will see, not what you should do.

Do not unlock the bootloader to "get in". On a locked Nokia that forces a wipe, and HMD
stopped issuing unlock codes for most models anyway.

---

# Sony Xperia

Sony is the best case of any brand, for three reasons: LED-signalled PC modes that work with
Android dead, published signed firmware, and — the important one — **an FTF can be flashed
with the userdata image removed**, repairing the system while leaving your files in place.

**Button combinations**

- Flash mode: power off; hold **Volume Down**, connect USB. **Green LED**, black screen.
- Fastboot: power off; hold **Volume Up**, connect USB. **Blue LED**.
- Force restart: **Power + Volume Up** until three vibrations. Older models have a recessed
  yellow button under the SIM flap.

**A correction to the folklore**

Xperia Companion's **Software repair erases personal data** — Sony's own flow says so. Its
**Software update** path, which works while the phone is still detected, does not. Try update
first. People lose data by clicking through to "repair" because a forum post called them the
same thing.

**The data-preserving repair**

1. **XperiFirm** — download the exact firmware for your model and market. Downloads come from
   Sony's own servers; nothing is written to the phone at this stage.
2. Unpack it, and **delete the userdata image from the folder**. This is the whole trick.
3. **Newflasher** — flash the rest, with the phone in flash mode (green LED).
4. Boot, then back up immediately.

Match the model number (e.g. `XQ-CT54`) and the market/customisation string XperiFirm shows.
The wrong market's firmware can leave the modem non-functional.

Do not unlock the bootloader on an Xperia you want data from: it wipes userdata and permanently
destroys the DRM keys behind the camera tuning. That damage is not reversible by relocking.

---

# Other brands

`ptransfer guide <brand>` covers Samsung, Xiaomi, Google, Motorola, OnePlus, OPPO/realme/vivo,
Huawei/Honor, ASUS, HTC, LG, TCL/Alcatel, ZTE and Fairphone.

Two that are worth knowing regardless of what you own:

- **Samsung**: flashing with Odin, use the **HOME_CSC** file, not the plain CSC. HOME_CSC keeps
  userdata; CSC wipes it. One letter, all your photos.
- **Google Pixel**: the only line where signed full OTA images are simply published. Recovery
  sideload of a full OTA is the cleanest data-preserving repair on any Android phone.

---

# What this app refuses to do, and why

- **Flash `userdata`, `metadata` or `persist`** — these are the partitions that destroy what
  you came here to save. `fastboot.flash()` raises rather than run it.
- **Unlock a bootloader** — on a locked phone this forces a factory reset. It is the most
  common way people permanently lose the data they were trying to rescue.
- **Bypass a lock screen or factory reset protection** — that is not a data transfer feature.

Stock signed firmware flashes fine on a locked bootloader. You do not need to unlock anything
to repair a phone.
