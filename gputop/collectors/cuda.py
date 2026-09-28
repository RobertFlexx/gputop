"""Optional NVIDIA GPU type inventory from the CUDA driver API."""

from __future__ import annotations

import ctypes as ct
import sys
import time

from gputop.collectors.hardware import pci_key

INVENTORY_INTERVAL = 60.0
CU_DEVICE_ATTRIBUTE_INTEGRATED = 18
LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
_cached_at = 0.0
_cached: dict[str, str] = {}


def _query() -> dict[str, str]:
    try:
        library = (
            ct.WinDLL("nvcuda.dll", winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
            if sys.platform == "win32"
            else ct.CDLL("libcuda.so.1")
        )
        library.cuInit.argtypes = [ct.c_uint]
        library.cuInit.restype = ct.c_int
        library.cuDeviceGetCount.argtypes = [ct.POINTER(ct.c_int)]
        library.cuDeviceGetCount.restype = ct.c_int
        library.cuDeviceGet.argtypes = [ct.POINTER(ct.c_int), ct.c_int]
        library.cuDeviceGet.restype = ct.c_int
        library.cuDeviceGetAttribute.argtypes = [
            ct.POINTER(ct.c_int), ct.c_int, ct.c_int
        ]
        library.cuDeviceGetAttribute.restype = ct.c_int
        library.cuDeviceGetPCIBusId.argtypes = [ct.c_void_p, ct.c_int, ct.c_int]
        library.cuDeviceGetPCIBusId.restype = ct.c_int
        if library.cuInit(0) != 0:
            return {}
        count = ct.c_int()
        if library.cuDeviceGetCount(ct.byref(count)) != 0:
            return {}
        result = {}
        for index in range(max(0, count.value)):
            device = ct.c_int()
            integrated = ct.c_int()
            bus_id = ct.create_string_buffer(32)
            if (
                library.cuDeviceGet(ct.byref(device), index) != 0
                or library.cuDeviceGetAttribute(
                    ct.byref(integrated), CU_DEVICE_ATTRIBUTE_INTEGRATED, device.value
                ) != 0
                or library.cuDeviceGetPCIBusId(bus_id, len(bus_id), device.value) != 0
            ):
                continue
            pci = bus_id.value.decode("ascii", errors="ignore").lower()
            if pci:
                result[pci_key(pci)] = "integrated" if integrated.value else "dedicated"
        return result
    except (OSError, AttributeError, ValueError):
        return {}


def kinds_by_pci() -> dict[str, str]:
    global _cached_at, _cached
    now = time.monotonic()
    interval = INVENTORY_INTERVAL if _cached else 5.0
    if _cached_at and now - _cached_at < interval:
        return _cached
    _cached = _query()
    _cached_at = now
    return _cached
