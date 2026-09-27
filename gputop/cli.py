from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import time

from gputop import __version__
from gputop.collectors.manager import Collector
from gputop.collectors.common import size, terminal_text


def _print_line(*values: object, file=None) -> None:
    print(terminal_text(" ".join(str(value) for value in values)), file=file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="htop-inspired GPU monitor")
    parser.add_argument(
        "--interval",
        type=float,
        default=1.5,
        help="Refresh interval in seconds, 0.1 to 60 (default: 1.5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one machine-readable snapshot and exit",
    )
    parser.add_argument(
        "--once", action="store_true", help="Print one human-readable snapshot and exit"
    )
    parser.add_argument(
        "--demo", action="store_true", help="Show sample GPUs and processes"
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Report detected GPU sources and available metrics",
    )
    parser.add_argument("--version", action="version", version=f"gputop {__version__}")
    args = parser.parse_args(argv)
    if not 0.1 <= args.interval <= 60:
        parser.error("--interval must be between 0.1 and 60 seconds")
    collector = Collector(demo=args.demo)
    if args.json or args.once or args.doctor:
        previous_end = None
        if not args.demo:
            started = time.monotonic()
            collector.collect()
            previous_end = time.monotonic()
            time.sleep(max(0.0, args.interval - (previous_end - started)))
        started = time.monotonic()
        snapshot = collector.collect()
        ended = time.monotonic()
        snapshot.collection_duration_s = ended - started
        snapshot.sample_interval_s = (
            ended - previous_end if previous_end is not None else None
        )
        if args.doctor:
            _print_line(f"gputop {__version__} on {platform.platform()}")
            available = [
                name
                for name in (
                    "ioreg",
                    "system_profiler",
                    "powermetrics",
                    "lspci",
                    "nvidia-smi",
                    "powershell.exe",
                )
                if shutil.which(name)
            ]
            _print_line("Utilities:", ", ".join(available) or "none")
            for gpu in snapshot.gpus:
                metrics = [
                    name
                    for name in (
                        "utilization",
                        "memory_used",
                        "memory_total",
                        "temperature",
                        "power_w",
                        "clock_mhz",
                        "core_count",
                    )
                    if getattr(gpu, name) is not None
                ]
                if gpu.core_utilization:
                    metrics.append("core_utilization")
                _print_line(f"GPU {gpu.id}: {gpu.name} [{gpu.vendor}] via {gpu.source}")
                _print_line(
                    f"  Type: {gpu.kind} (source: {gpu.extras.get('kind_source', 'unavailable')})"
                )
                _print_line("  Metrics:", ", ".join(metrics) or "inventory only")
                if gpu.core_count is not None:
                    _print_line(
                        f"  Individual GPU cores: {len(gpu.core_utilization)}/{gpu.core_count} measured"
                    )
                _print_line(
                    f"  Processes: {sum(p.gpu_id == gpu.id for p in snapshot.processes)}"
                )
                for note in gpu.notes:
                    _print_line("  Note:", note)
            for warning in snapshot.warnings:
                _print_line("Warning:", warning)
        elif args.json:
            print(json.dumps(snapshot.as_dict(), indent=2, allow_nan=False))
        else:
            for gpu in snapshot.gpus:
                usage = (
                    f"{gpu.utilization:.0f}%" if gpu.utilization is not None else "--"
                )
                temp = (
                    f"{gpu.temperature:.0f}°C" if gpu.temperature is not None else "--"
                )
                memory_label = "Unified" if gpu.vendor == "Apple" else "VRAM"
                cores = (
                    f"  {gpu.core_equivalent_load:.1f}/{gpu.core_count} core eq estimate"
                    if gpu.core_equivalent_load is not None and gpu.core_count
                    else ""
                )
                _print_line(
                    f"{gpu.name} [{gpu.id}] ({gpu.kind})  GPU {usage}  {memory_label} "
                    f"{size(gpu.memory_used)}/{size(gpu.memory_total)}  {temp}{cores}"
                )
                for name, value in sorted(gpu.core_utilization.items()):
                    _print_line(f"  {name}: {value:.1f}%")
            for process in sorted(
                snapshot.processes, key=lambda p: p.utilization or 0, reverse=True
            ):
                usage = (
                    f"{process.utilization:.1f}%"
                    if process.utilization is not None
                    else "--"
                )
                gpu_ms = (
                    f"{process.gpu_time_ms_s:.1f}ms/s"
                    if process.gpu_time_ms_s is not None
                    else "--"
                )
                _print_line(
                    f"  {process.pid:>7}  {process.name:<28} {usage:>6}  {gpu_ms:>9}  "
                    f"{size(process.memory_used):>9}  {process.gpu_id}"
                )
            for warning in snapshot.warnings:
                _print_line(f"warning: {warning}", file=sys.stderr)
        return 0 if snapshot.gpus else 1
    try:
        from gputop.ui import run

        run(collector, args.interval)
    except ImportError as exc:
        _print_line(
            f"Terminal UI unavailable: {exc}. On Windows install gputop[windows].",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
