from __future__ import annotations

import os
import re
import time
from functools import lru_cache
from pathlib import Path

from gputop.collectors import amdgpu
from gputop.collectors.common import clamp, command, integer, number, read
from gputop.collectors.hardware import pci_vendor
from gputop.model import GPU, Process

SYS_DRM = Path("/sys/class/drm")
PROC = Path("/proc")
INVENTORY_INTERVAL = 60.0
PWM_MAX = 255  # Linux hwmon PWM ABI range when pwm1_max is absent.


def _value(path: Path, divisor: float = 1.0) -> float | None:
    value = number(read(path))
    return value / divisor if value is not None else None


def _pci_id(device: Path) -> str:
    uevent = read(device / "uevent") or ""
    match = re.search(r"^PCI_SLOT_NAME=(.+)$", uevent, re.M)
    if match:
        return match.group(1)
    try:
        return str(device.resolve())
    except (OSError, RuntimeError):
        return str(device)


@lru_cache(maxsize=128)
def _pci_name(gpu_id: str) -> str | None:
    output = command(["lspci", "-s", gpu_id, "-mm"], 1)
    if not output:
        return None
    parts = re.findall(r'"([^"]+)"', output)
    return f"{parts[1]} {parts[2]}" if len(parts) >= 3 else None


def _gpu_cards(
    root: Path, kinds: dict[Path, tuple[float, str]] | None = None
) -> list[GPU]:
    gpus: list[GPU] = []
    now = time.monotonic()
    present: set[Path] = set()
    for card in sorted(root.glob("card[0-9]*")):
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = card / "device"
        if not device.exists():
            continue
        vendor_id = (read(device / "vendor") or "").lower()
        vendor = pci_vendor(vendor_id)
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
        vram_total = integer(read(device / "mem_info_vram_total"))
        vram_used = integer(read(device / "mem_info_vram_used"))
        kind = "unknown"
        try:
            identity = device.resolve()
        except (OSError, RuntimeError):
            identity = device
        present.add(identity)
        if driver == "amdgpu":
            cached = kinds.get(identity) if kinds is not None else None
            if cached is not None and now - cached[0] < INVENTORY_INTERVAL:
                kind = cached[1]
            else:
                nodes = [
                    Path("/dev/dri") / node.name
                    for node in sorted((device / "drm").glob("renderD*"))
                ]
                nodes.append(Path("/dev/dri") / card.name)
                kind = amdgpu.device_kind(nodes)
                if kinds is not None:
                    kinds[identity] = (now, kind)
        gpu = GPU(
            id=gpu_id,
            name=name,
            vendor=vendor,
            kind=kind,
            driver=driver,
            source="Linux DRM/sysfs",
            utilization=clamp(_value(device / "gpu_busy_percent")),
            memory_utilization=clamp(_value(device / "mem_busy_percent")),
            memory_used=vram_used if vram_used is not None and vram_used >= 0 else None,
            memory_total=(
                vram_total if vram_total is not None and vram_total >= 0 else None
            ),
        )
        if kind != "unknown":
            gpu.extras["kind_source"] = "AMDGPU_INFO_DEV_INFO.ids_flags"
        gpu.clock_mhz = _value(device / "gt_cur_freq_mhz")
        if gpu.clock_mhz is None:
            clocks = read(device / "pp_dpm_sclk") or ""
            active = re.search(r"^\s*\d+:\s*(\d+)Mhz\s*\*", clocks, re.M | re.I)
            if active:
                gpu.clock_mhz = float(active.group(1))
        for hwmon in sorted((device / "hwmon").glob("hwmon*")):
            for field, sensor, divisor in (
                ("temperature", "temp1_input", 1000),
                ("power_w", "power1_average", 1_000_000),
                ("power_limit_w", "power1_cap", 1_000_000),
            ):
                if getattr(gpu, field) is None:
                    setattr(gpu, field, _value(hwmon / sensor, divisor))
            if gpu.fan_percent is None:
                pwm = _value(hwmon / "pwm1")
                pwm_max = _value(hwmon / "pwm1_max")
                if pwm_max is None:
                    pwm_max = PWM_MAX
                if pwm is not None and pwm_max > 0:
                    gpu.fan_percent = clamp(pwm / pwm_max * 100)
            for channel, label in (
                ("temp2_input", "hotspot_c"),
                ("temp3_input", "memory_c"),
            ):
                value = _value(hwmon / channel, 1000)
                if value is not None:
                    gpu.extras[label] = value
        gpus.append(gpu)
    if kinds is not None:
        for identity in set(kinds) - present:
            del kinds[identity]
    return gpus


def _bytes(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*(B|KiB|MiB|GiB)?\s*", value, re.I)
    if not match:
        return None
    factor = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3}
    amount = integer(match.group(1))
    return (
        amount * factor[(match.group(2) or "B").lower()] if amount is not None else None
    )


def _nanoseconds(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s+ns\s*", value)
    return integer(match.group(1)) if match else None


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
        self.previous: dict[tuple[str, str, str], int] = {}
        self.last_time: float | None = None
        self.kinds: dict[Path, tuple[float, str]] = {}

    def collect(self) -> tuple[list[GPU], list[Process]]:
        gpus = _gpu_cards(self.drm_root, self.kinds)
        if not gpus:
            self.previous.clear()
            self.last_time = None
            return [], []
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
        percent_per_ns = (
            100 / (elapsed * 1_000_000_000)
            if elapsed is not None and elapsed > 0
            else None
        )
        counters: dict[tuple[str, str, str], int] = {}
        process_engines: dict[tuple[int, str], set[str]] = {}
        process_rates: dict[tuple[int, str, str], float] = {}
        process_memory: dict[tuple[int, str], int] = {}
        process_names: dict[int, str] = {}
        process_rss: dict[int, int] = {}
        client_rates: dict[tuple[str, str, str], float] = {}
        rate_factors: dict[tuple[str, str, int], float] = {}
        try:
            proc_dirs = list(self.proc_root.iterdir())
        except OSError:
            proc_dirs = []
        for proc in proc_dirs:
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
                if not any(key.startswith("drm-") for key in info):
                    continue
                client_id = info.get("drm-client-id", f"pid:{pid}:fd:{fd.name}")
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
                    amounts = [_bytes(info[key]) for key in memory_keys]
                    valid = [value for value in amounts if value is not None]
                    if valid:
                        process_memory[process_key] = process_memory.get(
                            process_key, 0
                        ) + sum(valid)
                for key, value in info.items():
                    if not key.startswith("drm-engine-") or key.startswith(
                        "drm-engine-capacity-"
                    ):
                        continue
                    engine = key.removeprefix("drm-engine-")
                    counter = _nanoseconds(value)
                    if counter is None:
                        continue
                    process_engines[process_key].add(engine)
                    counter_key = (gpu_id, client_id, engine)
                    old = self.previous.get(counter_key)
                    # DRM counters may temporarily decrease. Retain the high-water
                    # value until they catch up, as required by the fdinfo ABI.
                    counters[counter_key] = max(
                        counter, old or 0, counters.get(counter_key, 0)
                    )
                    capacity = integer(info.get(f"drm-engine-capacity-{engine}", 1))
                    if (
                        old is not None
                        and percent_per_ns is not None
                        and counter >= old
                        and capacity is not None
                        and capacity > 0
                    ):
                        factor_key = (gpu_id, engine, capacity)
                        if factor_key not in rate_factors:
                            rate_factors[factor_key] = percent_per_ns / capacity
                        rate = (counter - old) * rate_factors[factor_key]
                        rate_key = (pid, gpu_id, engine)
                        process_rates[rate_key] = (
                            process_rates.get(rate_key, 0.0) + rate
                        )
                        # A client can be shared by several processes. Count it
                        # once in device totals, even if several PIDs expose it.
                        client_rates[counter_key] = max(
                            client_rates.get(counter_key, 0.0), rate
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
                process_rates[(pid, gpu_id, engine)]
                for engine in engines
                if (pid, gpu_id, engine) in process_rates
            ]
            processes.append(
                Process(
                    pid,
                    process_names[pid],
                    gpu_id,
                    ",".join(sorted(engines)) or "DRM",
                    clamp(max(rates)) if rates else None,
                    process_memory.get((pid, gpu_id)),
                    "DRM fdinfo",
                    system_memory_used=process_rss.get(pid),
                )
            )
        engine_sums: dict[tuple[str, str], float] = {}
        for (gpu_id, _, engine), rate in client_rates.items():
            engine_sums[(gpu_id, engine)] = (
                engine_sums.get((gpu_id, engine), 0.0) + rate
            )
        for (gpu_id, engine), rate in engine_sums.items():
            by_id[gpu_id].engines[engine] = clamp(rate) or 0.0
        for gpu in gpus:
            if gpu.utilization is None and gpu.engines:
                gpu.utilization = max(gpu.engines.values())
            if gpu.utilization is None:
                gpu.notes.append(
                    "GPU load needs driver counters or accessible DRM fdinfo."
                )
        return gpus, processes
