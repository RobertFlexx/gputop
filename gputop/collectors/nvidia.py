from __future__ import annotations

import math
import re
import shutil

from gputop.collectors.common import clamp, command, csv_rows, integer, number
from gputop.model import GPU, Process

GPU_FIELDS = (
    "index,uuid,name,pci.bus_id,utilization.gpu,utilization.memory,"
    "memory.used,memory.total,temperature.gpu,power.draw,power.limit,"
    "fan.speed,clocks.current.graphics"
)


def _mib_bytes(value: str) -> int | None:
    amount = number(value)
    if amount is None or amount < 0:
        return None
    result = amount * 1024**2
    return int(result) if math.isfinite(result) else None


def collect() -> tuple[list[GPU], list[Process]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return [], []
    output = command(
        [executable, f"--query-gpu={GPU_FIELDS}", "--format=csv,noheader,nounits"], 3.5
    )
    if output is None:
        return [], []
    gpus: list[GPU] = []
    by_uuid: dict[str, str] = {}
    seen: set[str] = set()
    for row in csv_rows(output):
        if len(row) != 13:
            continue
        (
            index,
            uuid,
            name,
            pci,
            util,
            mem_util,
            mem_used,
            mem_total,
            temp,
            power,
            limit,
            fan,
            clock,
        ) = row
        if re.fullmatch(r"[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", pci):
            gpu_id = pci
        elif uuid.startswith(("GPU-", "MIG-")):
            gpu_id = uuid
        else:
            continue
        if not gpu_id or gpu_id in seen:
            continue
        seen.add(gpu_id)
        if uuid.startswith(("GPU-", "MIG-")):
            by_uuid[uuid] = gpu_id
        gpus.append(
            GPU(
                id=gpu_id,
                name=name,
                vendor="NVIDIA",
                driver="NVIDIA",
                source="nvidia-smi",
                utilization=clamp(number(util)),
                memory_utilization=clamp(number(mem_util)),
                memory_used=_mib_bytes(mem_used),
                memory_total=_mib_bytes(mem_total),
                temperature=number(temp),
                power_w=number(power),
                power_limit_w=number(limit),
                fan_percent=clamp(number(fan)),
                clock_mhz=number(clock),
                notes=[
                    "Per-process data covers compute processes reported by nvidia-smi."
                ],
            )
        )
    processes: list[Process] = []
    output = command(
        [
            executable,
            "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        2.5,
    )
    if output:
        for row in csv_rows(output):
            if len(row) != 4 or row[0] not in by_uuid:
                continue
            pid = integer(row[1])
            if pid is None or pid < 0:
                continue
            processes.append(
                Process(
                    pid,
                    row[2],
                    by_uuid[row[0]],
                    "compute",
                    memory_used=_mib_bytes(row[3]),
                    source="nvidia-smi",
                )
            )
    return gpus, processes
