"""Read Metal's device identity and memory architecture without PyObjC."""

from __future__ import annotations

import ctypes as ct
import sys

from gputop.collectors.hardware import HardwareAdapter


def adapters() -> list[HardwareAdapter]:
    if sys.platform != "darwin":
        return []
    try:
        metal = ct.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
        objc = ct.CDLL("/usr/lib/libobjc.A.dylib")
        objc.sel_registerName.argtypes = [ct.c_char_p]
        objc.sel_registerName.restype = ct.c_void_p
        objc.objc_getClass.argtypes = [ct.c_char_p]
        objc.objc_getClass.restype = ct.c_void_p
        metal.MTLCopyAllDevices.argtypes = []
        metal.MTLCopyAllDevices.restype = ct.c_void_p
    except (OSError, AttributeError):
        return []

    def method(result, *arguments):
        return ct.CFUNCTYPE(result, ct.c_void_p, ct.c_void_p, *arguments)(
            ("objc_msgSend", objc)
        )

    pointer = method(ct.c_void_p)
    unsigned = method(ct.c_uint64)
    boolean = method(ct.c_bool)
    string = method(ct.c_char_p)
    indexed = method(ct.c_void_p, ct.c_ulong)
    responds = method(ct.c_bool, ct.c_void_p)
    release = method(None)
    selector = objc.sel_registerName
    pool_class = objc.objc_getClass(b"NSAutoreleasePool")
    pool = pointer(pointer(pool_class, selector(b"alloc")), selector(b"init"))
    devices = None
    result: list[HardwareAdapter] = []
    try:
        devices = metal.MTLCopyAllDevices()
        if not devices:
            return []
        for index in range(unsigned(devices, selector(b"count"))):
            device = indexed(devices, selector(b"objectAtIndex:"), index)
            if not device or not responds(
                device, selector(b"respondsToSelector:"), selector(b"registryID")
            ):
                continue
            registry_id = unsigned(device, selector(b"registryID"))
            name_object = pointer(device, selector(b"name"))
            name = string(name_object, selector(b"UTF8String")) or b""
            kind = "unknown"
            if responds(
                device, selector(b"respondsToSelector:"), selector(b"hasUnifiedMemory")
            ):
                kind = (
                    "integrated"
                    if boolean(device, selector(b"hasUnifiedMemory"))
                    else "dedicated"
                )
            result.append(
                HardwareAdapter(
                    hex(registry_id), name.decode("utf-8", errors="replace"), kind
                )
            )
        return result
    finally:
        if devices:
            release(devices, selector(b"release"))
        if pool:
            release(pool, selector(b"drain"))
