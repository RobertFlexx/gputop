from __future__ import annotations

import ctypes as ct
import threading
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gputop.collectors import amdgpu, cuda, dxcore, metal
from gputop.collectors.hardware import HardwareAdapter
from gputop.model import GPU, Snapshot
from gputop.ui import Sampler


class AmdgpuApiTests(unittest.TestCase):
    def test_native_request_uses_bounded_buffer_and_decodes_apu_flag(self) -> None:
        for flags, expected in ((1, "integrated"), (2, "dedicated"), (3, "integrated")):
            closed = []

            def ioctl(fd, operation, request_bytes):
                request = amdgpu.InfoRequest.from_buffer_copy(request_bytes)
                self.assertEqual(request.query, amdgpu.AMDGPU_INFO_DEV_INFO)
                self.assertEqual(request.return_size, 144)
                self.assertEqual(amdgpu.DeviceInfo.ids_flags.offset, 136)
                amdgpu.DeviceInfo.from_address(request.return_pointer).ids_flags = flags

            fake_fcntl = SimpleNamespace(ioctl=ioctl)
            with (
                self.subTest(flags=flags),
                patch.object(amdgpu.sys, "platform", "linux"),
                patch.dict("sys.modules", {"fcntl": fake_fcntl}),
                patch.object(amdgpu.os, "open", return_value=7),
                patch.object(amdgpu.os, "close", side_effect=closed.append),
            ):
                self.assertEqual(
                    amdgpu.device_kind([Path("/dev/dri/renderD128")]), expected
                )
                self.assertEqual(closed, [7])

    def test_ioctl_failure_closes_fd_and_does_not_guess(self) -> None:
        fake_fcntl = SimpleNamespace(ioctl=Mock(side_effect=OSError("unsupported")))
        with (
            patch.object(amdgpu.sys, "platform", "linux"),
            patch.dict("sys.modules", {"fcntl": fake_fcntl}),
            patch.object(amdgpu.os, "open", return_value=7),
            patch.object(amdgpu.os, "close") as close,
        ):
            self.assertEqual(
                amdgpu.device_kind([Path("/dev/dri/renderD128")]), "unknown"
            )
        close.assert_called_once_with(7)

    def test_inaccessible_render_node_falls_back_to_card(self) -> None:
        def ioctl(fd, operation, request_bytes):
            request = amdgpu.InfoRequest.from_buffer_copy(request_bytes)
            amdgpu.DeviceInfo.from_address(request.return_pointer).ids_flags = 1

        with (
            patch.object(amdgpu.sys, "platform", "linux"),
            patch.dict("sys.modules", {"fcntl": SimpleNamespace(ioctl=ioctl)}),
            patch.object(
                amdgpu.os, "open", side_effect=[PermissionError(), 7]
            ) as opened,
            patch.object(amdgpu.os, "close"),
        ):
            kind = amdgpu.device_kind(
                [Path("/dev/dri/renderD128"), Path("/dev/dri/card0")]
            )
        self.assertEqual(kind, "integrated")
        self.assertEqual(opened.call_count, 2)


class MetalApiTests(unittest.TestCase):
    def test_multiple_devices_and_optional_unified_memory_property(self) -> None:
        released = []
        objc = SimpleNamespace(
            sel_registerName=Mock(side_effect=lambda name: name),
            objc_getClass=Mock(return_value=1),
        )
        library = SimpleNamespace(MTLCopyAllDevices=Mock(return_value=2))

        def send(receiver, selector, *args):
            if selector in (b"alloc", b"init"):
                return receiver
            if selector == b"count":
                return 3
            if selector == b"objectAtIndex:":
                return 10 + args[0]
            if selector == b"respondsToSelector:":
                return args[0] == b"registryID" or receiver != 12
            if selector == b"registryID":
                return receiver
            if selector == b"name":
                return receiver
            if selector == b"UTF8String":
                return b"Same name"
            if selector == b"hasUnifiedMemory":
                return receiver == 10
            if selector in (b"release", b"drain"):
                released.append((receiver, selector))
                return None
            self.fail(f"Unexpected selector: {selector!r}")

        with (
            patch.object(metal.sys, "platform", "darwin"),
            patch.object(metal.ct, "CDLL", side_effect=[library, objc]),
            patch.object(
                metal.ct, "CFUNCTYPE", side_effect=lambda *args: lambda symbol: send
            ),
        ):
            result = metal.adapters()
        self.assertEqual(
            [item.kind for item in result], ["integrated", "dedicated", "unknown"]
        )
        self.assertEqual(len({item.id for item in result}), 3)
        self.assertEqual(released, [(2, b"release"), (1, b"drain")])

    def test_missing_framework_is_optional(self) -> None:
        with (
            patch.object(metal.sys, "platform", "darwin"),
            patch.object(metal.ct, "CDLL", side_effect=OSError()),
        ):
            self.assertEqual(metal.adapters(), [])


class DxcoreApiTests(unittest.TestCase):
    def test_adapter_properties_use_luid_and_explicit_integrated_flag(self) -> None:
        for integrated, expected in (
            (True, "integrated"),
            (False, "dedicated"),
            (None, "unknown"),
        ):

            def prop(adapter, key, value):
                if key == dxcore.Property.IS_HARDWARE:
                    value.value = True
                elif key == dxcore.Property.INSTANCE_LUID:
                    value.low, value.high = 7, 3
                elif key == dxcore.Property.DRIVER_DESCRIPTION:
                    value.value = b"Arbitrary AMD name"
                elif key == dxcore.Property.IS_INTEGRATED:
                    if integrated is None:
                        return False
                    value.value = integrated
                elif key == dxcore.Property.HARDWARE_ID:
                    value.vendor = 0x1002
                return True

            def get_size(adapter, key, output):
                output._obj.value = 32
                return 0

            with (
                self.subTest(integrated=integrated),
                patch.object(dxcore, "_property", side_effect=prop),
                patch.object(dxcore, "_method", return_value=get_size),
            ):
                info = dxcore._read_adapter(ct.c_void_p(1))
            self.assertEqual(
                info,
                HardwareAdapter(
                    "0x00000003_0x00000007", "Arbitrary AMD name", expected, 0x1002,
                    "DXCore.IsIntegrated" if integrated is not None else "",
                ),
            )

    def test_dedicated_memory_fallback_when_integrated_flag_is_unavailable(self) -> None:
        def prop(adapter, key, value):
            if key == dxcore.Property.IS_HARDWARE:
                value.value = True
            elif key == dxcore.Property.INSTANCE_LUID:
                value.low, value.high = 7, 3
            elif key == dxcore.Property.IS_INTEGRATED:
                return False
            elif key == dxcore.Property.DEDICATED_ADAPTER_MEMORY:
                value.value = 24 * 1024**3
            return True

        with (
            patch.object(dxcore, "_property", side_effect=prop),
            patch.object(dxcore, "_method", return_value=lambda *args: -1),
        ):
            info = dxcore._read_adapter(ct.c_void_p(1))
        self.assertEqual(info.kind, "dedicated")
        self.assertEqual(info.kind_source, "DXCore.DedicatedAdapterMemory")


    def test_enumeration_deduplicates_apis_and_releases_every_interface(self) -> None:
        released = []

        def create(iid, output):
            output._obj.value = 1
            return 0

        library = SimpleNamespace(DXCoreCreateAdapterFactory=Mock(side_effect=create))

        def method(pointer, slot, *signature):
            handle = pointer.value
            if slot == dxcore.Method.RELEASE:
                return lambda pointer: released.append(pointer.value) or 0
            if handle == 1 and slot == dxcore.Method.CREATE_ADAPTER_LIST:

                def create_list(pointer, count, attribute, iid, output):
                    output._obj.value = 2
                    return 0

                return create_list
            if handle == 2 and slot == dxcore.Method.GET_ADAPTER_COUNT:
                return lambda pointer: 2
            if handle == 2 and slot == dxcore.Method.GET_ADAPTER:

                def get_adapter(pointer, index, iid, output):
                    output._obj.value = 10 + index
                    return 0

                return get_adapter
            self.fail(f"Unexpected COM method: {handle}, {slot}")

        def read_adapter(pointer):
            return HardwareAdapter(
                str(pointer.value), "Same name", "integrated", 0x1002
            )

        with (
            patch.object(dxcore.sys, "platform", "win32"),
            patch.object(dxcore.ct, "WinDLL", return_value=library, create=True),
            patch.object(dxcore, "_method", side_effect=method),
            patch.object(dxcore, "_read_adapter", side_effect=read_adapter),
        ):
            result = dxcore.adapters()
        self.assertEqual(len(result), 2)
        self.assertEqual(Counter(released), Counter({1: 1, 2: 2, 10: 2, 11: 2}))

    def test_missing_dxcore_is_optional(self) -> None:
        with (
            patch.object(dxcore.sys, "platform", "win32"),
            patch.object(dxcore.ct, "WinDLL", side_effect=OSError(), create=True),
        ):
            self.assertEqual(dxcore.adapters(), [])


class CudaApiTests(unittest.TestCase):
    def test_integrated_flag_is_matched_by_pci_identity(self) -> None:
        def _write_pci(output, device):
            value = f"0000:0{device + 1}:00.0".encode() + b"\0"
            ct.memmove(output, value, len(value))
            return 0

        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        library = SimpleNamespace(
            cuInit=FakeFunction(lambda flags: 0),
            cuDeviceGetCount=FakeFunction(
                lambda output: setattr(output._obj, "value", 2) or 0
            ),
            cuDeviceGet=FakeFunction(
                lambda output, index: setattr(output._obj, "value", index) or 0
            ),
            cuDeviceGetAttribute=FakeFunction(
                lambda output, attr, device: setattr(output._obj, "value", device) or 0
            ),
            cuDeviceGetPCIBusId=FakeFunction(
                lambda output, length, device: _write_pci(output, device)
            ),
        )
        with patch.object(cuda.ct, "CDLL", return_value=library):
            result = cuda._query()
        self.assertEqual(result, {"0:1:0:0": "dedicated", "0:2:0:0": "integrated"})


class SamplerTests(unittest.TestCase):
    def test_refresh_during_collection_is_not_lost_and_close_wakes_thread(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        refreshed = threading.Event()
        calls = []

        def collect():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            else:
                refreshed.set()
            return Snapshot([GPU("gpu", "Test", "AMD")])

        sampler = Sampler(SimpleNamespace(collect=collect), 60)
        sampler.start()
        try:
            self.assertTrue(entered.wait(2))
            sampler.refresh()
            release.set()
            self.assertTrue(refreshed.wait(2))
        finally:
            release.set()
            sampler.close()
        snapshot, revision = sampler.latest()
        self.assertEqual(revision, 2)
        self.assertEqual(snapshot.gpus[0].id, "gpu")
        self.assertFalse(sampler.thread.is_alive())

    def test_sampler_recovers_after_collector_failure(self) -> None:
        recovered = threading.Event()
        calls = []

        def collect():
            calls.append(1)
            if len(calls) == 1:
                sampler.refresh()
                raise OSError("source unavailable")
            recovered.set()
            return Snapshot([GPU("gpu", "Test", "AMD")])

        sampler = Sampler(SimpleNamespace(collect=collect), 60)
        sampler.start()
        try:
            self.assertTrue(recovered.wait(2))
        finally:
            sampler.close()
        self.assertEqual(sampler.latest()[1], 2)
        self.assertEqual(sampler.latest()[0].gpus[0].id, "gpu")

    def test_close_before_start_is_safe(self) -> None:
        Sampler(SimpleNamespace(), 1).close()
