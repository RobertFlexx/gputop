from __future__ import annotations

import os
import re
import time
from functools import lru_cache
from pathlib import Path

from gputop.collectors.common import clamp, command, number, read
from gputop.model import GPU, Process

SYS_DRM = Path("/sys/class/drm")
PROC = Path("/proc")
VENDORS = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA", "0x106b": "Apple"}


def _value(path: Path, divisor: float = 1.0) -> float | None:
    value = number(read(path))
    return value / divisor if value is not None else None


def _pci_id(device: Path) -> str:
    uevent = read(device / "uevent") or ""
    match = re.search(r"^PCI_SLOT_NAME=(.+)$", uevent, re.M)
    return match.group(1) if match else str(device.resolve())


@lru_cache(maxsize=128)
def _pci_name(gpu_id: str) -> str | None:
    output = command(["lspci", "-s", gpu_id, "-mm"], 1)
    if not output:
        return None
    parts = re.findall(r'"([^"]+)"', output)
    return f"{parts[1]} {parts[2]}" if len(parts) >= 3 else None


def _gpu_cards(root: Path) -> list[GPU]:
    gpus: list[GPU] = []
    for card in sorted(root.glob("card[0-9]*")):
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = card / "device"
        if not device.exists():
            continue
        vendor_id = (read(device / "vendor") or "").lower()
        vendor = VENDORS.get(vendor_id, "Unknown")
        driver_path = device / "driver"
        try:
            driver = driver_path.resolve().name if driver_path.exists() else ""
        except OSError:
            driver = ""
        gpu_id = _pci_id(device)
        name = (
            read(device / "product_name")
            or _pci_name(gpu_id)
            or read(device / "product_number")
        )
        if not name:
            name = f"{vendor} GPU {read(device / 'device') or card.name}"
        vram_total = _value(device / "mem_info_vram_total")
        vram_used = _value(device / "mem_info_vram_used")
        kind = (
            "dedicated"
            if vram_total and vram_total > 1024**3
            else "integrated" if driver in {"i915", "xe"} else "unknown"
        )
        gpu = GPU(
            id=gpu_id,
            name=name,
            vendor=vendor,
            kind=kind,
            driver=driver,
            source="Linux DRM/sysfs",
            utilization=clamp(_value(device / "gpu_busy_percent")),
            memory_utilization=clamp(_value(device / "mem_busy_percent")),
            memory_used=int(vram_used) if vram_used is not None else None,
            memory_total=int(vram_total) if vram_total is not None else None,
        )
        gpu.clock_mhz = _value(device / "gt_cur_freq_mhz")
        if gpu.clock_mhz is None:
            clocks = read(device / "pp_dpm_sclk") or ""
            active = re.search(r"^\s*\d+:\s*(\d+)Mhz\s*\*", clocks, re.M | re.I)
            if active:
                gpu.clock_mhz = float(active.group(1))
        for hwmon in sorted((device / "hwmon").glob("hwmon*")):
            gpu.temperature = _value(hwmon / "temp1_input", 1000)
            gpu.power_w = _value(hwmon / "power1_average", 1_000_000)
            gpu.power_limit_w = _value(hwmon / "power1_cap", 1_000_000)
            pwm = _value(hwmon / "pwm1")
            pwm_max = _value(hwmon / "pwm1_max") or 255
            gpu.fan_percent = clamp(pwm / pwm_max * 100) if pwm is not None else None
            for channel, label in (
                ("temp2_input", "hotspot_c"),
                ("temp3_input", "memory_c"),
            ):
                value = _value(hwmon / channel, 1000)
                if value is not None:
                    gpu.extras[label] = value
            if gpu.temperature is not None or gpu.power_w is not None:
                break
        gpus.append(gpu)
    return gpus


def _bytes(value: str) -> int | None:
    match = re.match(r"\s*(\d+)\s*(B|KiB|MiB|GiB)?", value, re.I)
    if not match:
        return None
    factor = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3}
    return int(match.group(1)) * factor.get((match.group(2) or "B").lower(), 1)


def _fdinfo(text: str) -> dict[str, str]:
    return dict(
        (key.strip(), value.strip())
        for key, value in (
            line.split(":", 1) for line in text.splitlines() if ":" in line
        )
    )


class LinuxCollector:
    def __init__(self, drm_root: Path = SYS_DRM, proc_root: Path = PROC) -> None:
        self.drm_root = drm_root
        self.proc_root = proc_root
        self.previous: dict[tuple[int, str, str, str], int] = {}
        self.last_time: float | None = None

    def collect(self) -> tuple[list[GPU], list[Process]]:
        gpus = _gpu_cards(self.drm_root)
        by_id = {gpu.id: gpu for gpu in gpus}
        node_ids: dict[str, str] = {}
        for node in self.drm_root.glob("*"):
            if (
                re.fullmatch(r"(?:card|renderD)\d+", node.name)
                and (node / "device").exists()
            ):
                node_ids[node.name] = _pci_id(node / "device")
        now = time.monotonic()
        elapsed = now - self.last_time if self.last_time is not None else None
        counters: dict[tuple[int, str, str, str], int] = {}
        process_engines: dict[tuple[int, str], set[str]] = {}
        process_deltas: dict[tuple[int, str, str], int] = {}
        process_memory: dict[tuple[int, str], int] = {}
        process_names: dict[int, str] = {}
        process_rss: dict[int, int] = {}
        engine_sums: dict[tuple[str, str], int] = {}
        for proc in self.proc_root.iterdir() if self.proc_root.exists() else []:
            if not proc.name.isdigit():
                continue
            pid = int(proc.name)
            fd_dir = proc / "fd"
            fdinfo_dir = proc / "fdinfo"
            try:
                fds = list(fd_dir.iterdir())
            except OSError:
                continue
            seen: set[tuple[str, str]] = set()
            for fd in fds:
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if not target.startswith("/dev/dri/"):
                    continue
                gpu_id = node_ids.get(Path(target).name)
                if not gpu_id or gpu_id not in by_id:
                    continue
                info = _fdinfo(read(fdinfo_dir / fd.name) or "")
                client_id = info.get("drm-client-id", fd.name)
                client_key = (gpu_id, client_id)
                if client_key in seen:
                    continue
                seen.add(client_key)
                memory_keys = [key for key in info if key.startswith("drm-resident-")]
                if not memory_keys:
                    memory_keys = [key for key in info if key.startswith("drm-memory-")]
                process_key = (pid, gpu_id)
                process_engines.setdefault(process_key, set())
                if memory_keys:
                    process_memory[process_key] = process_memory.get(
                        process_key, 0
                    ) + sum(_bytes(info[key]) or 0 for key in memory_keys)
                for key, value in info.items():
                    if not key.startswith("drm-engine-"):
                        continue
                    engine = key.removeprefix("drm-engine-")
                    counter = _bytes(value)
                    if counter is None:
                        continue
                    process_engines[process_key].add(engine)
                    counter_key = (pid, gpu_id, client_id, engine)
                    counters[counter_key] = counter
                    old = self.previous.get(counter_key)
                    if old is not None and elapsed and elapsed > 0 and counter >= old:
                        delta = counter - old
                        delta_key = (pid, gpu_id, engine)
                        process_deltas[delta_key] = (
                            process_deltas.get(delta_key, 0) + delta
                        )
                        engine_sums[(gpu_id, engine)] = (
                            engine_sums.get((gpu_id, engine), 0) + delta
                        )
                if pid not in process_names:
                    process_names[pid] = read(proc / "comm") or f"pid {pid}"
                    status = read(proc / "status") or ""
                    rss = re.search(r"^VmRSS:\s*(\d+)\s+kB", status, re.M)
                    if rss:
                        process_rss[pid] = int(rss.group(1)) * 1024
        self.previous = counters
        self.last_time = now
        processes: list[Process] = []
        for (pid, gpu_id), engines in process_engines.items():
            rates = [
                clamp(
                    process_deltas[(pid, gpu_id, engine)]
                    / (elapsed * 1_000_000_000)
                    * 100
                )
                for engine in engines
                if elapsed and elapsed > 0 and (pid, gpu_id, engine) in process_deltas
            ]
            processes.append(
                Process(
                    pid,
                    process_names[pid],
                    gpu_id,
                    ",".join(sorted(engines)) or "DRM",
                    max(rates) if rates else None,
                    process_memory.get((pid, gpu_id)),
                    "DRM fdinfo",
                    system_memory_used=process_rss.get(pid),
                )
            )
        for (gpu_id, engine), ns in engine_sums.items():
            if elapsed and elapsed > 0:
                by_id[gpu_id].engines[engine] = (
                    clamp(ns / (elapsed * 1_000_000_000) * 100) or 0
                )
        for gpu in gpus:
            if gpu.utilization is None and gpu.engines:
                gpu.utilization = max(gpu.engines.values())
            if gpu.utilization is None:
                gpu.notes.append(
                    "GPU load needs driver counters or accessible DRM fdinfo."
                )
        return gpus, processes
