"""Vendor profile model.

A profile answers three questions for one brand:

* how do I get this phone into a mode a PC can talk to (key combos)?
* which official tool repairs it, and does that tool keep my data?
* what is the safe order of steps when it will not boot?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DataSafety(str, Enum):
    SAFE = "safe"            # cannot lose user data
    USUALLY_SAFE = "usually" # keeps data in normal use, no guarantee
    WIPES = "wipes"          # destroys user data - always warn loudly


@dataclass(frozen=True)
class KeyCombo:
    mode: str
    steps: str
    note: str = ""


@dataclass(frozen=True)
class VendorTool:
    name: str
    url: str
    purpose: str
    data_safety: DataSafety
    note: str = ""


@dataclass(frozen=True)
class RescueStep:
    title: str
    detail: str
    data_safety: DataSafety = DataSafety.SAFE
    # Optional machine-executable action id handled by recovery.py
    action: str = ""
    # Device states this step is worth showing in, by State.value. Empty means
    # "always relevant". Without this a state-specific step - switching A/B
    # slots, which only works from fastboot - gets shown everywhere or nowhere.
    applies_to: tuple[str, ...] = ()

    @property
    def warns(self) -> bool:
        return self.data_safety is DataSafety.WIPES

    def relevant_in(self, state: str) -> bool:
        return not self.applies_to or state in self.applies_to


@dataclass
class VendorProfile:
    key: str
    display_name: str
    aliases: tuple[str, ...] = ()
    usb_vendor_ids: tuple[str, ...] = ()
    key_combos: tuple[KeyCombo, ...] = ()
    tools: tuple[VendorTool, ...] = ()
    rescue_steps: tuple[RescueStep, ...] = ()
    notes: tuple[str, ...] = ()
    quirks: dict[str, str] = field(default_factory=dict)

    def matches(self, vendor_key: str) -> bool:
        v = (vendor_key or "").lower()
        return v == self.key or v in self.aliases

    def combo(self, mode: str) -> KeyCombo | None:
        for c in self.key_combos:
            if c.mode == mode:
                return c
        return None
