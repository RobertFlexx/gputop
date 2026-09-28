from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gputop.collectors import linux, macos, nvidia, windows
from gputop.collectors.hardware import HardwareAdapter
from gputop.collectors.manager import Collector, _merge_nvidia
from gputop.model import GPU, Process, Snapshot


class LinuxRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.drm = self.root / "drm"
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.collector = linux.LinuxCollector(self.drm, self.proc)

    def gpu(self, index: int, driver: str = "test") -> str:
        device = self.drm / f"card{index}" / "device"
        device.mkdir(parents=True)
        identity = f"0000:{index + 1:02x}:00.0"
        (device / "uevent").write_text(f"PCI_SLOT_NAME={identity}\n")
        (device / "vendor").write_text("0x1002")
        (device / "product_name").write_text("Identical GPU label")
        driver_path = self.root / "drivers" / driver
        driver_path.mkdir(parents=True, exist_ok=True)
        (device / "driver").symlink_to(driver_path)
        (device / "mem_info_vram_total").write_text(str(8 * 1024**3))
        node = self.drm / f"renderD{128 + index}"
        node.mkdir()
        (node / "device").symlink_to(device)
        return identity

    def client(
        self,
        pid: int,
        gpu: int,
        client: int,
        counter: int,
        capacity: int = 1,
        memory: str = "0 KiB",
        fd: int = 7,
    ) -> Path:
        proc = self.proc / str(pid)
        (proc / "fd").mkdir(parents=True, exist_ok=True)
        (proc / "fdinfo").mkdir(exist_ok=True)
        (proc / "comm").write_text("render-app")
        link = proc / "fd" / str(fd)
        if not link.is_symlink():
            link.symlink_to(f"/dev/dri/renderD{128 + gpu}")
        info = proc / "fdinfo" / str(fd)
        info.write_text(
            f"drm-client-id: {client}\ndrm-engine-render: {counter} ns\n"
            f"drm-engine-capacity-render: {capacity}\ndrm-resident-vram: {memory}\n"
        )
        return info

    def sample(self, now: float):
        with patch.object(linux.time, "monotonic", return_value=now):
            return self.collector.collect()

    def test_engine_capacity_shared_clients_and_multiple_gpus(self) -> None:
        first_id = self.gpu(0)
        second_id = self.gpu(1)
        for pid in (1, 2):
            self.client(pid, 0, 9, 100_000_000, capacity=2)
        self.client(1, 1, 9, 100_000_000, fd=8)
        self.sample(10)
        for pid in (1, 2):
            self.client(pid, 0, 9, 600_000_000, capacity=2)
        self.client(1, 1, 9, 300_000_000, fd=8)
        gpus, processes = self.sample(11)
        by_id = {gpu.id: gpu for gpu in gpus}
        self.assertEqual(by_id[first_id].utilization, 25)
        self.assertEqual(by_id[second_id].utilization, 20)
        self.assertEqual(set(by_id[first_id].engines), {"render"})
        self.assertEqual(len(processes), 3)
        self.assertEqual(
            {p.gpu_id for p in processes if p.pid == 1}, {first_id, second_id}
        )

    def test_decreasing_counter_does_not_count_work_twice(self) -> None:
        self.gpu(0)
        self.client(1, 0, 9, 100_000_000)
        self.sample(10)
        self.client(1, 0, 9, 50_000_000)
        _, processes = self.sample(11)
        self.assertIsNone(processes[0].utilization)
        self.client(1, 0, 9, 120_000_000)
        gpus, _ = self.sample(12)
        self.assertEqual(gpus[0].utilization, 2)

    def test_unknown_memory_is_not_zero_and_nonpositive_intervals_have_no_rate(
        self,
    ) -> None:
        self.gpu(0)
        self.client(1, 0, 9, 1, memory="unavailable")
        _, processes = self.sample(10)
        self.assertIsNone(processes[0].memory_used)
        self.client(1, 0, 9, 100, memory="0 KiB")
        _, processes = self.sample(10)
        self.assertIsNone(processes[0].utilization)
        self.assertEqual(processes[0].memory_used, 0)

    def test_amd_type_comes_from_driver_flags_and_inventory_is_cached(self) -> None:
        self.gpu(0, "amdgpu")
        self.gpu(1, "amdgpu")
        with patch.object(
            linux.amdgpu, "device_kind", side_effect=["integrated", "dedicated"]
        ) as query:
            gpus, _ = self.sample(10)
            self.sample(11)
        self.assertEqual([gpu.kind for gpu in gpus], ["integrated", "dedicated"])
        self.assertEqual(query.call_count, 2)
        self.assertEqual([gpu.vendor for gpu in gpus], ["AMD", "AMD"])

    def test_vram_size_and_driver_name_do_not_guess_gpu_type(self) -> None:
        self.gpu(0, "xe")
        gpus, _ = self.sample(10)
        self.assertEqual(gpus[0].kind, "unknown")

    def test_unreadable_proc_does_not_discard_inventory(self) -> None:
        self.gpu(0)
        with patch.object(Path, "iterdir", side_effect=PermissionError):
            gpus, processes = self.sample(10)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(processes, [])

    def test_partial_hwmon_nodes_do_not_overwrite_other_sensors(self) -> None:
        self.gpu(0)
        hwmon = self.drm / "card0" / "device" / "hwmon"
        (hwmon / "hwmon0").mkdir(parents=True)
        (hwmon / "hwmon1").mkdir()
        (hwmon / "hwmon0" / "temp1_input").write_text("45000")
        (hwmon / "hwmon1" / "power1_average").write_text("12000000")
        (hwmon / "hwmon1" / "pwm1").write_text("100")
        (hwmon / "hwmon1" / "pwm1_max").write_text("0")
        gpus, _ = self.sample(10)
        self.assertEqual(gpus[0].temperature, 45)
        self.assertEqual(gpus[0].power_w, 12)
        self.assertIsNone(gpus[0].fan_percent)


class MacRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collector = macos.MacCollector()
        self.collector.last_discovery = macos.time.monotonic()
        for name in ("_refresh_process_info", "_refresh_powermetrics"):
            patcher = patch.object(self.collector, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_new_reset_or_reused_clients_have_no_rate_until_next_sample(self) -> None:
        def client(pid, counter, identity="client"):
            return macos.AGXClient(identity, pid, "test", counter, 0, 1)

        self.collector._process_samples([], "gpu", 1_000_000_000)
        for now, value in enumerate((client(1, 100), client(1, 10), client(2, 20)), 2):
            rows = self.collector._process_samples([value], "gpu", now * 1_000_000_000)
            self.assertIsNone(rows[0].utilization)
            self.assertIsNone(rows[0].gpu_time_ms_s)
        rows = self.collector._process_samples([client(2, 20)], "gpu", 5_000_000_000)
        self.assertEqual(rows[0].utilization, 0)
        self.assertEqual(rows[0].gpu_time_ms_s, 0)

    def test_same_pid_on_two_gpus_has_separate_rates(self) -> None:
        def clients(counter):
            return [
                macos.AGXClient("a", 42, "app", counter, 0, 1),
                macos.AGXClient("b", 42, "app", counter * 2, 0, 1),
            ]

        mapping = {"a": "gpu:1", "b": "gpu:2"}
        self.collector._process_samples(
            clients(100_000_000), "unused", 1_000_000_000, mapping
        )
        rows = self.collector._process_samples(
            clients(200_000_000), "unused", 2_000_000_000, mapping
        )
        self.assertEqual(
            {p.gpu_id: p.utilization for p in rows}, {"gpu:1": 10, "gpu:2": 20}
        )

    def test_multiple_amd_macs_with_identical_names_match_registry_ids(self) -> None:
        self.collector.hardware = [
            HardwareAdapter("0x102", "Same name", "dedicated"),
            HardwareAdapter("0x101", "Same name", "integrated"),
        ]
        output = """+-o AMDRadeonAccelerator <class AMDRadeonAccelerator, id 0x101, active>
  | "PerformanceStatistics" = {"Device Utilization %"=20,"inUseVidMemoryBytes"=0,"inUseSysMemoryBytes"=999}
+-o AMDRadeonAccelerator <class AMDRadeonAccelerator, id 0x102, active>
  | "PerformanceStatistics" = {"Device Utilization %"=70}
"""
        with patch.object(macos, "command", return_value=output):
            gpus, _ = self.collector.collect()
        self.assertEqual(
            [(g.kind, g.utilization, g.vendor) for g in gpus],
            [("dedicated", 70, "AMD"), ("integrated", 20, "AMD")],
        )
        self.assertEqual(gpus[1].memory_used, 0)

    def test_profiler_explicit_memory_type_handles_amd_and_intel_without_name_rules(
        self,
    ) -> None:
        self.collector.devices = [
            {
                "_name": "Unrecognized A",
                "spdisplays_vendor": "0x1002",
                "spdisplays_vram": "8 GB",
            },
            {
                "_name": "Unrecognized B",
                "spdisplays_vendor": "0x8086",
                "spdisplays_vram_shared": "1536 MB",
            },
        ]
        with patch.object(macos, "command", return_value=""):
            gpus, _ = self.collector.collect()
        self.assertEqual(
            [(g.vendor, g.kind) for g in gpus],
            [("AMD", "dedicated"), ("Intel", "integrated")],
        )

    def test_builtin_apple_silicon_gpu_is_integrated_without_metal_inventory(
        self,
    ) -> None:
        self.collector.devices = [
            {
                "_name": "Apple M5",
                "spdisplays_vendor": "sppci_vendor_Apple",
                "sppci_bus": "spdisplays_builtin",
            }
        ]
        with (
            patch.object(macos.platform, "machine", return_value="arm64"),
            patch.object(macos, "command", return_value=""),
        ):
            gpus, _ = self.collector.collect()
        self.assertEqual(gpus[0].kind, "integrated")
        self.assertIn("Apple silicon", gpus[0].extras["kind_source"])

    def test_non_apple_builtin_gpu_still_needs_type_evidence(self) -> None:
        self.collector.devices = [
            {"_name": "Unrecognized", "sppci_bus": "spdisplays_builtin"}
        ]
        with (
            patch.object(macos.platform, "machine", return_value="arm64"),
            patch.object(macos, "command", return_value=""),
        ):
            gpus, _ = self.collector.collect()
        self.assertEqual(gpus[0].kind, "unknown")

    def test_ambiguous_same_vendor_statistics_are_not_assigned_to_first_gpu(
        self,
    ) -> None:
        self.collector.devices = [
            {"_name": "A", "spdisplays_vendor": "0x1002"},
            {"_name": "B", "spdisplays_vendor": "0x1002"},
        ]
        output = """+-o AMDRadeonAccelerator <class AMDRadeonAccelerator, id 0x101, active>
  | "PerformanceStatistics" = {"Device Utilization %"=70}
"""
        with patch.object(macos, "command", return_value=output):
            gpus, _ = self.collector.collect()
        self.assertEqual([g.utilization for g in gpus[:2]], [None, None])
        self.assertEqual(gpus[2].id, "mac:0x101")
        self.assertEqual(gpus[2].utilization, 70)

    def test_malformed_inventory_does_not_remove_last_valid_devices(self) -> None:
        for payload in (
            "[]",
            '{"SPDisplaysDataType": null}',
            '{"SPDisplaysDataType": 42}',
        ):
            self.collector.last_discovery = None
            self.collector.devices = [{"_name": "Previous"}]
            with (
                patch.object(macos.metal, "adapters", return_value=[]),
                patch.object(macos, "command", return_value=payload),
            ):
                self.collector._refresh_devices()
            self.assertEqual(self.collector.devices, [{"_name": "Previous"}])

    def test_legacy_gpu_is_retained_when_metal_only_reports_other_device(self) -> None:
        self.collector.hardware = [HardwareAdapter("0x101", "Modern", "integrated")]
        self.collector.devices = [
            {"_name": "Modern"},
            {"_name": "Legacy", "spdisplays_vram": "1 GB"},
        ]
        with patch.object(macos, "command", return_value=""):
            gpus, _ = self.collector.collect()
        self.assertEqual([gpu.name for gpu in gpus], ["Modern", "Legacy"])
        self.assertEqual([gpu.kind for gpu in gpus], ["integrated", "dedicated"])

    def test_fractional_stats_and_zero_time_clients_are_preserved(self) -> None:
        stats = macos._stats(
            '"PerformanceStatistics" = {"Device Utilization %"=12.5,"bad"=NaN}'
        )
        self.assertEqual(stats, {"Device Utilization %": 12.5})
        clients = macos._agx_clients(
            """+-o AGXDeviceUserClient <class AGXDeviceUserClient, id 0x101, active>
  | "IOUserClientCreator" = "pid 42, app"
  | "AppUsage" = ({"accumulatedGPUTime"=0,"lastSubmittedTime"=0})
"""
        )
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].gpu_time_ns, 0)


class WindowsRegressionTests(unittest.TestCase):
    @staticmethod
    def engine(pid=42, luid=1, engine=0, value=40):
        return {
            "Path": rf"\gpu engine(pid_{pid}_luid_0x00000000_0x{luid:08x}_phys_0_eng_{engine}_engtype_3d)\utilization percentage",
            "Value": value,
        }

    @staticmethod
    def memory(pid=42, luid=1, value=0):
        return {
            "Path": rf"\gpu process memory(pid_{pid}_luid_0x00000000_0x{luid:08x}_phys_0)\dedicated usage",
            "Value": value,
        }

    def test_same_name_amd_devices_map_by_luid_and_keep_distinct_types(self) -> None:
        hardware = [
            HardwareAdapter("0x00000000_0x00000002", "Same name", "dedicated", 0x1002),
            HardwareAdapter("0x00000000_0x00000001", "Same name", "integrated", 0x1002),
        ]
        gpus, processes = windows.parse(
            {"engine": [self.engine(luid=1, value=20), self.engine(luid=2, value=70)]},
            hardware,
        )
        self.assertEqual(
            [(g.kind, g.utilization, g.vendor) for g in gpus],
            [("dedicated", 70, "AMD"), ("integrated", 20, "AMD")],
        )
        self.assertEqual({p.gpu_id for p in processes}, {g.id for g in gpus})

    def test_same_type_engines_are_not_summed_as_one_engine(self) -> None:
        gpus, _ = windows.parse(
            {
                "engine": [
                    self.engine(engine=0, value=40),
                    self.engine(engine=1, value=70),
                ]
            }
        )
        self.assertEqual(gpus[0].utilization, 70)
        self.assertEqual(len(gpus[0].engines), 2)

    def test_memory_only_process_singleton_rows_and_bad_values(self) -> None:
        gpus, processes = windows.parse(
            {
                "adapters": {
                    "Name": "Unfamiliar name",
                    "PNPDeviceID": r"PCI\VEN_1002&DEV_9999",
                },
                "engine": [None, "bad", self.engine(value="1e999")],
                "memory": self.memory(value=0),
                "names": [
                    {"Id": "bad"},
                    {"Id": 42, "Name": "test", "WorkingSet": "NaN"},
                ],
            }
        )
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0].vendor, "AMD")
        self.assertEqual(gpus[0].kind, "unknown")
        self.assertEqual(processes[0].name, "test")
        self.assertEqual(processes[0].memory_used, 0)
        self.assertIsNone(processes[0].utilization)
        self.assertIsNone(processes[0].system_memory_used)

    def test_malformed_top_level_is_empty_and_unavailable_counters_keep_inventory(
        self,
    ) -> None:
        for value in (None, [], 42, "bad"):
            self.assertEqual(windows.parse(value), ([], []))
        hardware = [
            HardwareAdapter("0x00000000_0x00000001", "Test", "integrated", 0x1002)
        ]
        with patch.object(windows, "command", return_value="not JSON"):
            gpus, _ = windows.collect(hardware)
        self.assertEqual(gpus[0].kind, "integrated")

    def test_two_unmapped_adapters_do_not_steal_each_others_counters(self) -> None:
        gpus, processes = windows.parse(
            {
                "adapters": [{"Name": "A"}, {"Name": "B"}],
                "engine": [self.engine(luid=1), self.engine(luid=2)],
            }
        )
        self.assertEqual(len(gpus), 4)
        self.assertEqual([g.utilization for g in gpus[:2]], [None, None])
        self.assertTrue(all(p.gpu_id.startswith("luid:") for p in processes))

    def test_legacy_gpu_is_retained_when_dxcore_only_reports_other_device(self) -> None:
        hardware = [
            HardwareAdapter("0x00000000_0x00000001", "Modern", "integrated", 0x1002)
        ]
        gpus, _ = windows.parse(
            {"adapters": [{"Name": "Modern"}, {"Name": "Legacy"}]}, hardware
        )
        self.assertEqual([gpu.name for gpu in gpus], ["Modern", "Legacy"])

    def test_inventory_is_cached_without_caching_live_counters(self) -> None:
        collector = windows.WindowsCollector()
        with (
            patch.object(windows.dxcore, "adapters", return_value=[]) as discover,
            patch.object(windows.time, "monotonic", side_effect=[10, 11]),
            patch.object(
                windows, "command", side_effect=['[{"Name":"Test"}]', "{}", "{}"]
            ) as command,
        ):
            first, _ = collector.collect()
            second, _ = collector.collect()
        self.assertEqual([gpu.name for gpu in first], ["Test"])
        self.assertEqual([gpu.name for gpu in second], ["Test"])
        discover.assert_called_once()
        self.assertEqual(command.call_count, 3)
        self.assertNotIn("Get-CimInstance", command.call_args_list[1].args[0][-1])
        self.assertNotIn("Get-CimInstance", command.call_args_list[2].args[0][-1])


class MergeRegressionTests(unittest.TestCase):
    def test_cuda_type_fills_unknown_without_overriding_native_type(self) -> None:
        snapshot = Snapshot(
            [
                GPU("0000:01:00.0", "A", "NVIDIA"),
                GPU(
                    "0000:02:00.0",
                    "B",
                    "NVIDIA",
                    kind="integrated",
                    extras={"kind_source": "DXCore.IsIntegrated"},
                ),
            ]
        )
        incoming = [
            GPU(
                "00000000:01:00.0",
                "A",
                "NVIDIA",
                kind="dedicated",
                extras={"kind_source": "CUDA CU_DEVICE_ATTRIBUTE_INTEGRATED"},
            ),
            GPU(
                "00000000:02:00.0",
                "B",
                "NVIDIA",
                kind="dedicated",
                extras={"kind_source": "CUDA CU_DEVICE_ATTRIBUTE_INTEGRATED"},
            ),
        ]
        _merge_nvidia(snapshot, incoming, [], "Linux")
        self.assertEqual(
            [gpu.kind for gpu in snapshot.gpus], ["dedicated", "integrated"]
        )
        self.assertIn("CUDA", snapshot.gpus[0].extras["kind_source"])
        self.assertEqual(snapshot.gpus[1].extras["kind_source"], "DXCore.IsIntegrated")

    def test_same_name_pci_devices_keep_separate_readings_when_order_changes(
        self,
    ) -> None:
        snapshot = Snapshot(
            [
                GPU("0000:01:00.0", "Same", "NVIDIA"),
                GPU("0000:02:00.0", "Same", "NVIDIA"),
            ]
        )
        incoming = [
            GPU("00000000:02:00.0", "Same", "NVIDIA", utilization=70),
            GPU("00000000:01:00.0", "Same", "NVIDIA", utilization=20),
        ]
        processes = [Process(42, "app", gpu.id) for gpu in incoming]
        _merge_nvidia(snapshot, incoming, processes, "Linux")
        self.assertEqual([gpu.utilization for gpu in snapshot.gpus], [20, 70])
        self.assertEqual(
            {process.gpu_id for process in snapshot.processes},
            {gpu.id for gpu in snapshot.gpus},
        )

    def test_zero_memory_replaces_old_reading_and_process_rows_are_deduplicated(
        self,
    ) -> None:
        snapshot = Snapshot(
            [GPU("0000:01:00.0", "GPU", "NVIDIA", kind="dedicated", source="DRM")],
            [Process(42, "app", "0000:01:00.0", memory_used=1024)],
        )
        incoming = GPU("00000000:01:00.0", "GPU", "NVIDIA", source="nvidia-smi")
        processes = [
            Process(42, "app", incoming.id, memory_used=0),
            Process(43, "app", incoming.id),
            Process(43, "app", incoming.id),
        ]
        _merge_nvidia(snapshot, [incoming], processes, "Linux")
        self.assertEqual(len(snapshot.gpus), 1)
        self.assertEqual(snapshot.gpus[0].kind, "dedicated")
        self.assertEqual(len(snapshot.processes), 2)
        self.assertEqual(snapshot.processes[0].memory_used, 0)
        self.assertTrue(
            all(p.gpu_id == snapshot.gpus[0].id for p in snapshot.processes)
        )

    def test_duplicate_nvidia_names_are_ambiguous_in_both_directions(self) -> None:
        for native_count, nvidia_count in ((1, 2), (2, 1), (2, 2)):
            with self.subTest(native_count=native_count, nvidia_count=nvidia_count):
                snapshot = Snapshot(
                    [
                        GPU(f"win:{i}", "Same name", "NVIDIA")
                        for i in range(native_count)
                    ]
                )
                incoming = [
                    GPU(f"0000:0{i}:00.0", "Same name", "NVIDIA")
                    for i in range(nvidia_count)
                ]
                _merge_nvidia(snapshot, incoming, [], "Windows")
                self.assertEqual(len(snapshot.gpus), native_count + nvidia_count)

    def test_one_source_failure_keeps_the_other_source(self) -> None:
        collector = Collector(demo=True)
        collector.demo = False
        collector.native = type(
            "Native", (), {"collect": lambda self: ([GPU("gpu", "A", "AMD")], [])}
        )()
        with patch.object(nvidia, "collect", side_effect=OSError("unavailable")):
            snapshot = collector.collect()
        self.assertEqual(len(snapshot.gpus), 1)
        self.assertIn("NVIDIA collector failed", snapshot.warnings[0])

    def test_nvidia_invalid_rows_do_not_discard_other_gpus(self) -> None:
        gpu_csv = (
            "0, GPU-1, A, 00000000:01:00.0, 42, 12, 1e999, 8192, 63, 150, 250, 44, 1800\n"
            "1, GPU-2, B, 00000000:02:00.0, 20, 2, 0, 8192, 63, 150, 250, 44, 1800\n"
        )
        proc_csv = "GPU-1, 1.5, bad, 42\nGPU-2, 7, good, 0\n"
        with (
            patch.object(nvidia.shutil, "which", return_value="/bin/nvidia-smi"),
            patch.object(nvidia, "command", side_effect=[gpu_csv, proc_csv]),
        ):
            gpus, processes = nvidia.collect()
        self.assertEqual(len(gpus), 2)
        self.assertIsNone(gpus[0].memory_used)
        self.assertEqual(gpus[1].memory_used, 0)
        self.assertEqual([p.pid for p in processes], [7])
        self.assertEqual(processes[0].gpu_id, gpus[1].id)
