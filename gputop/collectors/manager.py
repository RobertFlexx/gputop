from __future__ import annotations

import math
import platform
import time
from collections import Counter, defaultdict

from gputop.collectors import nvidia
from gputop.collectors.hardware import pci_key
from gputop.model import GPU, Process, Snapshot

_GPU_FIELDS = (
    "name",
    "vendor",
    "utilization",
    "memory_utilization",
    "memory_used",
    "memory_total",
    "temperature",
    "power_w",
    "power_limit_w",
    "fan_percent",
    "clock_mhz",
)


def _source(old: str, new: str) -> str:
    return " + ".join(
        dict.fromkeys(part for part in (old + " + " + new).split(" + ") if part)
    )


def _merge_nvidia(
    snapshot: Snapshot, gpus: list[GPU], processes: list[Process], os_name: str
) -> None:
    existing: dict[str, list[GPU]] = defaultdict(list)
    by_name: dict[str, list[GPU]] = defaultdict(list)
    for gpu in snapshot.gpus:
        existing[pci_key(gpu.id)].append(gpu)
        if gpu.vendor == "NVIDIA":
            by_name[gpu.name.casefold()].append(gpu)
    name_counts = Counter(gpu.name.casefold() for gpu in gpus)
    remap: dict[str, str] = {}
    matched: set[str] = set()
    for incoming in gpus:
        candidates = existing.get(pci_key(incoming.id), [])
        if (
            not candidates
            and os_name == "Windows"
            and name_counts[incoming.name.casefold()] == 1
        ):
            candidates = by_name.get(incoming.name.casefold(), [])
        old = (
            candidates[0]
            if len(candidates) == 1 and candidates[0].id not in matched
            else None
        )
        if old is None:
            snapshot.gpus.append(incoming)
            continue
        matched.add(old.id)
        remap[incoming.id] = old.id
        for field in _GPU_FIELDS:
            value = getattr(incoming, field)
            if value is not None:
                setattr(old, field, value)
        if old.kind == "unknown" and incoming.kind != "unknown":
            old.kind = incoming.kind
            if "kind_source" in incoming.extras:
                old.extras["kind_source"] = incoming.extras["kind_source"]
        old.source = _source(old.source, incoming.source)
        old.notes = list(dict.fromkeys(old.notes + incoming.notes))

    current = {(process.pid, process.gpu_id): process for process in snapshot.processes}
    for process in processes:
        process.gpu_id = remap.get(process.gpu_id, process.gpu_id)
        key = (process.pid, process.gpu_id)
        old = current.get(key)
        if old is None:
            snapshot.processes.append(process)
            current[key] = process
        else:
            if process.memory_used is not None:
                old.memory_used = process.memory_used
            if process.name:
                old.name = process.name
            old.source = _source(old.source, process.source)


class Collector:
    def __init__(self, demo: bool = False) -> None:
        self.demo = demo
        self.platform = platform.system()
        self.native = None
        if demo:
            return
        if self.platform == "Linux":
            from gputop.collectors.linux import LinuxCollector

            self.native = LinuxCollector()
        elif self.platform == "Darwin":
            from gputop.collectors.macos import MacCollector

            self.native = MacCollector()
        elif self.platform == "Windows":
            from gputop.collectors.windows import WindowsCollector

            self.native = WindowsCollector()

    def collect(self) -> Snapshot:
        if self.demo:
            return demo_snapshot()
        snapshot = Snapshot()
        try:
            if self.native is not None:
                snapshot.gpus, snapshot.processes = self.native.collect()
            else:
                snapshot.warnings.append(f"No native collector for {self.platform}.")
        except Exception as exc:
            snapshot.warnings.append(
                f"Native collector failed: {type(exc).__name__}: {exc}"
            )
        try:
            nv_gpus, nv_processes = nvidia.collect()
            _merge_nvidia(snapshot, nv_gpus, nv_processes, self.platform)
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
