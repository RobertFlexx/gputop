from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gputop.collectors import linux, macos, nvidia, windows
from gputop.collectors.manager import Collector
from gputop.model import GPU, Process


class NvidiaTests(unittest.TestCase):
    def test_gpu_and_compute_process(self) -> None:
        gpu_csv = "0, GPU-1, NVIDIA RTX, 00000000:01:00.0, 42, 12, 2048, 8192, 63, 150, 250, 44, 1800\n"
        proc_csv = "GPU-1, 1234, python, 1024\n"
        with patch.object(nvidia.shutil, "which", return_value="/bin/nvidia-smi"), \
             patch.object(nvidia, "command", side_effect=[gpu_csv, proc_csv]):
            gpus, processes = nvidia.collect()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0].utilization, 42)
        self.assertEqual(gpus[0].memory_used, 2048 * 1024**2)
        self.assertEqual(processes[0].pid, 1234)
        self.assertEqual(processes[0].memory_used, 1024 * 1024**2)


class LinuxTests(unittest.TestCase):
    def test_fdinfo_deltas_are_process_and_engine_utilization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            drm = root / "drm"
            proc = root / "proc"
            card = drm / "card0"
            device = card / "device"
            device.mkdir(parents=True)
            (device / "vendor").write_text("0x8086")
            (device / "device").write_text("0x1234")
            (device / "uevent").write_text("PCI_SLOT_NAME=0000:00:02.0\n")
            (device / "product_name").write_text("Intel test GPU")
            (drm / "renderD128").mkdir()
            (drm / "renderD128" / "device").symlink_to(device)
            process = proc / "123"
            (process / "fd").mkdir(parents=True)
            (process / "fdinfo").mkdir()
            (process / "comm").write_text("render-app\n")
            (process / "fd" / "7").symlink_to("/dev/dri/renderD128")
            fdinfo = process / "fdinfo" / "7"
            fdinfo.write_text("drm-client-id:\t9\ndrm-engine-render:\t100000000 ns\n"
                              "drm-memory-local:\t1024 KiB\n")
            collector = linux.LinuxCollector(drm, proc)
            with patch.object(linux.time, "monotonic", return_value=10.0):
                first_gpus, _ = collector.collect()
            fdinfo.write_text("drm-client-id:\t9\ndrm-engine-render:\t600000000 ns\n"
                              "drm-memory-local:\t1024 KiB\n")
            with patch.object(linux.time, "monotonic", return_value=11.0):
                gpus, processes = collector.collect()
            self.assertIsNone(first_gpus[0].utilization)
            self.assertEqual(gpus[0].utilization, 50.0)
            self.assertEqual(gpus[0].engines["render"], 50.0)
            self.assertEqual(processes[0].memory_used, 1024**2)
        self.assertEqual(processes[0].name, "render-app")

    def test_multiple_drm_clients_are_one_process_with_summed_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            device = root / "drm" / "card0" / "device"
            device.mkdir(parents=True)
            (device / "vendor").write_text("0x8086")
            (device / "uevent").write_text("PCI_SLOT_NAME=0000:00:02.0\n")
            node = root / "drm" / "renderD128"
            node.mkdir()
            (node / "device").symlink_to(device)
            proc = root / "proc" / "123"
            (proc / "fd").mkdir(parents=True)
            (proc / "fdinfo").mkdir()
            (proc / "comm").write_text("render-app\n")
            for fd, client in (("7", "9"), ("8", "10")):
                (proc / "fd" / fd).symlink_to("/dev/dri/renderD128")
                (proc / "fdinfo" / fd).write_text(
                    f"drm-client-id: {client}\ndrm-engine-render: 100000000 ns\n"
                    "drm-memory-local: 0 KiB\n")
            collector = linux.LinuxCollector(root / "drm", root / "proc")
            with patch.object(linux.time, "monotonic", return_value=10.0):
                collector.collect()
            for fd in ("7", "8"):
                (proc / "fdinfo" / fd).write_text(
                    "drm-client-id: " + ("9" if fd == "7" else "10") +
                    "\ndrm-engine-render: 300000000 ns\ndrm-memory-local: 0 KiB\n")
            with patch.object(linux.time, "monotonic", return_value=11.0):
                gpus, processes = collector.collect()
            self.assertEqual(len(processes), 1)
            self.assertEqual(processes[0].utilization, 40)
            self.assertEqual(processes[0].memory_used, 0)
            self.assertEqual(gpus[0].utilization, 40)


class MacTests(unittest.TestCase):
    def test_fractional_core_readings_outside_performance_statistics(self) -> None:
        collector = macos.MacCollector()
        collector.devices = [{"_name": "Apple M5", "sppci_cores": "8"}]
        collector.last_discovery = macos.time.monotonic()
        output = '''+-o AGXAcceleratorG17G  <class AGXAcceleratorG17G, id 0x100, active>
  | "PerformanceStatistics" = {"Device Utilization %"=50}
  | "GPU Core 0 Utilization %" = 12.5
  | "Shader Core 1 Utilization %" = "0"
  | "gpu-core-count" = 8
'''
        with patch.object(macos, "command", return_value=output), \
             patch.object(collector, "_refresh_process_info"):
            gpus, _ = collector.collect()
        self.assertEqual(gpus[0].core_utilization,
                         {"GPU Core 0 Utilization %": 12.5, "Shader Core 1 Utilization %": 0.0})

    def test_ioreg_stats_and_powermetrics_tasks(self) -> None:
        text = '  |   "PerformanceStatistics" = {"Device Utilization %"=32,"Renderer Utilization %"=30,"Tiler Utilization %"=12,"In use system memory"=4096}'
        self.assertEqual(macos._stats(text)["Device Utilization %"], 32)
        tasks = """*** Running tasks ***
Name   ID    CPU ms/s   GPU ms/s
  WindowServer                 141  103.03  58.19  112.36  43.15  80.53
*** GPU power ***
"""
        result = macos._processes(tasks, "mac:0")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].gpu_time_ms_s, 80.53)

    def test_agx_clients_and_counter_deltas(self) -> None:
        text1 = """+-o AGXAcceleratorG17G  <class AGXAcceleratorG17G, id 0x100, active>
  | "PerformanceStatistics" = {"Device Utilization %"=40}
  +-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x101, active>
  | {
  |   "AppUsage" = ({"API"="Metal","lastSubmittedTime"=1000000000,"accumulatedGPUTime"=200000000})
  |   "IOUserClientCreator" = "pid 42, render"
  |   "CommandQueueCount" = 2
  | }
  +-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x102, active>
  | {
  |   "AppUsage" = ({"API"="Metal","lastSubmittedTime"=1000000000,"accumulatedGPUTime"=100000000})
  |   "IOUserClientCreator" = "pid 42, render"
  |   "CommandQueueCount" = 1
  | }
"""
        text2 = text1.replace("200000000", "500000000").replace(
            '"accumulatedGPUTime"=100000000', '"accumulatedGPUTime"=300000000')
        collector = macos.MacCollector()
        first = macos._agx_clients(text1)
        second = macos._agx_clients(text2)
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0].pid, 42)
        collector._process_samples(first, "mac:0", 1_000_000_000)
        result = collector._process_samples(second, "mac:0", 2_000_000_000)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].utilization, 50)
        self.assertEqual(result[0].gpu_time_total_ns, 800_000_000)
        self.assertEqual(result[0].queue_count, 3)

    def test_single_intel_mac_uses_ioaccelerator_stats(self) -> None:
        collector = macos.MacCollector()
        collector.devices = [{"_name": "Intel Iris Graphics", "spdisplays_vram": "1536 MB"}]
        collector.last_discovery = macos.time.monotonic()
        output = """+-o IntelAccelerator  <class IntelAccelerator, id 0x100, active>
  | "PerformanceStatistics" = {"Device Utilization %"=37,"inUseSysMemoryBytes"=1048576,"Device Unit 0 Utilization %"=19}
"""
        with patch.object(macos, "command", return_value=output), \
             patch.object(collector, "_refresh_process_info"):
            gpus, processes = collector.collect()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0].utilization, 37)
        self.assertEqual(gpus[0].memory_used, 1048576)
        self.assertEqual(gpus[0].memory_total, 1536 * 1024**2)
        self.assertEqual(gpus[0].engines["Device Unit 0 Utilization %"], 19)
        self.assertEqual(processes, [])

    def test_intel_and_amd_macs_map_accelerators_by_vendor(self) -> None:
        collector = macos.MacCollector()
        collector.devices = [{"_name": "Intel Iris Graphics"}, {"_name": "AMD Radeon Pro"}]
        collector.last_discovery = macos.time.monotonic()
        output = """+-o AMDRadeonAccelerator  <class AMDRadeonAccelerator, id 0x100, active>
  | "PerformanceStatistics" = {"Device Utilization %"=72}
+-o IntelAccelerator  <class IntelAccelerator, id 0x101, active>
  | "PerformanceStatistics" = {"Device Utilization %"=18}
"""
        with patch.object(macos, "command", return_value=output), \
             patch.object(collector, "_refresh_process_info"):
            gpus, _ = collector.collect()
        self.assertEqual(gpus[0].vendor, "Intel")
        self.assertEqual(gpus[0].utilization, 18)
        self.assertEqual(gpus[1].vendor, "AMD")
        self.assertEqual(gpus[1].utilization, 72)

    def test_iokit_inventory_survives_system_profiler_failure(self) -> None:
        collector = macos.MacCollector()
        output = """+-o AGXAcceleratorG17G  <class AGXAcceleratorG17G, id 0x100, active>
  | "PerformanceStatistics" = {"Device Utilization %"=27}
  | "gpu-core-count" = 8
"""
        with patch.object(macos, "command", side_effect=[None, output, None]), \
             patch.object(collector, "_refresh_process_info"):
            gpus, _ = collector.collect()
        self.assertEqual(gpus[0].vendor, "Apple")
        self.assertEqual(gpus[0].utilization, 27)
        self.assertEqual(gpus[0].core_count, 8)
        self.assertEqual(gpus[0].core_utilization, {})
        self.assertIn("no physical per-core utilization counters", gpus[0].notes[-2])


class WindowsTests(unittest.TestCase):
    def test_wddm_engine_and_process_name(self) -> None:
        payload = {
            "adapters": [{"Name": "Intel UHD Graphics", "DriverVersion": "1.2"}],
            "engine": [{"Path": r"\gpu engine(pid_42_luid_0x00000000_0x00000001_phys_0_eng_0_engtype_3d)\utilization percentage", "Value": 47}],
            "memory": [{"Path": r"\gpu process memory(pid_42_luid_0x00000000_0x00000001_phys_0)\dedicated usage", "Value": 1048576}],
            "names": [{"Id": 42, "Name": "game.exe"}],
        }
        gpus, processes = windows.parse(payload)
        self.assertEqual(gpus[0].utilization, 47)
        self.assertEqual(processes[0].name, "game.exe")
        self.assertEqual(processes[0].memory_used, 1048576)


class ManagerTests(unittest.TestCase):
    def test_demo_snapshot_has_two_gpus_and_processes(self) -> None:
        snapshot = Collector(demo=True).collect()
        self.assertEqual(len(snapshot.gpus), 2)
        self.assertGreater(len(snapshot.processes), 0)

    def test_nvidia_merges_with_linux_pci_device(self) -> None:
        collector = Collector()
        collector.platform = "Linux"
        native_gpu = GPU("0000:01:00.0", "Unknown GPU", "NVIDIA", source="DRM")
        collector.native = type("Native", (), {"collect": lambda self: ([native_gpu], [])})()
        nv_gpu = GPU("00000000:01:00.0", "NVIDIA RTX", "NVIDIA", utilization=65, source="nvidia-smi")
        nv_proc = Process(7, "train", nv_gpu.id, "compute")
        with patch("gputop.collectors.manager.nvidia.collect", return_value=([nv_gpu], [nv_proc])):
            snapshot = collector.collect()
        self.assertEqual(len(snapshot.gpus), 1)
        self.assertEqual(snapshot.gpus[0].utilization, 65)
        self.assertEqual(snapshot.processes[0].gpu_id, native_gpu.id)


if __name__ == "__main__":
    unittest.main()
