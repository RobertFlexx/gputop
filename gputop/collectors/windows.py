from __future__ import annotations

import json
import re

from gputop.collectors.common import clamp, command, number
from gputop.model import GPU, Process

# Get-Counter exposes WDDM GPU engines for Intel, AMD, and NVIDIA devices.
# Counter instances include a PID and adapter LUID, but WMI does not expose a
# reliable LUID to physical adapter mapping. We keep LUID groups separate.
SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$adapters = @(Get-CimInstance Win32_VideoController | ForEach-Object {
    [pscustomobject]@{ Name=$_.Name; PNPDeviceID=$_.PNPDeviceID; AdapterRAM=$_.AdapterRAM; DriverVersion=$_.DriverVersion }
})
$engine = @((Get-Counter '\GPU Engine(*)\Utilization Percentage').CounterSamples | ForEach-Object {
    [pscustomobject]@{ Path=$_.Path; Value=$_.CookedValue }
})
$memory = @((Get-Counter '\GPU Process Memory(*)\Dedicated Usage').CounterSamples | ForEach-Object {
    [pscustomobject]@{ Path=$_.Path; Value=$_.CookedValue }
})
$names = @(Get-Process | ForEach-Object { [pscustomobject]@{ Id=$_.Id; Name=$_.ProcessName; WorkingSet=$_.WorkingSet64 } })
[pscustomobject]@{ adapters=$adapters; engine=$engine; memory=$memory; names=$names } | ConvertTo-Json -Compress -Depth 5
"""


def parse(payload: dict) -> tuple[list[GPU], list[Process]]:
    adapters = payload.get("adapters") or []
    if isinstance(adapters, dict):
        adapters = [adapters]
    gpus: list[GPU] = []
    for index, entry in enumerate(adapters):
        name = str(entry.get("Name") or f"GPU {index}")
        vendor = (
            "NVIDIA"
            if "NVIDIA" in name.upper()
            else (
                "AMD"
                if any(x in name.upper() for x in ("AMD", "RADEON"))
                else "Intel" if "INTEL" in name.upper() else "Unknown"
            )
        )
        if vendor == "Intel":
            kind = (
                "dedicated"
                if re.search(r"\bArc [AB]\d|Iris Xe MAX", name, re.I)
                else "integrated"
            )
        else:
            kind = "unknown"
        gpus.append(
            GPU(
                id=f"win:{index}",
                name=name,
                vendor=vendor,
                kind=kind,
                driver=str(entry.get("DriverVersion") or ""),
                source="WDDM counters",
                notes=["Temperature and power need a vendor utility."],
            )
        )
    groups: dict[str, dict[str, float]] = {}
    proc_load: dict[tuple[str, int], float] = {}
    for entry in payload.get("engine") or []:
        path = str(entry.get("Path") or "").lower()
        match = re.search(
            r"pid_(\d+)_luid_([^_]+_[^_]+)_phys_\d+_eng_\d+_engtype_([^\\)]+)", path
        )
        value = number(entry.get("Value"))
        if not match or value is None:
            continue
        pid, luid, engine = int(match.group(1)), match.group(2), match.group(3)
        groups.setdefault(luid, {})[engine] = (
            groups.setdefault(luid, {}).get(engine, 0) + value
        )
        key = (luid, pid)
        proc_load[key] = max(proc_load.get(key, 0), value)
    luid_map: dict[str, str] = {}
    if len(gpus) == 1 and len(groups) == 1:
        luid = next(iter(groups))
        luid_map[luid] = gpus[0].id
        gpus[0].engines = {
            name: clamp(load) or 0 for name, load in groups[luid].items()
        }
        gpus[0].utilization = max(gpus[0].engines.values(), default=None)
    else:
        for luid, engines in groups.items():
            gpu_id = f"luid:{luid}"
            luid_map[luid] = gpu_id
            mapped = {name: clamp(load) or 0 for name, load in engines.items()}
            gpus.append(
                GPU(
                    id=gpu_id,
                    name=f"WDDM adapter {luid}",
                    vendor="Unknown",
                    source="WDDM counters",
                    utilization=max(mapped.values(), default=None),
                    engines=mapped,
                    notes=["Adapter LUID could not be matched to a device name."],
                )
            )
    mem_usage: dict[tuple[str, int], int] = {}
    pid_mem_usage: dict[int, int] = {}
    for entry in payload.get("memory") or []:
        path = str(entry.get("Path") or "").lower()
        match = re.search(r"pid_(\d+)", path)
        luid_match = re.search(r"luid_([^_]+_[^_]+)", path)
        value = number(entry.get("Value"))
        if match and value is not None:
            pid = int(match.group(1))
            if luid_match:
                key = (luid_match.group(1), pid)
                mem_usage[key] = max(mem_usage.get(key, 0), int(value))
            else:
                pid_mem_usage[pid] = max(pid_mem_usage.get(pid, 0), int(value))
    names = {
        int(item["Id"]): str(item["Name"])
        for item in payload.get("names") or []
        if isinstance(item, dict) and item.get("Id") is not None
    }
    ram = {
        int(item["Id"]): int(item["WorkingSet"])
        for item in payload.get("names") or []
        if isinstance(item, dict)
        and item.get("Id") is not None
        and item.get("WorkingSet") is not None
    }
    processes = [
        Process(
            pid,
            names.get(pid, f"pid {pid}"),
            luid_map[luid],
            "WDDM",
            clamp(load),
            mem_usage.get((luid, pid), pid_mem_usage.get(pid)),
            "WDDM counters",
            system_memory_used=ram.get(pid),
        )
        for (luid, pid), load in proc_load.items()
        if luid in luid_map
    ]
    return gpus, processes


def collect() -> tuple[list[GPU], list[Process]]:
    output = command(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT], 6
    )
    if not output:
        return [], []
    try:
        return parse(json.loads(output))
    except (TypeError, ValueError, KeyError):
        return [], []
