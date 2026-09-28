"""Optional Windows hardware inventory using the system DXCore API."""

from __future__ import annotations

import ctypes as ct
import sys
import uuid
from enum import IntEnum

from gputop.collectors.hardware import HardwareAdapter

LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
MAX_DESCRIPTION_BYTES = 64 * 1024


class Property(IntEnum):
    INSTANCE_LUID = 0
    DRIVER_DESCRIPTION = 2
    HARDWARE_ID = 3
    DEDICATED_ADAPTER_MEMORY = 7
    IS_HARDWARE = 11
    IS_INTEGRATED = 12


class Method(IntEnum):
    RELEASE = 2
    CREATE_ADAPTER_LIST = 3
    GET_ADAPTER = 3
    GET_ADAPTER_COUNT = 4
    GET_PROPERTY = 6
    GET_PROPERTY_SIZE = 7


class Luid(ct.Structure):
    _fields_ = [("low", ct.c_uint32), ("high", ct.c_uint32)]


class HardwareID(ct.Structure):
    _fields_ = [
        ("vendor", ct.c_uint32),
        ("device", ct.c_uint32),
        ("subsystem", ct.c_uint32),
        ("revision", ct.c_uint32),
    ]


def _guid(value: str):
    return (ct.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)


# Interface and attribute IDs from Microsoft's dxcore_interface.h.
IID_FACTORY = _guid("78ee5945-c36e-4b13-a669-005dd11c0f06")
IID_LIST = _guid("526c7776-40e9-459b-b711-f32ad76dfc28")
IID_ADAPTER = _guid("f0db4c7f-fe5a-42a2-bd62-f2a6cf6fc83e")
GRAPHICS_ATTRIBUTES = (
    _guid("8c47866b-7583-450d-f0f0-6bada895af4b"),  # D3D11 graphics
    _guid("0c9ece4d-2f6e-4f01-8c96-e89e331b47b1"),  # D3D12 graphics
)


def _method(pointer, slot: Method, result, *arguments):
    vtable = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    return ct.WINFUNCTYPE(result, ct.c_void_p, *arguments)(vtable[slot])


def _release(pointer) -> None:
    if pointer:
        _method(pointer, Method.RELEASE, ct.c_uint32)(pointer)


def _property(adapter, prop: Property, value) -> bool:
    call = _method(
        adapter, Method.GET_PROPERTY, ct.c_int32, ct.c_uint32, ct.c_size_t, ct.c_void_p
    )
    return call(adapter, prop, ct.sizeof(value), ct.byref(value)) >= 0


def _read_adapter(adapter) -> HardwareAdapter | None:
    hardware = ct.c_bool()
    luid = Luid()
    if not _property(adapter, Property.IS_HARDWARE, hardware) or not hardware.value:
        return None
    if not _property(adapter, Property.INSTANCE_LUID, luid):
        return None
    size = ct.c_size_t()
    get_size = _method(
        adapter, Method.GET_PROPERTY_SIZE, ct.c_int32, ct.c_uint32, ct.c_void_p
    )
    name = ""
    if (
        get_size(adapter, Property.DRIVER_DESCRIPTION, ct.byref(size)) >= 0
        and 0 < size.value <= MAX_DESCRIPTION_BYTES
    ):
        buffer = ct.create_string_buffer(size.value)
        if _property(adapter, Property.DRIVER_DESCRIPTION, buffer):
            name = buffer.value.decode("utf-8", errors="replace")
    integrated = ct.c_bool()
    kind = "unknown"
    kind_source = ""
    if _property(adapter, Property.IS_INTEGRATED, integrated):
        kind = "integrated" if integrated.value else "dedicated"
        kind_source = "DXCore.IsIntegrated"
    else:
        dedicated = ct.c_uint64()
        if (
            _property(adapter, Property.DEDICATED_ADAPTER_MEMORY, dedicated)
            and dedicated.value
        ):
            kind = "dedicated"
            kind_source = "DXCore.DedicatedAdapterMemory"
    hardware_id = HardwareID()
    vendor_id = (
        hardware_id.vendor
        if _property(adapter, Property.HARDWARE_ID, hardware_id)
        else None
    )
    identity = f"0x{luid.high:08x}_0x{luid.low:08x}"
    return HardwareAdapter(identity, name or f"GPU {identity}", kind, vendor_id, kind_source)


def adapters() -> list[HardwareAdapter]:
    if sys.platform != "win32":
        return []
    try:
        # LOAD_LIBRARY_SEARCH_SYSTEM32 prevents loading a DLL from the working directory.
        library = ct.WinDLL("dxcore.dll", winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
        create = library.DXCoreCreateAdapterFactory
        create.argtypes = [ct.c_void_p, ct.POINTER(ct.c_void_p)]
        create.restype = ct.c_int32
    except (OSError, AttributeError):
        return []
    factory = ct.c_void_p()
    if create(ct.byref(IID_FACTORY), ct.byref(factory)) < 0 or not factory:
        return []
    result: dict[str, HardwareAdapter] = {}
    try:
        for attribute in GRAPHICS_ATTRIBUTES:
            listing = ct.c_void_p()
            create_list = _method(
                factory,
                Method.CREATE_ADAPTER_LIST,
                ct.c_int32,
                ct.c_uint32,
                ct.c_void_p,
                ct.c_void_p,
                ct.c_void_p,
            )
            status = create_list(
                factory, 1, ct.byref(attribute), ct.byref(IID_LIST), ct.byref(listing)
            )
            if status < 0 or not listing:
                continue
            try:
                count = _method(listing, Method.GET_ADAPTER_COUNT, ct.c_uint32)(listing)
                for index in range(count):
                    adapter = ct.c_void_p()
                    get_adapter = _method(
                        listing,
                        Method.GET_ADAPTER,
                        ct.c_int32,
                        ct.c_uint32,
                        ct.c_void_p,
                        ct.c_void_p,
                    )
                    if (
                        get_adapter(
                            listing, index, ct.byref(IID_ADAPTER), ct.byref(adapter)
                        )
                        < 0
                        or not adapter
                    ):
                        continue
                    try:
                        info = _read_adapter(adapter)
                        if info is not None:
                            result[info.id] = info
                    finally:
                        _release(adapter)
            finally:
                _release(listing)
    finally:
        _release(factory)
    return list(result.values())
