from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from gputop.collectors import metal
from gputop.collectors.common import clamp, command, integer, number
from gputop.collectors.hardware import HardwareAdapter, pci_vendor
from gputop.model import GPU, Process

INVENTORY_INTERVAL = 60.0
DISCOVERY_RETRY_INTERVAL = 10.0
PROCESS_INFO_INTERVAL = 15.0
POWER_INTERVAL = 5.0
INACTIVE_PROCESS_SECONDS = 60.0


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
    has_delta: bool = False


def _stats(text: str) -> dict[str, int | float]:
    match = re.search(r'"PerformanceStatistics"\s*=\s*\{([^}]+)\}', text)
    if not match:
        return {}
    result: dict[str, int | float] = {}
    for key, raw in re.findall(r'"([^"]+)"\s*=\s*([^,}]+)', match.group(1)):
        value = number(raw)
        if value is not None:
            whole = integer(raw)
            result[key] = whole if whole is not None else value
    return result


def _core_stats(text: str) -> dict[str, float]:
    """Read actual per-core percentages, including fractional driver readings.

    Some drivers publish these outside PerformanceStatistics, so search the
    accelerator's properties rather than only that dictionary. IOReportLegend
    channel names alone are not readings and have no numeric assignment.
    """
    result: dict[str, float] = {}
    pattern = r'"((?:GPU|Shader) Core \d+ Utilization %)"\s*=\s*"?([\d]+(?:\.[\d]+)?)"?'
    for name, value in re.findall(pattern, text):
        reading = clamp(number(value))
        if reading is not None:
            result[name] = reading
    return result


def _accelerators(text: str) -> list[tuple[str, str]]:
    starts = list(re.finditer(r"(?m)^\+-o\s+(\S+)\s+<class ", text))
    result = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        result.append((match.group(1), text[match.start() : end]))
    return result


def _agx_clients(text: str) -> list[AGXClient]:
    result: list[AGXClient] = []
    current: dict[str, int | str] | None = None

    def finish() -> None:
        if current is None or "pid" not in current:
            return
        if "gpu_time_ns" not in current:
            return
        result.append(
            AGXClient(
                str(current["registry_id"]),
                int(current["pid"]),
                str(current.get("name", f"pid {current['pid']}")),
                int(current.get("gpu_time_ns", 0)),
                int(current.get("last_submitted_ns", 0)),
                int(current.get("queues", 0)),
            )
        )

    for line in text.splitlines():
        start = re.search(
            r"\+-o\s+AGXDeviceUserClient\s+<class .*?\bid (0x[0-9a-fA-F]+)", line
        )
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
            current["gpu_time_ns"] = sum(
                map(int, re.findall(r'"accumulatedGPUTime"\s*=\s*(\d+)', line))
            )
            submitted = [
                int(value)
                for value in re.findall(r'"lastSubmittedTime"\s*=\s*(\d+)', line)
            ]
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
            result.append(
                Process(
                    int(pid),
                    name.strip(),
                    gpu_id,
                    "GPU time",
                    source="powermetrics",
                    gpu_time_ms_s=value,
                )
            )
    return result


def _vendor(class_name: str) -> str:
    name = class_name.lower()
    if name.startswith("agx") or "apple" in name:
        return "Apple"
    if "radeon" in name or "amd" in name:
        return "AMD"
    if "intel" in name:
        return "Intel"
    if "nvidia" in name or name.startswith("nvda"):
        return "NVIDIA"
    return "Unknown"


def _profiler_vendor(info: dict) -> str:
    value = str(info.get("spdisplays_vendor-id") or info.get("spdisplays_vendor") or "")
    match = re.search(r"0x[0-9a-fA-F]+", value)
    return pci_vendor(match.group()) if match else _vendor(value)


def _registry_id(segment: str) -> str | None:
    match = re.search(r"\bid (0x[0-9a-fA-F]+)", segment.splitlines()[0])
    return hex(int(match.group(1), 16)) if match else None


class MacCollector:
    def __init__(self) -> None:
        self.devices: list[dict] = []
        self.hardware: list[HardwareAdapter] = []
        self.last_discovery: float | None = None
        self.previous_clients: dict[str, tuple[int, int]] = {}
        self.last_sample_ns: int | None = None
        self.names: dict[int, str] = {}
        self.rss: dict[int, int] = {}
        self.last_names: float | None = None
        self.power: dict[str, float] = {}
        self.power_text = ""
        self.last_power: float | None = None

    def _refresh_devices(self) -> None:
        now = time.monotonic()
        interval = (
            INVENTORY_INTERVAL
            if self.devices or self.hardware
            else DISCOVERY_RETRY_INTERVAL
        )
        if self.last_discovery is not None and now - self.last_discovery < interval:
            return
        self.last_discovery = now
        self.hardware = metal.adapters()
        output = command(["system_profiler", "SPDisplaysDataType", "-json"], 5)
        if not output:
            return
        try:
            payload = json.loads(output)
            devices = (
                payload.get("SPDisplaysDataType") if isinstance(payload, dict) else None
            )
            if isinstance(devices, list):
                self.devices = [item for item in devices if isinstance(item, dict)]
        except (ValueError, TypeError):
            pass

    def _refresh_process_info(self) -> None:
        now = time.monotonic()
        if (
            self.last_names is not None
            and now - self.last_names < PROCESS_INFO_INTERVAL
        ):
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
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        if (
            self.last_power is not None
            and time.monotonic() - self.last_power < POWER_INTERVAL
        ):
            return
        output = command(
            [
                "powermetrics",
                "--samplers",
                "tasks,gpu_power",
                "--show-process-gpu",
                "-n",
                "1",
                "-i",
                "1000",
            ],
            3,
        )
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
                value = number(match.group(1))
                if value is not None:
                    self.power[key] = value

    def _process_samples(
        self,
        clients: list[AGXClient],
        gpu_id: str,
        now_ns: int,
        gpu_ids: dict[str, str] | None = None,
    ) -> list[Process]:
        elapsed_ns = (
            now_ns - self.last_sample_ns if self.last_sample_ns is not None else None
        )
        percent_per_ns = (
            100 / elapsed_ns if elapsed_ns is not None and elapsed_ns > 0 else None
        )
        current: dict[str, tuple[int, int]] = {}
        by_pid: dict[tuple[int, str], ProcessTotals] = {}
        for client in clients:
            if client.registry_id in current:
                continue
            current[client.registry_id] = (client.pid, client.gpu_time_ns)
            process_gpu = (
                gpu_ids.get(client.registry_id, gpu_id)
                if gpu_ids is not None
                else gpu_id
            )
            row = by_pid.setdefault(
                (client.pid, process_gpu), ProcessTotals(client.name)
            )
            row.gpu_time_ns += client.gpu_time_ns
            row.last_submitted_ns = max(row.last_submitted_ns, client.last_submitted_ns)
            row.queues += client.queues
            old = self.previous_clients.get(client.registry_id)
            if old and old[0] == client.pid and client.gpu_time_ns >= old[1]:
                row.delta_ns += client.gpu_time_ns - old[1]
                row.has_delta = True
        self.previous_clients = current
        self.last_sample_ns = now_ns
        result: list[Process] = []
        for (pid, process_gpu), row in by_pid.items():
            last_active = (
                max(0.0, (now_ns - row.last_submitted_ns) / 1e9)
                if row.last_submitted_ns
                else None
            )
            if (
                last_active is not None
                and last_active > INACTIVE_PROCESS_SECONDS
                and row.delta_ns == 0
            ):
                continue
            rate = (
                row.delta_ns * percent_per_ns
                if row.has_delta and percent_per_ns is not None
                else None
            )
            gpu_ms = rate * 10 if rate is not None else None
            percent = clamp(rate)
            result.append(
                Process(
                    pid,
                    self.names.get(pid, row.name),
                    process_gpu,
                    "Metal",
                    percent,
                    source="AGX AppUsage",
                    gpu_time_ms_s=gpu_ms,
                    gpu_time_total_ns=row.gpu_time_ns,
                    queue_count=row.queues,
                    last_active_seconds=last_active,
                    system_memory_used=self.rss.get(pid),
                )
            )
        return result

    def collect(self) -> tuple[list[GPU], list[Process]]:
        self._refresh_devices()
        output = command(["ioreg", "-r", "-l", "-w0", "-c", "IOAccelerator"], 2) or ""
        segments = _accelerators(output)
        if not segments:
            output = (
                command(["ioreg", "-r", "-l", "-w0", "-c", "AGXFamilyAccelerator"], 2)
                or ""
            )
            segments = _accelerators(output)
        sampled_ns = time.monotonic_ns()
        gpus: list[GPU] = []
        profiler_info: dict[str, dict] = {}
        remaining_names = Counter(device.name.casefold() for device in self.hardware)
        for hardware in self.hardware:
            matches = [
                info
                for info in self.devices
                if str(
                    info.get("spdisplays_model") or info.get("_name") or ""
                ).casefold()
                == hardware.name.casefold()
            ]
            info = (
                matches[0]
                if len(matches) == 1 and remaining_names[hardware.name.casefold()] == 1
                else {}
            )
            gpu = GPU(
                id=f"mac:{hardware.id}",
                name=hardware.name,
                vendor=_profiler_vendor(info),
                kind=hardware.kind,
                driver="Metal/IOKit",
                source="Metal + IOKit",
                core_count=integer(
                    info.get("sppci_cores") or info.get("spdisplays_cores")
                ),
            )
            gpu.extras["registry_id"] = hardware.id
            if hardware.kind != "unknown":
                gpu.extras["kind_source"] = "Metal.hasUnifiedMemory"
            gpus.append(gpu)
            profiler_info[gpu.id] = info
        for index, info in enumerate(self.devices):
            name = str(
                info.get("spdisplays_model") or info.get("_name") or f"GPU {index}"
            )
            if remaining_names[name.casefold()] > 0:
                remaining_names[name.casefold()] -= 1
                continue
            # Preserve devices that do not support Metal. System Profiler's
            # explicit shared-memory field distinguishes these from VRAM.
            kind = (
                "integrated"
                if info.get("spdisplays_vram_shared") is not None
                else (
                    "dedicated"
                    if info.get("spdisplays_vram") is not None
                    else "unknown"
                )
            )
            gpu = GPU(
                id=f"mac:{index}",
                name=name,
                vendor=_profiler_vendor(info),
                kind=kind,
                driver="IOKit",
                source="system_profiler + IOKit",
                core_count=integer(
                    info.get("sppci_cores") or info.get("spdisplays_cores")
                ),
            )
            if kind != "unknown":
                gpu.extras["kind_source"] = "system_profiler memory type"
            gpus.append(gpu)
            profiler_info[gpu.id] = info
        if not gpus:
            for index, (class_name, segment) in enumerate(segments):
                vendor = _vendor(class_name)
                registry_id = _registry_id(segment)
                gpus.append(
                    GPU(
                        id=f"mac:{registry_id or index}",
                        name=class_name,
                        vendor=vendor,
                        extras={"registry_id": registry_id} if registry_id else {},
                        driver="IOKit",
                        source="IOKit",
                    )
                )
        if not gpus:
            return [], []
        clients: list[AGXClient] = []
        client_gpus: dict[str, str] = {}
        matched: set[str] = set()
        vendor_counts = Counter(_vendor(name) for name, _ in segments)
        for class_name, segment in segments:
            vendor = _vendor(class_name)
            registry_id = _registry_id(segment)
            candidates = [
                gpu
                for gpu in gpus
                if registry_id is not None
                and gpu.extras.get("registry_id") == registry_id
            ]
            if not candidates and vendor_counts[vendor] == 1:
                candidates = [gpu for gpu in gpus if gpu.vendor == vendor]
            if len(candidates) != 1:
                if len(gpus) == 1 and len(segments) == 1:
                    candidates = gpus
                else:
                    gpu = GPU(
                        id=f"mac:{registry_id or class_name}",
                        name=class_name,
                        vendor=vendor,
                        driver="IOKit",
                        source="IOKit",
                        notes=[
                            "IOKit registry ID could not be matched to a named inventory entry."
                        ],
                    )
                    if registry_id:
                        gpu.extras["registry_id"] = registry_id
                    gpus.append(gpu)
                    candidates = [gpu]
            gpu = candidates[0]
            if registry_id and "registry_id" not in gpu.extras:
                info = profiler_info.pop(gpu.id, {})
                gpu.id = f"mac:{registry_id}"
                gpu.extras["registry_id"] = registry_id
                profiler_info[gpu.id] = info
            if gpu.id in matched:
                continue
            matched.add(gpu.id)
            if gpu.vendor == "Unknown":
                gpu.vendor = vendor
            stats = _stats(segment)
            gpu.utilization = clamp(number(stats.get("Device Utilization %")))
            if gpu.vendor == "Apple":
                gpu.engines = {
                    name: clamp(stats[key])
                    for name, key in (
                        ("renderer", "Renderer Utilization %"),
                        ("tiler", "Tiler Utilization %"),
                    )
                    if key in stats
                }
                gpu.memory_used = integer(stats.get("In use system memory"))
                count = re.search(r'"gpu-core-count"\s*=\s*(\d+)', segment)
                gpu.core_count = gpu.core_count or integer(
                    count.group(1) if count else None
                )
                gpu.core_utilization = _core_stats(segment)
                gpu.notes.append("Unified memory is shared with the CPU.")
                if gpu.core_count and not gpu.core_utilization:
                    gpu.notes.append(
                        "IOKit exposes GPU core count, but no physical per-core utilization counters."
                    )
                gpu_clients = _agx_clients(segment)
                clients.extend(gpu_clients)
                client_gpus.update(
                    (client.registry_id, gpu.id) for client in gpu_clients
                )
            else:
                gpu.memory_used = integer(stats.get("inUseVidMemoryBytes"))
                if gpu.memory_used is None:
                    gpu.memory_used = integer(stats.get("inUseSysMemoryBytes"))
                gpu.engines = {
                    name: clamp(value)
                    for name, value in stats.items()
                    if re.fullmatch(r"Device Unit \d+ Utilization %", name)
                }
                gpu.core_utilization = _core_stats(segment)
                info = profiler_info.get(gpu.id, {})
                vram = str(
                    info.get("spdisplays_vram")
                    or info.get("spdisplays_vram_shared")
                    or ""
                )
                capacity = re.search(r"([\d.]+)\s*(GB|MB)", vram, re.I)
                if capacity:
                    amount = number(capacity.group(1))
                    if amount is not None and amount >= 0:
                        gpu.memory_total = integer(
                            amount
                            * (
                                1024**3
                                if capacity.group(2).lower() == "gb"
                                else 1024**2
                            )
                        )
            if gpu.core_count and gpu.utilization is not None:
                gpu.core_equivalent_load = gpu.core_count * gpu.utilization / 100
            if "recoveryCount" in stats:
                gpu.extras["gpu_recoveries"] = stats["recoveryCount"]
            if "Alloc system memory" in stats:
                gpu.extras["driver_allocated_bytes"] = stats["Alloc system memory"]
            if "TiledSceneBytes" in stats:
                gpu.extras["tiled_scene_bytes"] = stats["TiledSceneBytes"]
        self._refresh_process_info()
        processes = self._process_samples(clients, gpus[0].id, sampled_ns, client_gpus)
        self._refresh_powermetrics()
        if self.power and sum(gpu.vendor == "Apple" for gpu in gpus) == 1:
            for gpu in gpus:
                if gpu.vendor == "Apple":
                    gpu.power_w = (
                        self.power.get("power_mw", 0) / 1000
                        if "power_mw" in self.power
                        else None
                    )
                    gpu.clock_mhz = self.power.get("clock_mhz")
                    gpu.temperature = self.power.get("temperature")
        if not processes and self.power_text and len(gpus) == 1:
            processes = _processes(self.power_text, gpus[0].id)
        if not clients and any(gpu.vendor == "Apple" for gpu in gpus):
            for gpu in gpus:
                if gpu.vendor == "Apple":
                    gpu.notes.append(
                        "This macOS driver did not expose AGX process counters."
                    )
        return gpus, processes
