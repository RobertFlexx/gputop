from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class Process:
    pid: int
    name: str
    gpu_id: str
    engine: str = ""
    utilization: float | None = None
    memory_used: int | None = None
    source: str = ""
    gpu_time_ms_s: float | None = None
    gpu_time_total_ns: int | None = None
    queue_count: int | None = None
    last_active_seconds: float | None = None
    system_memory_used: int | None = None


@dataclass
class GPU:
    id: str
    name: str
    vendor: str
    kind: str = "unknown"
    driver: str = ""
    source: str = ""
    utilization: float | None = None
    memory_utilization: float | None = None
    memory_used: int | None = None
    memory_total: int | None = None
    temperature: float | None = None
    power_w: float | None = None
    power_limit_w: float | None = None
    fan_percent: float | None = None
    clock_mhz: float | None = None
    core_count: int | None = None
    core_equivalent_load: float | None = None
    core_utilization: dict[str, float] = field(default_factory=dict)
    engines: dict[str, float] = field(default_factory=dict)
    extras: dict[str, int | float | str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    gpus: list[GPU] = field(default_factory=list)
    processes: list[Process] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sample_interval_s: float | None = None
    collection_duration_s: float | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict:
        return asdict(self)
