"""The stock Android recovery menu, annotated.

Recovery is the one screen this app cannot mirror - there is no Android display
service running behind it - so instead it tells you what you are looking at.

Every walkthrough on the internet ends the same way: wipe cache, and if that
does not work, factory reset. That final step is correct advice for getting a
*phone* working again, and it is exactly wrong when the point is to get the
*data* off it. A factory reset does not tidily erase your files; it discards
the encryption key, and every remaining byte becomes permanently meaningless.
There is no undelete afterwards, and no recovery service can help.

So the menu below is ordered the way the safe options actually run, and the
destructive ones carry the warning the guides leave out.
"""

from __future__ import annotations

from dataclasses import dataclass

from .oem import DataSafety


@dataclass(frozen=True)
class MenuEntry:
    label: str
    does: str
    safety: DataSafety
    advice: str = ""

    @property
    def destroys_data(self) -> bool:
        return self.safety is DataSafety.WIPES


NAVIGATION = (
    "Volume Up and Volume Down move the highlight up and down.",
    "The Power button selects the highlighted item.",
    "If you land on a dead Android with 'No command', hold Power and tap Volume Up once - "
    "that opens the real menu.",
    "Nothing is done until you press Power, so scrolling around is safe.",
)

MENU = (
    MenuEntry(
        "Reboot system now",
        "Restarts the phone normally.",
        DataSafety.SAFE,
        "Always worth trying first, and again after every other step.",
    ),
    MenuEntry(
        "Wipe cache partition",
        "Clears temporary system files. Your photos, messages and apps are not in the cache.",
        DataSafety.SAFE,
        "The standard first repair. It fixes a boot loop caused by a half-applied update and "
        "cannot cost you anything. Not present on newer phones, which have no cache partition - "
        "that is normal, not a fault.",
    ),
    MenuEntry(
        "Apply update from ADB",
        "Installs a signed update package sent from this PC.",
        DataSafety.SAFE,
        "This is the good one. A full update rebuilds the system and leaves your files alone. "
        "Select it, then run 'ptransfer rescue --sideload <file.zip>'. For a Nokia, run "
        "'ptransfer nokia --apply-ota' first - the phone may already be holding a signed package.",
    ),
    MenuEntry(
        "Apply update from SD card",
        "Installs a signed update package from the memory card.",
        DataSafety.SAFE,
        "Same as above if you have an SD card slot and the zip on the card.",
    ),
    MenuEntry(
        "Mount /system",
        "Makes the system partition writable from recovery.",
        DataSafety.SAFE,
        "Harmless on its own. Rarely needed.",
    ),
    MenuEntry(
        "View recovery logs",
        "Shows why the last boot or update failed.",
        DataSafety.SAFE,
        "Read-only, and the most useful thing in this menu. 'ptransfer nokia --logs' pulls the "
        "same file to the PC and explains what it means.",
    ),
    MenuEntry(
        "Run graphics test / Run locale test",
        "Hardware self-tests.",
        DataSafety.SAFE,
        "Diagnostic only. Changes nothing.",
    ),
    MenuEntry(
        "Power off",
        "Shuts the phone down.",
        DataSafety.SAFE,
    ),
    MenuEntry(
        "Wipe data/factory reset",
        "Erases everything: photos, videos, messages, contacts, apps and their data.",
        DataSafety.WIPES,
        "This is the one to avoid while you still want the data. Guides recommend it because it "
        "reliably produces a working phone - but it destroys the encryption key, so nothing is "
        "recoverable afterwards by any tool or service. Use it only once you have your data back, "
        "or once you have decided the phone matters more.",
    ),
    MenuEntry(
        "Format data / Format userdata",
        "The same erasure, done more thoroughly.",
        DataSafety.WIPES,
        "Identical consequence. Same answer.",
    ),
)

SAFE_ORDER = (
    "Reboot system now",
    "Wipe cache partition",
    "Reboot system now",
    "View recovery logs",
    "Apply update from ADB",
)


def safe_entries() -> list[MenuEntry]:
    return [e for e in MENU if not e.destroys_data]


def destructive_entries() -> list[MenuEntry]:
    return [e for e in MENU if e.destroys_data]


def entry(label: str) -> MenuEntry | None:
    want = label.strip().lower()
    for e in MENU:
        if e.label.lower() == want:
            return e
    for e in MENU:
        if want and want in e.label.lower():
            return e
    return None


def as_text() -> str:
    out = ["Android recovery menu", "=" * 21, ""]
    out.append("Getting around")
    out.append("-" * 14)
    for line in NAVIGATION:
        out.append(f"  - {line}")
    out.append("")

    out.append("Try these, in this order")
    out.append("-" * 24)
    for i, label in enumerate(SAFE_ORDER, 1):
        e = entry(label)
        out.append(f"  {i}. {label}")
        if e and e.advice:
            out.append(f"     {e.advice}")
    out.append("")

    out.append("Every option, and what it costs you")
    out.append("-" * 35)
    for e in MENU:
        tag = "ERASES EVERYTHING" if e.destroys_data else "safe"
        out.append(f"  [{tag}] {e.label}")
        out.append(f"      {e.does}")
        if e.advice:
            out.append(f"      {e.advice}")
    out.append("")
    out.append("If none of the safe options get it booting, stop and read")
    out.append("'ptransfer guide nokia' before choosing a wipe - a phone that")
    out.append("reaches recovery is usually repairable without one.")
    return "\n".join(out)
