from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import time

from gputop import __version__
from gputop.collectors.manager import Collector
from gputop.collectors.common import size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="htop-inspired GPU monitor")
    parser.add_argument("--interval", type=float, default=1.5, help="Refresh interval in seconds (default: 1.5)")
    parser.add_argument("--json", action="store_true", help="Print one machine-readable snapshot and exit")
    parser.add_argument("--once", action="store_true", help="Print one human-readable snapshot and exit")
    parser.add_argument("--demo", action="store_true", help="Show sample GPUs and processes")
    parser.add_argument("--doctor", action="store_true", help="Report detected GPU sources and available metrics")
    parser.add_argument("--version", action="version", version=f"gputop {__version__}")
    args = parser.parse_args(argv)
    if not 0.1 <= args.interval <= 60:
        parser.error("--interval must be between 0.1 and 60 seconds")
    collector = Collector(demo=args.demo)
    if args.json or args.once or args.doctor:
        if not args.demo:
            collector.collect()
            time.sleep(args.interval)
        snapshot = collector.collect()
        if args.doctor:
            print(f"gputop {__version__} on {platform.platform()}")
            available = [name for name in ("ioreg", "system_profiler", "powermetrics", "lspci",
                                           "nvidia-smi", "powershell.exe") if shutil.which(name)]
            print("Utilities:", ", ".join(available) or "none")
            for gpu in snapshot.gpus:
                metrics = [name for name in ("utilization", "memory_used", "memory_total",
                                            "temperature", "power_w", "clock_mhz", "core_count")
                           if getattr(gpu, name) is not None]
                print(f"GPU {gpu.id}: {gpu.name} [{gpu.vendor}] via {gpu.source}")
                print("  Metrics:", ", ".join(metrics) or "inventory only")
                print(f"  Processes: {sum(p.gpu_id == gpu.id for p in snapshot.processes)}")
                for note in gpu.notes:
                    print("  Note:", note)
            for warning in snapshot.warnings:
                print("Warning:", warning)
        elif args.json:
            print(json.dumps(snapshot.as_dict(), indent=2))
        else:
            for gpu in snapshot.gpus:
                usage = f"{gpu.utilization:.0f}%" if gpu.utilization is not None else "--"
                temp = f"{gpu.temperature:.0f}°C" if gpu.temperature is not None else "--"
                memory_label = "Unified" if gpu.vendor == "Apple" else "VRAM"
                cores = (f"  {gpu.core_equivalent_load:.1f}/{gpu.core_count} core eq estimate"
                         if gpu.core_equivalent_load is not None and gpu.core_count else "")
                print(f"{gpu.name} [{gpu.id}]  GPU {usage}  {memory_label} "
                      f"{size(gpu.memory_used)}/{size(gpu.memory_total)}  {temp}{cores}")
            for process in sorted(snapshot.processes, key=lambda p: p.utilization or 0, reverse=True):
                usage = f"{process.utilization:.1f}%" if process.utilization is not None else "--"
                gpu_ms = f"{process.gpu_time_ms_s:.1f}ms/s" if process.gpu_time_ms_s is not None else "--"
                print(f"  {process.pid:>7}  {process.name:<28} {usage:>6}  {gpu_ms:>9}  "
                      f"{size(process.memory_used):>9}  {process.gpu_id}")
            for warning in snapshot.warnings:
                print(f"warning: {warning}", file=sys.stderr)
        return 0 if snapshot.gpus else 1
    try:
        from gputop.ui import run
        run(collector, args.interval)
    except ImportError as exc:
        print(f"Terminal UI unavailable: {exc}. On Windows install gputop[windows].", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
