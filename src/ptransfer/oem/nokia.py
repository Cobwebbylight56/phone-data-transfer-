"""Nokia (HMD Global) Android phones.

Facts that shape everything below:

* HMD's flashing tools (OST LA / NOST) are distributed to authorised service
  points only. Copies circulating on forums are unlicensed and routinely
  repackaged with malware, so this app does not link to or drive them.
* HMD does not publish signed OTA zips, so recovery's "apply update from ADB"
  has nothing legitimate to feed it in most cases.
* Nokia models split between MediaTek and Qualcomm silicon, which changes what
  the lowest-level rescue mode even is.

The practical consequence: for a Nokia the data-preserving moves are the
recovery-menu ones, and past that it is a Nokia care point.
"""

from __future__ import annotations

from .base import DataSafety, KeyCombo, RescueStep, VendorProfile, VendorTool

PROFILE = VendorProfile(
    key="nokia",
    display_name="Nokia (HMD Global)",
    aliases=("hmd", "hmd global", "nokia mobile"),
    usb_vendor_ids=("2e04", "0421", "0e8d", "05c6"),
    key_combos=(
        KeyCombo(
            "recovery",
            "Power the phone off fully. Hold Volume Up and keep holding it while you press "
            "and hold Power. Release Power when the logo appears, keep Volume Up held until "
            "the recovery screen shows.",
            "If you land on the 'No command' droid, press and hold Power then tap Volume Up "
            "once - that opens the real recovery menu.",
        ),
        KeyCombo(
            "fastboot",
            "Power off, then hold Volume Down while plugging the USB cable into the PC.",
            "Some models want Volume Down + Power instead. Both are safe to try.",
        ),
        KeyCombo(
            "force-restart",
            "Hold Power and Volume Up together for about 10 seconds until the phone vibrates.",
            "This is a hardware reset of the power rail, not a factory reset - your data is untouched.",
        ),
        KeyCombo(
            "brom",
            "MediaTek models only: with the phone off and unplugged, hold Volume Up + Volume Down "
            "and then connect USB. Windows shows 'MediaTek USB Port' for a few seconds.",
            "Chip-level mode. Nothing this app does uses it; it is listed so you can recognise it.",
        ),
    ),
    tools=(
        VendorTool(
            "Nokia / HMD support and care points",
            "https://www.nokia.com/phones/en_int/support",
            "The only sanctioned route to a firmware reflash for a Nokia Android phone.",
            DataSafety.USUALLY_SAFE,
            "Ask them explicitly for a software reflash that preserves userdata before you hand it over - "
            "the default service procedure often wipes.",
        ),
        VendorTool(
            "Android stock recovery (already on the phone)",
            "",
            "Wipe cache, reboot, and apply an update if you have a signed zip.",
            DataSafety.SAFE,
            "Everything except 'Wipe data/factory reset' in this menu is data-safe.",
        ),
    ),
    rescue_steps=(
        RescueStep(
            "Charge it for 30 minutes first",
            "A flat battery looks exactly like a dead phone: black screen, no charging LED for the "
            "first several minutes. Use the original charger and a known-good cable, and leave it "
            "alone for half an hour before concluding anything.",
        ),
        RescueStep(
            "Force restart",
            "Power + Volume Up held for ~10 seconds. This clears a hung kernel and fixes a "
            "surprising share of 'bricked' Nokias outright.",
            action="force-restart",
        ),
        RescueStep(
            "Run the guided Nokia rescue",
            "Connect the phone and run 'ptransfer nokia'. It reads the phone's own recovery log to "
            "find out what actually failed, checks the A/B system slots, and looks for an update "
            "package already on the phone. Every check is read-only.",
            action="nokia-guided",
        ),
        RescueStep(
            "Switch to the other system slot",
            "Nearly every Nokia since the Android One line carries two complete system slots. A "
            "failed update leaves the new one unbootable while the previous, working system sits "
            "untouched in the other. 'ptransfer nokia --switch-slot' swaps them - it writes "
            "nothing and does not touch your data. This is the most effective single fix for a "
            "Nokia that died during an update.",
            action="nokia-switch-slot",
            applies_to=("bootloader",),
        ),
        RescueStep(
            "Apply an update the phone already downloaded",
            "If an OTA finished downloading before the phone broke, the signed zip is still in "
            "/data/ota_package or /cache. HMD signed it, so recovery accepts it - and it is the "
            "one legitimate route to a signed Nokia OTA, since HMD does not publish them. "
            "'ptransfer nokia --apply-ota' finds and stages it.",
            action="nokia-apply-ota",
            applies_to=("recovery", "sideload", "online"),
        ),
        RescueStep(
            "Boot to recovery and wipe cache",
            "Volume Up + Power into recovery, then 'Wipe cache partition' (not factory reset). "
            "This clears a corrupt update cache without touching your files.",
            action="reboot-recovery",
        ),
        RescueStep(
            "The moment it boots, back it up",
            "Connect immediately and take a full backup before doing anything else. A phone that "
            "booted once may not boot twice.",
            action="backup-now",
        ),
        RescueStep(
            "If it still will not boot: Nokia care point",
            "HMD's service tool can reflash the system partition, and a service point can be asked "
            "to keep userdata. Third-party 'unlock/flash' shops usually start with a factory reset, "
            "which destroys exactly what you are trying to save - say no to that.",
            DataSafety.USUALLY_SAFE,
        ),
    ),
    notes=(
        "Nokia Android phones encrypt /data with a key tied to your screen lock. If Android itself "
        "never starts, no PC tool - including this one - can read your files off the phone. Repairing "
        "the boot is the only route to the data.",
        "Do not unlock the bootloader to 'get in'. On a locked Nokia, unlocking triggers a mandatory "
        "wipe, and HMD stopped issuing unlock codes for most models anyway.",
        "If the bootloader refuses to switch slots because it is locked, that is not a dead end: "
        "after several failed boot attempts it falls back to the other slot on its own. Let the "
        "phone try to boot a few more times before giving up on it.",
        "Stock recovery writes the reason a boot or update failed to last_log, and it is readable "
        "over adb from recovery. 'ptransfer nokia --logs' prints it. It is the most informative "
        "thing on a phone that will not start, and almost nobody looks at it.",
    ),
    quirks={
        "chipset-detect": "ro.board.platform starting with 'mt' means MediaTek, 'msm'/'sdm'/'sm' means Qualcomm, 'ums'/'sp9' means Unisoc.",
        "ota-sideload": "HMD does not publish signed OTA zips, but a package the phone downloaded itself is signed and still valid - look in /data/ota_package and /cache.",
        "ab-slots": "Android One and later Nokias are A/B; slot switching is data-safe and is the highest-value repair after a failed update.",
    },
)
