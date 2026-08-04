"""Sony Xperia.

Sony is the friendliest of the big brands for this job, because:

* the phone has two obvious, LED-signalled PC modes (flash = green,
  fastboot = blue) that work even when Android is dead;
* Sony publishes official firmware, and XperiFirm pulls it straight from
  Sony's own servers;
* an FTF can be flashed with the userdata partition left out, which repairs the
  system while leaving your files in place. That is the single most useful
  data-preserving repair available on any brand.

One correction to the folklore: Xperia Companion's *Software repair* erases
personal data - Sony says so in the flow. The *Software update* path, which
only works while the phone is still detected, keeps it. Try update first.
"""

from __future__ import annotations

from .base import DataSafety, KeyCombo, RescueStep, VendorProfile, VendorTool

PROFILE = VendorProfile(
    key="sony",
    display_name="Sony Xperia",
    aliases=("sony ericsson", "somc"),
    usb_vendor_ids=("0fce",),
    key_combos=(
        KeyCombo(
            "flash",
            "Power off completely. Hold Volume Down, then plug the USB cable in while still "
            "holding it. The notification LED turns green and the screen stays black.",
            "Green LED = flash mode. This is the mode Newflasher and Emma talk to.",
        ),
        KeyCombo(
            "fastboot",
            "Power off completely. Hold Volume Up, then plug the USB cable in while still holding it. "
            "The LED turns blue.",
            "Blue LED = fastboot. 'fastboot devices' will list the phone here.",
        ),
        KeyCombo(
            "force-restart",
            "Hold Power + Volume Up together until the phone vibrates three times, then let go.",
            "Older Xperias have a recessed yellow OFF button under the SIM flap instead.",
        ),
        KeyCombo(
            "recovery",
            "With the phone off, hold Volume Down + Power. If adb still works, 'adb reboot recovery' "
            "is more reliable.",
            "Stock recovery on Xperia is minimal; some models have no key combo for it at all.",
        ),
    ),
    tools=(
        VendorTool(
            "Xperia Companion - Software update",
            "https://www.sony.com/electronics/support/downloads/W0011263",
            "Reinstalls firmware on a phone Windows can still detect.",
            DataSafety.USUALLY_SAFE,
            "Use this before the repair option. It keeps your data.",
        ),
        VendorTool(
            "Xperia Companion - Software repair",
            "https://www.sony.com/electronics/support/downloads/W0011263",
            "Last-resort reflash for a phone that will not start.",
            DataSafety.WIPES,
            "Sony's own flow warns that personal data is erased. Only after you have exhausted "
            "the data-preserving options.",
        ),
        VendorTool(
            "XperiFirm",
            "https://xperifirm.com/",
            "Downloads official, signed Sony firmware (FTF) directly from Sony's servers.",
            DataSafety.SAFE,
            "Downloading firmware changes nothing on the phone.",
        ),
        VendorTool(
            "Newflasher",
            "https://github.com/munjeni/newflasher",
            "Flashes an unpacked Sony firmware in flash mode (green LED).",
            DataSafety.USUALLY_SAFE,
            "Delete the userdata/persist images from the firmware folder before flashing and your "
            "files survive. Leave them in and they do not.",
        ),
    ),
    rescue_steps=(
        RescueStep(
            "Force restart before anything else",
            "Power + Volume Up until three vibrations. Xperias hang on a black screen far more often "
            "than they truly brick.",
            action="force-restart",
        ),
        RescueStep(
            "Put it in flash mode and confirm it is alive",
            "Volume Down held while connecting USB. Green LED means the bootloader is fine and only "
            "the Android side is broken - that is a repairable soft-brick, not a dead phone.",
            action="scan",
        ),
        RescueStep(
            "Try Xperia Companion's Software *update* first",
            "If Companion detects the phone, the update path reinstalls the system and keeps your "
            "data. Do not click through to 'repair' yet.",
            DataSafety.USUALLY_SAFE,
        ),
        RescueStep(
            "Reflash the system with userdata excluded",
            "Fetch the exact firmware for your model and market with XperiFirm, unpack it, delete the "
            "userdata image from the folder, and flash the rest with Newflasher in flash mode. This "
            "rebuilds the system partitions and leaves your files untouched.",
            DataSafety.USUALLY_SAFE,
        ),
        RescueStep(
            "Boot it, then back up immediately",
            "The moment Android starts, connect and run a full backup here. Repair the phone properly "
            "afterwards, not before.",
            action="backup-now",
        ),
        RescueStep(
            "Only then: Software repair",
            "This wipes. It is the right answer when the phone matters more than the data, and the "
            "wrong answer any earlier.",
            DataSafety.WIPES,
        ),
    ),
    notes=(
        "Flashing the wrong market's firmware can leave the modem non-functional. Match the model "
        "number (e.g. XQ-CT54) and the customisation/market string XperiFirm shows.",
        "Do not unlock the bootloader on an Xperia you want data from: unlocking wipes userdata and "
        "permanently destroys the DRM keys behind the camera tuning.",
    ),
    quirks={
        "leds": "green = flash mode, blue = fastboot, red flashing = flat battery.",
        "usb-ids": "0fce:adde is flash mode, 0fce:0dde is S1Boot fastboot.",
    },
)
