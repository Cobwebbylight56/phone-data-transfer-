"""Vendor profile registry."""

from __future__ import annotations

from .base import DataSafety, KeyCombo, RescueStep, VendorProfile, VendorTool
from . import generic, nokia, sony

ALL_PROFILES: list[VendorProfile] = [nokia.PROFILE, sony.PROFILE, *generic.PROFILES]
FALLBACK = generic.FALLBACK


def profile_for(vendor_key: str) -> VendorProfile:
    """Resolve a brand string (ro.product.manufacturer, usually) to a profile."""
    v = (vendor_key or "").strip().lower()
    if not v:
        return FALLBACK
    for p in ALL_PROFILES:
        if p.matches(v):
            return p
    # Substring pass: "sony ericsson mobile communications ab" etc.
    for p in ALL_PROFILES:
        if p.key in v or any(a in v for a in p.aliases):
            return p
    return FALLBACK


def profile_for_usb_vendor(vid: str) -> VendorProfile | None:
    vid = (vid or "").lower()
    for p in ALL_PROFILES:
        if vid in p.usb_vendor_ids:
            return p
    return None


def known_brands() -> list[str]:
    return [p.display_name for p in ALL_PROFILES]


__all__ = [
    "ALL_PROFILES",
    "FALLBACK",
    "DataSafety",
    "KeyCombo",
    "RescueStep",
    "VendorProfile",
    "VendorTool",
    "profile_for",
    "profile_for_usb_vendor",
    "known_brands",
]
