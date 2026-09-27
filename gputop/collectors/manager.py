from __future__ import annotations

import math
import platform
import time

from gputop.collectors import nvidia
from gputop.collectors.linux import LinuxCollector
from gputop.collectors.macos import MacCollector
from gputop.model import GPU, Process, Snapshot


def _pci_key(value: str) -> str:
    parts = value.lower().split(":")
    if len(parts) >= 3:
        return ":".join(parts[-3:]).lstrip("0")
    return value.lower()


class Collector:
    def __init__(self, demo: bool = False) -> None:
        self.demo = demo
        self.platform = platform.system()
        if self.platform == "Linux":

            self.native = LinuxCollector()
        elif self.platform == "Darwin":

            self.native = MacCollector()
        else:
            self.native = None

    def collect(self) -> Snapshot:
        if self.demo:
            return demo_snapshot()
        snapshot = Snapshot()
        try:
            if self.platform in {"Linux", "Darwin"} and self.native is not None:
                snapshot.gpus, snapshot.processes = self.native.collect()
            elif self.platform == "Windows":
                from gputop.collectors.windows import collect

                snapshot.gpus, snapshot.processes = collect()
            else:
                snapshot.warnings.append(f"No native collector for {self.platform}.")
        except Exception as exc:
            snapshot.warnings.append(
                f"Native collector failed: {type(exc).__name__}: {exc}"
            )
        try:
            nv_gpus, nv_processes = nvidia.collect()
            existing = {_pci_key(gpu.id): gpu for gpu in snapshot.gpus}
            for nv in nv_gpus:
                old = existing.get(_pci_key(nv.id))
                if old is None and self.platform == "Windows":
                    candidates = [
                        gpu
                        for gpu in snapshot.gpus
                        if gpu.vendor == "NVIDIA"
                        and gpu.name.casefold() == nv.name.casefold()
                    ]
                    if len(candidates) == 1:
                        old = candidates[0]
                if old is None:
                    snapshot.gpus.append(nv)
                    continue
                for field in (
                    "name",
                    "vendor",
                    "kind",
                    "utilization",
                    "memory_utilization",
                    "memory_used",
                    "memory_total",
                    "temperature",
                    "power_w",
                    "power_limit_w",
                    "fan_percent",
                    "clock_mhz",
                ):
                    value = getattr(nv, field)
                    if value is not None:
                        setattr(old, field, value)
                old.source += " + nvidia-smi"
                old.notes.extend(nv.notes)
                for process in nv_processes:
                    if process.gpu_id == nv.id:
                        process.gpu_id = old.id
            current = {(p.pid, p.gpu_id): p for p in snapshot.processes}
            for process in nv_processes:
                old = current.get((process.pid, process.gpu_id))
                if old:
                    old.memory_used = process.memory_used or old.memory_used
                    old.name = process.name or old.name
                    old.source += " + nvidia-smi"
                else:
                    snapshot.processes.append(process)
        except Exception as exc:
            snapshot.warnings.append(
                f"NVIDIA collector failed: {type(exc).__name__}: {exc}"
            )
        if not snapshot.gpus:
            snapshot.warnings.append(
                "No GPU detected. Check graphics drivers and access to vendor utilities."
            )
        return snapshot


def demo_snapshot() -> Snapshot:
    t = time.monotonic()
    swing = lambda shift: 50 + 36 * math.sin(t / 7 + shift)
    gpus = [
        GPU(
            "demo:0",
            "NVIDIA RTX 4090",
            "NVIDIA",
            "dedicated",
            "demo",
            "demo",
            utilization=swing(0),
            memory_utilization=swing(1),
            memory_used=12_300_000_000,
            memory_total=24_000_000_000,
            temperature=65 + 8 * math.sin(t / 11),
            power_w=220 + 65 * math.sin(t / 13),
            power_limit_w=450,
            fan_percent=55,
            clock_mhz=2520,
            engines={"graphics": swing(0), "copy": swing(1.9), "video": swing(3.1)},
        ),
        GPU(
            "demo:1",
            "Intel Arc integrated graphics",
            "Intel",
            "integrated",
            "demo",
            "demo",
            utilization=swing(2.5) / 2,
            memory_used=820_000_000,
            temperature=49,
            engines={"render": swing(2.5) / 2, "video": swing(0.8) / 2},
        ),
    ]
    processes = [
        Process(
            2451, "blender", "demo:0", "compute", swing(0) / 2, 8_400_000_000, "demo"
        ),
        Process(
            2218,
            "python train.py",
            "demo:0",
            "compute",
            swing(1.2) / 2,
            3_200_000_000,
            "demo",
        ),
        Process(
            964, "Firefox", "demo:1", "render", swing(2.5) / 3, 510_000_000, "demo"
        ),
    ]
    return Snapshot(gpus, processes)
