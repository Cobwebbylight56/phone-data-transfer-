"""Profiles for the remaining brands, plus the fallback used for unknown ones.

These are deliberately shorter than the Nokia and Sony profiles: enough to get
the phone into a mode the PC can see and to point at the vendor's own repair
tool, without pretending to model-specific knowledge we do not have.
"""

from __future__ import annotations

from .base import DataSafety, KeyCombo, RescueStep, VendorProfile, VendorTool

_COMMON_STEPS = (
    RescueStep(
        "Charge for 30 minutes on the original charger",
        "A deeply discharged battery is indistinguishable from a dead phone for the first "
        "several minutes of charging.",
    ),
    RescueStep(
        "Force restart",
        "Hold Power + Volume Down (or Power + Volume Up on some brands) for 10-20 seconds until "
        "the phone vibrates or the logo appears.",
        action="force-restart",
    ),
    RescueStep(
        "Scan to see which mode it is in",
        "Connect the phone and scan. adb, fastboot and the raw USB view together will tell you "
        "whether it is booting, in a bootloader, or in a chip-level rescue mode.",
        action="scan",
    ),
    RescueStep(
        "Boot to recovery and wipe cache",
        "Data-safe. Clears a corrupted update that is stopping the boot.",
        action="reboot-recovery",
    ),
    RescueStep(
        "Reflash stock firmware, skipping userdata",
        "Every vendor tool has a mode that reinstalls system partitions only. Use it. The one "
        "thing never to accept is a flow that erases userdata.",
        DataSafety.USUALLY_SAFE,
    ),
    RescueStep(
        "Back up the second it boots",
        "Do not use the phone, do not sign in, do not update. Connect and back up first.",
        action="backup-now",
    ),
)


def _profile(
    key: str,
    name: str,
    aliases: tuple[str, ...] = (),
    combos: tuple[KeyCombo, ...] = (),
    tools: tuple[VendorTool, ...] = (),
    notes: tuple[str, ...] = (),
    usb_vendor_ids: tuple[str, ...] = (),
) -> VendorProfile:
    return VendorProfile(
        key=key,
        display_name=name,
        aliases=aliases,
        usb_vendor_ids=usb_vendor_ids,
        key_combos=combos or (
            KeyCombo("recovery", "Power off, then hold Volume Up + Power until the recovery screen appears."),
            KeyCombo("fastboot", "Power off, then hold Volume Down + Power."),
            KeyCombo("force-restart", "Hold Power + Volume Down for 10-20 seconds."),
        ),
        tools=tools,
        rescue_steps=_COMMON_STEPS,
        notes=notes,
    )


PROFILES = [
    _profile(
        "samsung",
        "Samsung Galaxy",
        usb_vendor_ids=("04e8",),
        combos=(
            KeyCombo("recovery", "Power off. Hold Volume Up + Side/Power (older: + Bixby) until the recovery menu appears."),
            KeyCombo("download", "Power off. Hold Volume Up + Volume Down and connect USB - this is Odin/download mode."),
            KeyCombo("force-restart", "Hold Volume Down + Power for 10 seconds."),
        ),
        tools=(
            VendorTool(
                "Samsung Smart Switch - Emergency software recovery",
                "https://www.samsung.com/us/support/owners/app/smart-switch",
                "Reinstalls firmware on a phone stuck in download mode.",
                DataSafety.USUALLY_SAFE,
                "Needs the model and serial from the phone's label or box.",
            ),
        ),
        notes=(
            "Flashing with Odin: use the HOME_CSC file, not the plain CSC. HOME_CSC keeps userdata; "
            "CSC wipes it.",
        ),
    ),
    _profile(
        "xiaomi",
        "Xiaomi / Redmi / POCO",
        aliases=("redmi", "poco"),
        tools=(
            VendorTool(
                "Mi Flash / MiAssistant",
                "https://global.c.mi.com/",
                "Official firmware flashing for Xiaomi devices.",
                DataSafety.USUALLY_SAFE,
                "In MiFlash choose 'save user data'. 'clean all' and 'clean all and lock' both wipe.",
            ),
        ),
        notes=(
            "Recovery ROMs applied through MiAssistant keep data; fastboot ROMs depend on which "
            "flash script you pick.",
        ),
    ),
    _profile(
        "google",
        "Google Pixel",
        aliases=("pixel",),
        usb_vendor_ids=("18d1",),
        tools=(
            VendorTool(
                "Android Flash Tool",
                "https://flash.android.com/",
                "Browser-based official reflash for Pixels.",
                DataSafety.USUALLY_SAFE,
                "Untick 'Wipe device' and 'Lock bootloader' to keep your data.",
            ),
            VendorTool(
                "Full OTA image + recovery sideload",
                "https://developers.google.com/android/ota",
                "Reinstalls the system from stock recovery without unlocking anything.",
                DataSafety.SAFE,
                "This is the best data-preserving repair on any Android phone - Pixels are the only "
                "line where the signed OTA zips are simply published.",
            ),
        ),
    ),
    _profile(
        "motorola",
        "Motorola",
        aliases=("moto", "lenovo"),
        tools=(
            VendorTool(
                "Motorola Rescue and Smart Assistant (LMSA)",
                "https://en-us.support.motorola.com/app/rescue-and-smart-assistant",
                "Official rescue and firmware reinstall.",
                DataSafety.WIPES,
                "Motorola's rescue flow wipes. Back up first if the phone still boots at all.",
            ),
        ),
    ),
    _profile(
        "oneplus",
        "OnePlus",
        tools=(
            VendorTool(
                "OnePlus MSM Download Tool / official support",
                "https://www.oneplus.com/support",
                "Restores a hard-bricked OnePlus from EDL.",
                DataSafety.WIPES,
                "MSM always wipes. Local upgrade from the phone's own updater keeps data if the "
                "phone still boots.",
            ),
        ),
    ),
    _profile(
        "oppo",
        "OPPO / realme / vivo",
        aliases=("realme", "vivo", "iqoo"),
        tools=(
            VendorTool(
                "Vendor service centre",
                "https://support.oppo.com/",
                "These brands do not publish flashing tools to the public.",
                DataSafety.USUALLY_SAFE,
                "Ask for a software reflash that preserves data.",
            ),
        ),
    ),
    _profile(
        "huawei",
        "Huawei / Honor",
        aliases=("honor",),
        tools=(
            VendorTool(
                "HiSuite / eRecovery",
                "https://consumer.huawei.com/en/support/hisuite/",
                "eRecovery downloads and reinstalls firmware over Wi-Fi from the phone itself.",
                DataSafety.USUALLY_SAFE,
                "Power + Volume Up while plugged in gets you to eRecovery. 'Download latest version "
                "and recovery' keeps data more often than not.",
            ),
        ),
    ),
    _profile("asus", "ASUS", aliases=("zenfone", "rog")),
    _profile("htc", "HTC"),
    _profile("lg", "LG"),
    _profile("tcl", "TCL / Alcatel", aliases=("alcatel",)),
    _profile("zte", "ZTE", aliases=("nubia",)),
    _profile("fairphone", "Fairphone"),
]

FALLBACK = VendorProfile(
    key="generic",
    display_name="Android phone",
    key_combos=(
        KeyCombo(
            "recovery",
            "Power off. Hold Volume Up + Power. If that fails, try Volume Down + Power - the two "
            "cover nearly every Android phone made.",
        ),
        KeyCombo("fastboot", "Power off, hold Volume Down, then connect USB."),
        KeyCombo("force-restart", "Hold Power for 20 seconds, or Power + Volume Down."),
    ),
    tools=(
        VendorTool(
            "Your manufacturer's own repair tool",
            "",
            "Every vendor has one. It is always a better bet than a forum flasher.",
            DataSafety.USUALLY_SAFE,
        ),
    ),
    rescue_steps=_COMMON_STEPS,
    notes=(
        "Since Android 10, /data is encrypted per-user with a key derived from your screen lock. "
        "A phone that cannot boot Android cannot decrypt its own storage, so no PC tool can pull "
        "your files out of it. Repairing the boot is the only path to the data.",
    ),
)
