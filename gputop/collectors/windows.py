from __future__ import annotations

import json
import re
import time
from collections import Counter

from gputop.collectors import dxcore
from gputop.collectors.common import clamp, command, integer, number
from gputop.collectors.hardware import HardwareAdapter, pci_vendor
from gputop.model import GPU, Process

INVENTORY_INTERVAL = 60.0

# Sample engine and memory counters together. Explicit UTF-8 also handles
# non-ASCII process and device names under Windows PowerShell.
SCRIPT_HEADER = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
"""
INVENTORY_SCRIPT = r"""
$adapters = @(Get-CimInstance Win32_VideoController | ForEach-Object {
    [pscustomobject]@{ Name=$_.Name; PNPDeviceID=$_.PNPDeviceID; DriverVersion=$_.DriverVersion }
})
"""
COUNTER_SCRIPT = r"""
$samples = (Get-Counter -Counter @('\GPU Engine(*)\Utilization Percentage', '\GPU Process Memory(*)\Dedicated Usage')).CounterSamples
$engine = @($samples | Where-Object { $_.Path -like '*\gpu engine(*' } | ForEach-Object {
    [pscustomobject]@{ Path=$_.Path; Value=$_.CookedValue }
})
$memory = @($samples | Where-Object { $_.Path -like '*\gpu process memory(*' } | ForEach-Object {
    [pscustomobject]@{ Path=$_.Path; Value=$_.CookedValue }
})
$names = @(Get-Process | ForEach-Object { [pscustomobject]@{ Id=$_.Id; Name=$_.ProcessName; WorkingSet=$_.WorkingSet64 } })
[pscustomobject]@{ adapters=$adapters; engine=$engine; memory=$memory; names=$names } | ConvertTo-Json -Compress -Depth 5
"""
SCRIPT = SCRIPT_HEADER + INVENTORY_SCRIPT + COUNTER_SCRIPT

_ENGINE = re.compile(
    r"pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_(\d+)_eng_(\d+)_engtype_([^\\)]+)"
)
_MEMORY = re.compile(r"pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+")


def _rows(payload: dict, key: str) -> list[dict]:
    value = payload.get(key)
    if isinstance(value, dict):
        return [value]
    return (
        [entry for entry in value if isinstance(entry, dict)]
        if isinstance(value, list)
        else []
    )


def _luid(value: str) -> str:
    high, low = value.split("_")
    return f"0x{int(high, 16):08x}_0x{int(low, 16):08x}"


def parse(
    payload: object, hardware: list[HardwareAdapter] | None = None
) -> tuple[list[GPU], list[Process]]:
    if not isinstance(payload, dict):
        return [], []
    gpus: list[GPU] = []
    luid_map: dict[str, GPU] = {}
    for info in hardware or []:
        gpu = GPU(
            id=f"luid:{info.id}",
            name=info.name,
            vendor=pci_vendor(info.vendor_id),
            kind=info.kind,
            source="DXCore + WDDM counters",
        )
        if info.kind != "unknown":
            gpu.extras["kind_source"] = "DXCore.IsIntegrated"
        gpus.append(gpu)
        luid_map[info.id] = gpu
    remaining_names = Counter(gpu.name.casefold() for gpu in gpus)
    native_gpus = list(gpus)
    inventory = _rows(payload, "adapters")
    inventory_names = Counter(
        str(entry.get("Name") or "").casefold() for entry in inventory
    )
    for index, entry in enumerate(inventory):
        name = str(entry.get("Name") or f"GPU {index}")
        if remaining_names[name.casefold()] > 0:
            remaining_names[name.casefold()] -= 1
            matches = [
                gpu for gpu in native_gpus if gpu.name.casefold() == name.casefold()
            ]
            if len(matches) == 1 and inventory_names[name.casefold()] == 1:
                matches[0].driver = str(entry.get("DriverVersion") or "")
            continue
        # Keep older or unsupported adapters even if DXCore found other GPUs.
        pnp_id = str(entry.get("PNPDeviceID") or "")
        vendor_id = re.search(r"VEN_([0-9a-f]{4})", pnp_id, re.I)
        gpu_id = f"win:{pnp_id.casefold() or index}"
        if any(gpu.id == gpu_id for gpu in gpus):
            continue
        gpus.append(
            GPU(
                id=gpu_id,
                name=name,
                vendor=pci_vendor(vendor_id.group(1)) if vendor_id else "Unknown",
                driver=str(entry.get("DriverVersion") or ""),
                source="WDDM counters",
                notes=["GPU type unavailable: DXCore did not report this adapter."],
            )
        )

    groups: dict[str, dict[tuple[str, str, str], float]] = {}
    proc_load: dict[tuple[str, int], float] = {}
    for entry in _rows(payload, "engine"):
        match = _ENGINE.search(str(entry.get("Path") or "").lower())
        value = number(entry.get("Value"))
        if not match or value is None or value < 0:
            continue
        pid, luid = int(match.group(1)), _luid(match.group(2))
        engine = (match.group(5), match.group(3), match.group(4))
        engines = groups.setdefault(luid, {})
        engines[engine] = engines.get(engine, 0.0) + value
        key = (luid, pid)
        proc_load[key] = max(proc_load.get(key, 0.0), value)

    memory: dict[tuple[str, int], int] = {}
    for entry in _rows(payload, "memory"):
        match = _MEMORY.search(str(entry.get("Path") or "").lower())
        value = number(entry.get("Value"))
        if match and value is not None and value >= 0:
            luid, pid = _luid(match.group(2)), int(match.group(1))
            groups.setdefault(luid, {})
            memory[(luid, pid)] = max(memory.get((luid, pid), 0), int(value))

    # Fallback mapping is safe only when both inventories contain one adapter.
    if not hardware and len(gpus) == 1 and len(groups) == 1:
        luid_map[next(iter(groups))] = gpus[0]
    for luid, engines in groups.items():
        gpu = luid_map.get(luid)
        if gpu is None:
            gpu = GPU(
                id=f"luid:{luid}",
                name=f"WDDM adapter {luid}",
                vendor="Unknown",
                source="WDDM counters",
                notes=["Adapter LUID could not be matched to a device name."],
            )
            luid_map[luid] = gpu
            gpus.append(gpu)
        counts = Counter(engine for engine, _, _ in engines)
        gpu.engines = {
            (name if counts[name] == 1 else f"{name} {physical}:{index}"): clamp(load)
            or 0.0
            for (name, physical, index), load in engines.items()
        }
        gpu.utilization = max(gpu.engines.values(), default=None)

    names: dict[int, str] = {}
    ram: dict[int, int] = {}
    for entry in _rows(payload, "names"):
        pid = integer(entry.get("Id"))
        if pid is None or pid < 0:
            continue
        names[pid] = str(entry.get("Name") or f"pid {pid}")
        used = integer(entry.get("WorkingSet"))
        if used is not None and used >= 0:
            ram[pid] = used
    processes = [
        Process(
            pid,
            names.get(pid, f"pid {pid}"),
            luid_map[luid].id,
            "WDDM",
            clamp(proc_load.get((luid, pid))),
            memory.get((luid, pid)),
            "WDDM counters",
            system_memory_used=ram.get(pid),
        )
        for luid, pid in sorted(proc_load.keys() | memory.keys())
    ]
    return gpus, processes


def collect(
    hardware: list[HardwareAdapter] | None = None, inventory: list[dict] | None = None
) -> tuple[list[GPU], list[Process]]:
    script = SCRIPT if inventory is None else SCRIPT_HEADER + COUNTER_SCRIPT
    output = command(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], 6
    )
    try:
        payload = json.loads(output) if output else {}
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if inventory is not None:
        payload["adapters"] = inventory
    return parse(payload, hardware)


class WindowsCollector:
    def __init__(self) -> None:
        self.hardware: list[HardwareAdapter] = []
        self.inventory: list[dict] = []
        self.last_discovery: float | None = None

    def collect(self) -> tuple[list[GPU], list[Process]]:
        now = time.monotonic()
        if (
            self.last_discovery is None
            or now - self.last_discovery >= INVENTORY_INTERVAL
        ):
            self.hardware = dxcore.adapters()
            script = (
                SCRIPT_HEADER
                + INVENTORY_SCRIPT
                + "ConvertTo-Json -InputObject @($adapters) -Compress -Depth 3"
            )
            output = command(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                6,
            )
            if output:
                try:
                    self.inventory = _rows({"adapters": json.loads(output)}, "adapters")
                except (TypeError, ValueError):
                    pass
            self.last_discovery = now
        return collect(self.hardware, self.inventory)
