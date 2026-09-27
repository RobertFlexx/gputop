from __future__ import annotations

import shutil

from gputop.collectors.common import clamp, command, csv_rows, number
from gputop.model import GPU, Process

GPU_FIELDS = (
    "index,uuid,name,pci.bus_id,utilization.gpu,utilization.memory,"
    "memory.used,memory.total,temperature.gpu,power.draw,power.limit,"
    "fan.speed,clocks.current.graphics"
)


def collect() -> tuple[list[GPU], list[Process]]:
    if not shutil.which("nvidia-smi"):
        return [], []
    output = command(["nvidia-smi", f"--query-gpu={GPU_FIELDS}", "--format=csv,noheader,nounits"], 3.5)
    if output is None:
        return [], []
    gpus: list[GPU] = []
    by_uuid: dict[str, str] = {}
    for row in csv_rows(output):
        if len(row) != 13:
            continue
        index, uuid, name, pci, util, mem_util, mem_used, mem_total, temp, power, limit, fan, clock = row
        gpu_id = pci if pci and pci != "[N/A]" else uuid or index
        by_uuid[uuid] = gpu_id
        used_mb = number(mem_used)
        total_mb = number(mem_total)
        gpus.append(GPU(
            id=gpu_id, name=name, vendor="NVIDIA", kind="dedicated", driver="NVIDIA",
            source="nvidia-smi", utilization=clamp(number(util)),
            memory_utilization=clamp(number(mem_util)),
            memory_used=int(used_mb * 1024 * 1024) if used_mb is not None else None,
            memory_total=int(total_mb * 1024 * 1024) if total_mb is not None else None,
            temperature=number(temp), power_w=number(power), power_limit_w=number(limit),
            fan_percent=clamp(number(fan)), clock_mhz=number(clock),
            notes=["Per-process data covers compute processes reported by nvidia-smi."],
        ))
    processes: list[Process] = []
    output = command([
        "nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
        "--format=csv,noheader,nounits",
    ], 2.5)
    if output:
        for row in csv_rows(output):
            if len(row) != 4 or row[0] not in by_uuid:
                continue
            pid = number(row[1])
            if pid is None:
                continue
            used = number(row[3])
            processes.append(Process(int(pid), row[2], by_uuid[row[0]], "compute",
                                     memory_used=int(used * 1024 * 1024) if used is not None else None,
                                     source="nvidia-smi"))
    return gpus, processes
