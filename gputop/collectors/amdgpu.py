"""Read AMD's integrated-device flag from the Linux DRM information ioctl."""

from __future__ import annotations

import ctypes as ct
import os
import sys
from pathlib import Path

# Definitions from Linux include/uapi/drm/amdgpu_drm.h. Request only the
# stable prefix through ids_flags; the kernel bounds its copy by return_size.
AMDGPU_INFO_DEV_INFO = 0x16
AMDGPU_IDS_FLAGS_FUSION = 0x01


class DeviceInfo(ct.Structure):
    _fields_ = [
        ("device_id", ct.c_uint32),
        ("chip_rev", ct.c_uint32),
        ("external_rev", ct.c_uint32),
        ("pci_rev", ct.c_uint32),
        ("family", ct.c_uint32),
        ("num_shader_engines", ct.c_uint32),
        ("num_shader_arrays_per_engine", ct.c_uint32),
        ("gpu_counter_freq", ct.c_uint32),
        ("max_engine_clock", ct.c_uint64),
        ("max_memory_clock", ct.c_uint64),
        ("cu_active_number", ct.c_uint32),
        ("cu_ao_mask", ct.c_uint32),
        ("cu_bitmap", ct.c_uint32 * 16),
        ("enabled_rb_pipes_mask", ct.c_uint32),
        ("num_rb_pipes", ct.c_uint32),
        ("num_hw_gfx_contexts", ct.c_uint32),
        ("pcie_gen", ct.c_uint32),
        ("ids_flags", ct.c_uint64),
    ]


class InfoRequest(ct.Structure):
    _fields_ = [
        ("return_pointer", ct.c_uint64),
        ("return_size", ct.c_uint32),
        ("query", ct.c_uint32),
        ("query_data", ct.c_uint32 * 4),
    ]


# _IOW('d', DRM_COMMAND_BASE + DRM_AMDGPU_INFO, struct drm_amdgpu_info).
DRM_IOCTL_AMDGPU_INFO = (
    (1 << 30) | (ct.sizeof(InfoRequest) << 16) | (ord("d") << 8) | 0x45
)


def device_kind(nodes: list[Path]) -> str:
    if not sys.platform.startswith("linux"):
        return "unknown"
    import fcntl

    for node in nodes:
        try:
            fd = os.open(node, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            info = DeviceInfo()
            request = InfoRequest(
                ct.addressof(info), ct.sizeof(info), AMDGPU_INFO_DEV_INFO
            )
            fcntl.ioctl(fd, DRM_IOCTL_AMDGPU_INFO, bytes(request))
            return (
                "integrated"
                if info.ids_flags & AMDGPU_IDS_FLAGS_FUSION
                else "dedicated"
            )
        except OSError:
            continue
        finally:
            os.close(fd)
    return "unknown"
