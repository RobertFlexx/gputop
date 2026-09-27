from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from gputop.collectors.common import clamp, command, integer, number
from gputop.model import GPU, Process


@dataclass(frozen=True)
class AGXClient:
    registry_id: str
    pid: int
    name: str
    gpu_time_ns: int
    last_submitted_ns: int
    queues: int


@dataclass
class ProcessTotals:
    name: str
    gpu_time_ns: int = 0
    delta_ns: int = 0
    last_submitted_ns: int = 0
    queues: int = 0


def _stats(text: str) -> dict[str, int]:
    match = re.search(r'"PerformanceStatistics"\s*=\s*\{([^}]+)\}', text)
    if not match:
        return {}
    return {key: int(value) for key, value in
            re.findall(r'"([^"]+)"\s*=\s*(\d+)', match.group(1))}


def _core_stats(text: str) -> dict[str, float]:
    """Read actual per-core percentages, including fractional driver readings.

    Some drivers publish these outside PerformanceStatistics, so search the
    accelerator's properties rather than only that dictionary. IOReportLegend
    channel names alone are not readings and have no numeric assignment.
    """
    result: dict[str, float] = {}
    pattern = r'"((?:GPU|Shader) Core \d+ Utilization %)"\s*=\s*"?([\d]+(?:\.[\d]+)?)"?'
    for name, value in re.findall(pattern, text):
        result[name] = clamp(float(value)) or 0.0
    return result


def _accelerators(text: str) -> list[tuple[str, str]]:
    starts = list(re.finditer(r"(?m)^\+-o\s+(\S+)\s+<class ", text))
    result = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        result.append((match.group(1), text[match.start():end]))
    return result


def _agx_clients(text: str) -> list[AGXClient]:
    result: list[AGXClient] = []
    current: dict[str, int | str] | None = None

    def finish() -> None:
        if current is None or "pid" not in current:
            return
        if int(current.get("gpu_time_ns", 0)) <= 0:
            return
        result.append(AGXClient(str(current["registry_id"]), int(current["pid"]),
                                str(current.get("name", f"pid {current['pid']}")),
                                int(current.get("gpu_time_ns", 0)),
                                int(current.get("last_submitted_ns", 0)),
                                int(current.get("queues", 0))))

    for line in text.splitlines():
        start = re.search(r"\+-o\s+AGXDeviceUserClient\s+<class .*?\bid (0x[0-9a-fA-F]+)", line)
        if start:
            finish()
            current = {"registry_id": start.group(1)}
            continue
        if "+-o " in line:
            finish()
            current = None
            continue
        if current is None:
            continue
        creator = re.search(r'"IOUserClientCreator"\s*=\s*"pid (\d+),\s*(.*?)"', line)
        if creator:
            current["pid"] = int(creator.group(1))
            current["name"] = creator.group(2)
        if '"AppUsage"' in line:
            current["gpu_time_ns"] = sum(map(int, re.findall(r'"accumulatedGPUTime"\s*=\s*(\d+)', line)))
            submitted = [int(value) for value in re.findall(r'"lastSubmittedTime"\s*=\s*(\d+)', line)]
            current["last_submitted_ns"] = max(submitted, default=0)
        queues = re.search(r'"CommandQueueCount"\s*=\s*(\d+)', line)
        if queues:
            current["queues"] = int(queues.group(1))
    finish()
    return result


def _processes(text: str, gpu_id: str) -> list[Process]:
    """Fallback for systems that do not expose AppUsage."""
    result: list[Process] = []
    in_tasks = False
    for line in text.splitlines():
        if "*** Running tasks ***" in line:
            in_tasks = True
            continue
        if in_tasks and line.startswith("***"):
            break
        if not in_tasks or not line.startswith("  ") or "GPU ms/s" in line:
            continue
        match = re.match(r"^\s{2,}(.+?)\s{2,}(\d+)\s+.+?\s+(\d+(?:\.\d+)?)\s*$", line)
        if not match:
            continue
        name, pid, gpu_ms = match.groups()
        value = float(gpu_ms)
        if name and value > 0:
            result.append(Process(int(pid), name.strip(), gpu_id, "GPU time", source="powermetrics",
                                  gpu_time_ms_s=value))
    return result


def _vendor(class_name: str) -> str:
    name = class_name.lower()
    if name.startswith("agx") or "apple" in name:
        return "Apple"
    if "radeon" in name or "amd" in name:
        return "AMD"
    if "intel" in name:
        return "Intel"
    return "Unknown"


class MacCollector:
    def __init__(self) -> None:
        self.devices: list[dict] = []
        self.last_discovery = 0.0
        self.previous_clients: dict[str, tuple[int, int]] = {}
        self.last_sample_ns: int | None = None
        self.names: dict[int, str] = {}
        self.rss: dict[int, int] = {}
        self.last_names = 0.0
        self.power: dict[str, float] = {}
        self.power_text = ""
        self.last_power = 0.0

    def _refresh_devices(self) -> None:
        now = time.monotonic()
        if now - self.last_discovery < (60 if self.devices else 10):
            return
        self.last_discovery = now
        output = command(["system_profiler", "SPDisplaysDataType", "-json"], 5)
        if not output:
            return
        try:
            self.devices = json.loads(output).get("SPDisplaysDataType", [])
        except (ValueError, TypeError):
            pass

    def _refresh_process_info(self) -> None:
        now = time.monotonic()
        if now - self.last_names < 15:
            return
        self.last_names = now
        output = command(["ps", "-A", "-o", "pid=,comm=,rss="], 2)
        if output:
            names: dict[int, str] = {}
            rss: dict[int, int] = {}
            for line in output.splitlines():
                parts = line.strip().split()
                if len(parts) >= 3 and parts[0].isdigit() and parts[-1].isdigit():
                    pid = int(parts[0])
                    names[pid] = Path(" ".join(parts[1:-1])).name
                    rss[pid] = int(parts[-1]) * 1024
            self.names = names
            self.rss = rss

    def _refresh_powermetrics(self) -> None:
        if os.geteuid() != 0 or time.monotonic() - self.last_power < 5:
            return
        output = command(["powermetrics", "--samplers", "tasks,gpu_power", "--show-process-gpu",
                          "-n", "1", "-i", "1000"], 3)
        self.last_power = time.monotonic()
        self.power_text = output or ""
        patterns = {
            "power_mw": r"^GPU Power:\s*([\d.]+)\s*mW\b",
            "clock_mhz": r"^GPU HW active frequency:\s*([\d.]+)\s*MHz",
            "temperature": r"^GPU die temperature:\s*([\d.]+)\s*C",
        }
        self.power = {}
        for key, pattern in patterns.items():
            match = re.search(pattern, self.power_text, re.M | re.I)
            if match:
                self.power[key] = float(match.group(1))

    def _process_samples(self, clients: list[AGXClient], gpu_id: str, now_ns: int) -> list[Process]:
        elapsed_ns = now_ns - self.last_sample_ns if self.last_sample_ns is not None else None
        current: dict[str, tuple[int, int]] = {}
        by_pid: dict[int, ProcessTotals] = {}
        for client in clients:
            current[client.registry_id] = (client.pid, client.gpu_time_ns)
            row = by_pid.setdefault(client.pid, ProcessTotals(client.name))
            row.gpu_time_ns += client.gpu_time_ns
            row.last_submitted_ns = max(row.last_submitted_ns, client.last_submitted_ns)
            row.queues += client.queues
            old = self.previous_clients.get(client.registry_id)
            if old and old[0] == client.pid and client.gpu_time_ns >= old[1]:
                row.delta_ns += client.gpu_time_ns - old[1]
        self.previous_clients = current
        self.last_sample_ns = now_ns
        result: list[Process] = []
        for pid, row in by_pid.items():
            last_active = (max(0.0, (now_ns - row.last_submitted_ns) / 1e9)
                           if row.last_submitted_ns else None)
            if last_active is not None and last_active > 60 and row.delta_ns == 0:
                continue
            gpu_ms = row.delta_ns / elapsed_ns * 1000 if elapsed_ns and elapsed_ns > 0 else None
            percent = clamp(row.delta_ns / elapsed_ns * 100) if elapsed_ns and elapsed_ns > 0 else None
            result.append(Process(pid, self.names.get(pid, row.name), gpu_id, "Metal",
                                  percent, source="AGX AppUsage", gpu_time_ms_s=gpu_ms,
                                  gpu_time_total_ns=row.gpu_time_ns,
                                  queue_count=row.queues,
                                  last_active_seconds=last_active,
                                  system_memory_used=self.rss.get(pid)))
        return result

    def collect(self) -> tuple[list[GPU], list[Process]]:
        self._refresh_devices()
        output = command(["ioreg", "-r", "-l", "-w0", "-c", "IOAccelerator"], 2) or ""
        segments = _accelerators(output)
        if not segments:
            output = command(["ioreg", "-r", "-l", "-w0", "-c", "AGXFamilyAccelerator"], 2) or ""
            segments = _accelerators(output)
        gpus: list[GPU] = []
        for index, info in enumerate(self.devices):
            name = info.get("spdisplays_model") or info.get("_name") or f"GPU {index}"
            apple = "Apple" in name or "Apple" in info.get("spdisplays_vendor", "")
            vendor = "Apple" if apple else "AMD" if "AMD" in name or "Radeon" in name else "Intel" if "Intel" in name else "Unknown"
            kind = "integrated" if apple or vendor == "Intel" else "dedicated" if vendor == "AMD" else "unknown"
            gpus.append(GPU(id=f"mac:{index}", name=name, vendor=vendor, kind=kind,
                            driver="Metal/IOKit", source="system_profiler + IOKit",
                            core_count=integer(info.get("sppci_cores") or info.get("spdisplays_cores"))))
        if not gpus:
            for index, (class_name, _) in enumerate(segments):
                vendor = _vendor(class_name)
                gpus.append(GPU(id=f"mac:{index}", name=class_name, vendor=vendor,
                                kind="integrated" if vendor in {"Apple", "Intel"} else "unknown",
                                driver="IOKit", source="IOKit"))
        if not gpus:
            return [], []
        clients: list[AGXClient] = []
        for class_name, segment in segments:
            vendor = _vendor(class_name)
            candidates = [gpu for gpu in gpus if gpu.vendor == vendor]
            if len(candidates) != 1:
                if len(gpus) != 1:
                    continue
                candidates = gpus
            gpu = candidates[0]
            stats = _stats(segment)
            gpu.utilization = clamp(number(stats.get("Device Utilization %")))
            if gpu.vendor == "Apple":
                gpu.engines = {name: float(stats[key]) for name, key in
                               (("renderer", "Renderer Utilization %"), ("tiler", "Tiler Utilization %"))
                               if key in stats}
                gpu.memory_used = stats.get("In use system memory")
                count = re.search(r'"gpu-core-count"\s*=\s*(\d+)', segment)
                gpu.core_count = gpu.core_count or integer(count.group(1) if count else None)
                gpu.core_utilization = _core_stats(segment)
                gpu.notes.append("Unified memory is shared with the CPU.")
                if gpu.core_count and not gpu.core_utilization:
                    gpu.notes.append("IOKit exposes GPU core count, but no physical per-core utilization counters.")
                clients.extend(_agx_clients(segment))
            else:
                gpu.memory_used = stats.get("inUseVidMemoryBytes") or stats.get("inUseSysMemoryBytes")
                gpu.engines = {name: float(value) for name, value in stats.items()
                               if re.fullmatch(r"Device Unit \d+ Utilization %", name)}
                gpu.core_utilization = _core_stats(segment)
                gpu_index = int(gpu.id.split(":")[-1])
                vram = str(self.devices[gpu_index].get("spdisplays_vram") or "") if gpu_index < len(self.devices) else ""
                capacity = re.search(r"([\d.]+)\s*(GB|MB)", vram, re.I)
                if capacity:
                    gpu.memory_total = int(float(capacity.group(1)) *
                                           (1024**3 if capacity.group(2).lower() == "gb" else 1024**2))
            if gpu.core_count and gpu.utilization is not None:
                gpu.core_equivalent_load = gpu.core_count * gpu.utilization / 100
            if "recoveryCount" in stats:
                gpu.extras["gpu_recoveries"] = stats["recoveryCount"]
            if "Alloc system memory" in stats:
                gpu.extras["driver_allocated_bytes"] = stats["Alloc system memory"]
            if "TiledSceneBytes" in stats:
                gpu.extras["tiled_scene_bytes"] = stats["TiledSceneBytes"]
        self._refresh_process_info()
        now_ns = time.monotonic_ns()
        processes = self._process_samples(clients, next((gpu.id for gpu in gpus if gpu.vendor == "Apple"), gpus[0].id), now_ns)
        self._refresh_powermetrics()
        if self.power:
            for gpu in gpus:
                if gpu.vendor == "Apple":
                    gpu.power_w = self.power.get("power_mw", 0) / 1000 if "power_mw" in self.power else None
                    gpu.clock_mhz = self.power.get("clock_mhz")
                    gpu.temperature = self.power.get("temperature")
        if not processes and self.power_text:
            processes = _processes(self.power_text, gpus[0].id)
        if not clients and any(gpu.vendor == "Apple" for gpu in gpus):
            for gpu in gpus:
                if gpu.vendor == "Apple":
                    gpu.notes.append("This macOS driver did not expose AGX process counters.")
        return gpus, processes
