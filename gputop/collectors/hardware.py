"""Hardware identity shared by native inventory readers."""

from __future__ import annotations

from dataclasses import dataclass
import re

# PCI vendor identifiers are ABI constants, independent of product names.
PCI_VENDORS = {0x1002: "AMD", 0x8086: "Intel", 0x10DE: "NVIDIA", 0x106B: "Apple"}


@dataclass(frozen=True)
class HardwareAdapter:
    id: str
    name: str
    kind: str = "unknown"
    vendor_id: int | None = None
    kind_source: str = ""


def pci_vendor(value: object) -> str:
    try:
        vendor_id = int(value, 16) if isinstance(value, str) else int(value)
    except (ValueError, TypeError, OverflowError):
        return "Unknown"
    return PCI_VENDORS.get(vendor_id, "Unknown")


def pci_key(value: str) -> str:
    match = re.fullmatch(
        r"([0-9a-f]{4,8}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])", value.lower()
    )
    if match:
        return ":".join(f"{int(part, 16):x}" for part in match.groups())
    return value.casefold()
