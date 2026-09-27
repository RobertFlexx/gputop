"""Hardware identity shared by native inventory readers."""

from __future__ import annotations

from dataclasses import dataclass

# PCI vendor identifiers are ABI constants, independent of product names.
PCI_VENDORS = {0x1002: "AMD", 0x8086: "Intel", 0x10DE: "NVIDIA", 0x106B: "Apple"}


@dataclass(frozen=True)
class HardwareAdapter:
    id: str
    name: str
    kind: str = "unknown"
    vendor_id: int | None = None


def pci_vendor(value: object) -> str:
    try:
        vendor_id = int(value, 16) if isinstance(value, str) else int(value)
    except (ValueError, TypeError, OverflowError):
        return "Unknown"
    return PCI_VENDORS.get(vendor_id, "Unknown")
