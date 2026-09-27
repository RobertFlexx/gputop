from __future__ import annotations

import curses
import math
import re
import threading
import time
from collections import defaultdict, deque

from gputop.collectors.common import clamp, size
from gputop.collectors.manager import Collector
from gputop.model import GPU, Process, Snapshot

SPEEDS = (0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 5.0, 10.0, 30.0, 60.0)


class Sampler:
    def __init__(self, collector: Collector, interval: float) -> None:
        self.collector = collector
        self.interval = interval
        self.snapshot = Snapshot(warnings=["Collecting GPU data…"])
        self.revision = 0
        self.paused = False
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(
            target=self._loop, name="gputop-sampler", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=7)

    def refresh(self) -> None:
        self.wake.set()

    def _loop(self) -> None:
        previous_end: float | None = None
        while not self.stop.is_set():
            if not self.paused:
                started = time.monotonic()
                try:
                    snapshot = self.collector.collect()
                except Exception as exc:
                    snapshot = Snapshot(
                        warnings=[f"Collection failed: {type(exc).__name__}: {exc}"]
                    )
                ended = time.monotonic()
                snapshot.sample_interval_s = (
                    ended - previous_end if previous_end is not None else None
                )
                snapshot.collection_duration_s = ended - started
                previous_end = ended
                self.snapshot = snapshot
                self.revision += 1
                delay = max(0.0, self.interval - (ended - started))
            else:
                delay = self.interval
            self.wake.wait(delay)
            self.wake.clear()


class App:
    def __init__(self, screen: curses.window, sampler: Sampler) -> None:
        self.screen = screen
        self.sampler = sampler
        self.gpu_index = 0
        self.all_gpus = False
        self.sort_index = 0
        self.sorts = ("GPU%", "VRAM", "PID", "NAME")
        self.filter = ""
        self.searching = False
        self.show_help = False
        self.show_cores = False
        self.core_offset = 0
        self.row = 0
        self.offset = 0
        self.history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=28))
        self.seen_revision = -1
        self.colors = False
        try:
            curses.start_color()
            curses.use_default_colors()
            for pair, color in enumerate(
                (
                    curses.COLOR_CYAN,
                    curses.COLOR_GREEN,
                    curses.COLOR_YELLOW,
                    curses.COLOR_RED,
                    curses.COLOR_MAGENTA,
                    curses.COLOR_BLUE,
                ),
                1,
            ):
                curses.init_pair(pair, color, -1)
            self.colors = True
        except curses.error:
            pass
        curses.curs_set(0)
        screen.keypad(True)
        screen.timeout(100)

    def color(self, pair: int, bold: bool = False) -> int:
        return (curses.color_pair(pair) if self.colors else 0) | (
            curses.A_BOLD if bold else 0
        )

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width or not text:
            return
        try:
            self.screen.addnstr(y, x, text, max(0, width - x - 1), attr)
        except (curses.error, UnicodeError):
            pass

    def line(self, y: int, text: str = "", attr: int = 0) -> None:
        _, width = self.screen.getmaxyx()
        self.put(y, 0, text.ljust(max(0, width - 1)), attr)

    def bar(
        self, y: int, label: str, value: float | None, width: int, color: int
    ) -> None:
        value = clamp(value)
        prefix = f" {label:<8} "
        percent = f"{value:5.1f}%" if value is not None else "   -- "
        bar_width = max(0, width - len(prefix) - len(percent) - 4)
        filled = round(bar_width * value / 100) if value is not None else 0
        self.put(y, 0, prefix, self.color(1, True))
        self.put(y, len(prefix), "[", self.color(1))
        self.put(y, len(prefix) + 1, "█" * filled, self.color(color, True))
        self.put(y, len(prefix) + 1 + filled, "·" * (bar_width - filled), self.color(6))
        self.put(
            y, len(prefix) + 1 + bar_width, "] " + percent, self.color(color, True)
        )

    def _selected_gpu(self, snapshot: Snapshot) -> GPU | None:
        if not snapshot.gpus:
            return None
        self.gpu_index %= len(snapshot.gpus)
        return snapshot.gpus[self.gpu_index]

    def _processes(self, snapshot: Snapshot, gpu: GPU | None) -> list[Process]:
        processes = snapshot.processes
        if gpu and not self.all_gpus:
            processes = [p for p in processes if p.gpu_id == gpu.id]
        if self.filter:
            term = self.filter.casefold()
            processes = [
                p for p in processes if term in p.name.casefold() or term in str(p.pid)
            ]
        if self.sort_index == 0:
            return sorted(
                processes,
                key=lambda p: (
                    p.utilization
                    if p.utilization is not None
                    else p.gpu_time_ms_s / 10 if p.gpu_time_ms_s is not None else -1
                ),
                reverse=True,
            )
        if self.sort_index == 1:
            return sorted(
                processes,
                key=lambda p: p.memory_used if p.memory_used is not None else -1,
                reverse=True,
            )
        if self.sort_index == 2:
            return sorted(processes, key=lambda p: p.pid)
        return sorted(processes, key=lambda p: p.name.casefold())

    def _core_rows(self, gpu: GPU) -> list[tuple[str, float | None]]:
        readings = {}
        for name, value in gpu.core_utilization.items():
            match = re.fullmatch(r"(?:GPU|Shader) Core (\d+) Utilization %", name)
            if match:
                readings[int(match.group(1))] = value
        if gpu.core_count:
            first = 0 if 0 in readings else 1
            return [
                (f"Core {index:02d}", readings.get(index))
                for index in range(first, first + gpu.core_count)
            ]
        return [(name, value) for name, value in sorted(gpu.core_utilization.items())]

    def _draw_cores(self, gpu: GPU | None, y: int, height: int, width: int) -> None:
        if gpu is None:
            self.line(y, " No GPU selected", self.color(3))
            return
        cores = self._core_rows(gpu)
        self.line(y, f" GPU cores ({len(cores)})  c collapse", self.color(1, True))
        y += 1
        measured = sum(value is not None for _, value in cores)
        if measured == 0:
            note = " Per-core usage unavailable; -- means no reading."
        else:
            note = f" Measured per-core utilization: {measured}/{len(cores)}"
        self.line(y, note, self.color(3))
        y += 1
        if not cores:
            self.line(
                y, " Core count and per-core readings unavailable.", self.color(3)
            )
            return
        cell_width = 26
        columns = max(1, width // cell_width)
        total_rows = math.ceil(len(cores) / columns)
        visible_rows = max(0, height - 3 - y)
        self.core_offset = min(
            max(0, self.core_offset), max(0, total_rows - visible_rows)
        )
        bar_width = cell_width - 19
        for row in range(
            self.core_offset, min(total_rows, self.core_offset + visible_rows)
        ):
            for col in range(columns):
                index = row * columns + col
                if index >= len(cores):
                    break
                label, value = cores[index]
                value = clamp(value)
                filled = round(bar_width * value / 100) if value is not None else 0
                bar = (
                    ("█" * filled + "·" * (bar_width - filled))
                    if value is not None
                    else "-" * bar_width
                )
                percent = f"{value:5.1f}%" if value is not None else "   -- "
                self.put(
                    y + row - self.core_offset,
                    col * cell_width,
                    f" {label:<7} [{bar}] {percent}",
                    self.color(2 if value is not None else 6),
                )
        if total_rows > visible_rows:
            self.line(
                height - 3,
                f" Core rows {self.core_offset + 1}-{min(total_rows, self.core_offset + visible_rows)}"
                f"/{total_rows}  ↑↓/PgUp/PgDn scroll",
                self.color(3),
            )

    def draw(self) -> None:
        self.screen.erase()
        snapshot = self.sampler.snapshot
        height, width = self.screen.getmaxyx()
        if height < 17 or width < 54:
            self.put(
                0, 0, "gputop needs at least 54 columns × 17 rows", self.color(3, True)
            )
            self.screen.refresh()
            return
        gpu = self._selected_gpu(snapshot)
        if self.seen_revision != self.sampler.revision:
            self.seen_revision = self.sampler.revision
            for item in snapshot.gpus:
                self.history[item.id].append(item.utilization or 0)
        self.line(
            0,
            " gputop  GPU process monitor".ljust(width - 20)
            + time.strftime("%H:%M:%S"),
            self.color(1, True),
        )
        actual = (
            f"/{snapshot.sample_interval_s:.2f}s"
            if snapshot.sample_interval_s is not None
            else ""
        )
        state = (
            "PAUSED"
            if self.sampler.paused
            else f"poll {self.sampler.interval:g}s{actual}"
        )
        self.line(
            1,
            f" {len(snapshot.gpus)} GPU  {len(snapshot.processes)} proc  {state}  "
            f"sort {self.sorts[self.sort_index]}  {'all' if self.all_gpus else 'selected'}",
            self.color(2),
        )
        y = 2
        if not snapshot.gpus:
            self.line(y, " No GPUs found yet", self.color(3, True))
            y += 1
            for warning in snapshot.warnings[:2]:
                self.line(y, " " + warning, self.color(3))
                y += 1
        else:
            slots = min(len(snapshot.gpus), max(1, min(4, height - 16)))
            start = min(
                max(0, self.gpu_index - slots + 1), max(0, len(snapshot.gpus) - slots)
            )
            for index in range(start, start + slots):
                item = snapshot.gpus[index]
                util = (
                    f"{item.utilization:5.1f}%"
                    if item.utilization is not None
                    else "   -- "
                )
                temp = (
                    f"{item.temperature:.0f}°C"
                    if item.temperature is not None
                    else "--"
                )
                marker = "▶" if index == self.gpu_index else " "
                title = f" {marker} {index} {item.name[:max(10, width - 37)]:<{max(10, width - 37)}} {util:>6}  {temp:>4}"
                self.line(
                    y,
                    title,
                    self.color(
                        1 if index == self.gpu_index else 6, index == self.gpu_index
                    ),
                )
                y += 1
            if gpu:
                self.line(
                    y,
                    f" ─ {gpu.vendor}  {gpu.kind}  {gpu.driver or gpu.source}  {gpu.id} ",
                    self.color(5),
                )
                y += 1
                self.bar(
                    y,
                    "GPU",
                    gpu.utilization,
                    width,
                    2 if (gpu.utilization or 0) < 80 else 3,
                )
                y += 1
                if self.show_cores:
                    estimate = (
                        f"  ·  {gpu.core_equivalent_load:.1f}/{gpu.core_count} core eq est"
                        if gpu.core_equivalent_load is not None and gpu.core_count
                        else ""
                    )
                    self.line(y, f" Aggregate GPU load{estimate}", self.color(2))
                    y += 1
                else:
                    self.bar(
                        y,
                        "Memory",
                        (
                            gpu.memory_utilization
                            if gpu.memory_utilization is not None
                            else (
                                gpu.memory_used / gpu.memory_total * 100
                                if gpu.memory_total and gpu.memory_used is not None
                                else None
                            )
                        ),
                        width,
                        5,
                    )
                    y += 1
                    mem_label = f"{size(gpu.memory_used)}/{size(gpu.memory_total)}"
                    fields = [
                        f"{'Unified' if gpu.vendor == 'Apple' else 'VRAM'} {mem_label}"
                    ]
                    if gpu.core_count is not None:
                        core_label = f"{gpu.core_count} cores"
                        if gpu.core_equivalent_load is not None:
                            core_label += f" / {gpu.core_equivalent_load:.1f} eq est"
                        fields.append(core_label)
                    if gpu.temperature is not None:
                        fields.append(f"{gpu.temperature:.0f}°C")
                    if gpu.power_w is not None:
                        power = (
                            f"{gpu.power_w:.0f}/{gpu.power_limit_w:.0f}W"
                            if gpu.power_limit_w is not None
                            else f"{gpu.power_w:.0f}W"
                        )
                        fields.append(power)
                    if gpu.clock_mhz is not None:
                        fields.append(f"{gpu.clock_mhz:.0f}MHz")
                    if gpu.fan_percent is not None:
                        fields.append(f"{gpu.fan_percent:.0f}% fan")
                    self.line(y, "  " + "  ·  ".join(fields), self.color(2))
                    y += 1
                    engines = "   ".join(
                        f"{name} {value:.0f}%"
                        for name, value in list(gpu.engines.items())[:5]
                    )
                    self.line(y, " Engines  " + (engines or "--"), self.color(3))
                    y += 1
                    history = "".join(
                        "▁▂▃▄▅▆▇█"[min(7, int(v / 12.5))] for v in self.history[gpu.id]
                    )
                    if gpu.core_utilization:
                        cores = "  ".join(
                            f"{name} {value:.0f}%"
                            for name, value in list(gpu.core_utilization.items())[:6]
                        )
                        self.line(y, " Cores  " + cores, self.color(2))
                    else:
                        self.line(y, " History  " + history, self.color(2))
                    y += 1
        y += 1
        if self.show_cores:
            self._draw_cores(gpu, y, height, width)
            if self.searching:
                self.line(
                    height - 2, " Search: " + self.filter + "_", self.color(3, True)
                )
            elif self.show_help:
                self.line(
                    height - 2,
                    " c cores  Tab GPU  ↑↓/PgUp/PgDn scroll  +/- speed  Space pause  q quit",
                    self.color(3),
                )
            else:
                self.line(
                    height - 2,
                    " c cores  Tab GPU  ↑↓/PgUp/PgDn scroll  Space pause  ? help  q quit",
                    self.color(6),
                )
            self.line(
                height - 1, " F1 Help    F5 Refresh    F10 Quit", curses.A_REVERSE
            )
            self.screen.refresh()
            return
        processes = self._processes(snapshot, gpu)
        self.line(
            y,
            f" Processes ({len(processes)})"
            + (f"  filter: {self.filter}" if self.filter else ""),
            self.color(1, True),
        )
        y += 1
        self.line(
            y,
            f" {'PID':>7}  {'GPU%':>6}  {'GPU ms/s':>8}  {'GPU Mem':>10}  {'Engine':<12} Name",
            self.color(6, True),
        )
        y += 1
        rows = max(0, height - y - 3)
        self.row = min(max(0, self.row), max(0, len(processes) - 1))
        if self.row < self.offset:
            self.offset = self.row
        if self.row >= self.offset + rows and rows:
            self.offset = self.row - rows + 1
        self.offset = min(self.offset, max(0, len(processes) - rows))
        for index, process in enumerate(
            processes[self.offset : self.offset + rows], self.offset
        ):
            util = (
                f"{process.utilization:5.1f}%"
                if process.utilization is not None
                else "   -- "
            )
            gpu_ms = (
                f"{process.gpu_time_ms_s:8.1f}"
                if process.gpu_time_ms_s is not None
                else "      --"
            )
            label = (
                f" {process.pid:>7}  {util:>6}  {gpu_ms:>8}  {size(process.memory_used):>10}  "
                f"{process.engine[:12]:<12} {process.name}"
            )
            self.line(
                y + index - self.offset,
                label,
                curses.A_REVERSE if index == self.row else 0,
            )
        if not processes and y < height - 3:
            empty_note = (
                gpu.notes[-1]
                if gpu and gpu.notes
                else "No process data. Some drivers restrict process telemetry."
            )
            self.line(y, " " + empty_note, self.color(3))
        note = (
            snapshot.warnings[0]
            if snapshot.warnings
            else (gpu.notes[-1] if gpu and gpu.notes else "")
        )
        if processes and self.row < len(processes):
            chosen = processes[self.row]
            if chosen.gpu_time_total_ns is not None:
                total = chosen.gpu_time_total_ns / 1e9
                elapsed = (
                    f"{chosen.last_active_seconds:.1f}s ago"
                    if chosen.last_active_seconds is not None
                    else "--"
                )
                ram = (
                    f"  RAM {size(chosen.system_memory_used)}"
                    if chosen.system_memory_used is not None
                    else ""
                )
                note = (
                    f"PID {chosen.pid}  GPU time {total:.1f}s  "
                    f"{chosen.queue_count or 0} Metal queue(s)  last work {elapsed}{ram}"
                )
            elif chosen.system_memory_used is not None:
                note = (
                    f"PID {chosen.pid}  GPU memory {size(chosen.memory_used)}  "
                    f"RAM {size(chosen.system_memory_used)}  source {chosen.source}"
                )
        if self.show_help:
            note = (
                "Speeds: 1=0.1s  2=0.25s  3=0.5s  4=1s  5=1.5s  6=2s; +/- step further"
            )
        self.line(height - 3, " " + note, self.color(3))
        if self.searching:
            self.line(height - 2, " Search: " + self.filter + "_", self.color(3, True))
        elif self.show_help:
            self.line(
                height - 2,
                " Tab GPU  c cores  a all  s sort  / search  ↑↓ select  +/- speed  Space pause  q quit",
                self.color(3),
            )
        else:
            self.line(
                height - 2,
                " Tab GPU  c cores  a all  s sort  / search  ↑↓ select  +/- speed  ? help  q quit",
                self.color(6),
            )
        self.line(
            height - 1,
            " F1 Help    F2 Sort    F3 Search    F4 All GPUs    F5 Refresh    F10 Quit",
            curses.A_REVERSE,
        )
        self.screen.refresh()

    def key(self, key: int) -> bool:
        if self.searching:
            if key in (10, 13, 27):
                self.searching = False
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.filter = self.filter[:-1]
            elif 32 <= key < 127:
                self.filter += chr(key)
            self.row = self.offset = 0
            return True
        if key in (ord("q"), ord("Q"), curses.KEY_F10):
            return False
        if key in (9, curses.KEY_RIGHT):
            self.gpu_index += 1
            self.row = self.offset = 0
            self.core_offset = 0
        elif key == curses.KEY_LEFT:
            self.gpu_index -= 1
            self.row = self.offset = 0
            self.core_offset = 0
        elif key in (ord("c"), ord("C")):
            self.show_cores = not self.show_cores
            self.core_offset = 0
        elif key in (ord("a"), curses.KEY_F4):
            self.all_gpus = not self.all_gpus
            self.row = self.offset = 0
        elif key in (ord("s"), curses.KEY_F2):
            self.sort_index = (self.sort_index + 1) % len(self.sorts)
            self.row = self.offset = 0
        elif key in (ord("/"), curses.KEY_F3):
            self.searching = True
        elif key in (ord("?"), curses.KEY_F1):
            self.show_help = not self.show_help
        elif key in (curses.KEY_DOWN, ord("j")):
            if self.show_cores:
                self.core_offset += 1
            else:
                self.row += 1
        elif key in (curses.KEY_UP, ord("k")):
            if self.show_cores:
                self.core_offset = max(0, self.core_offset - 1)
            else:
                self.row -= 1
        elif key == curses.KEY_NPAGE:
            if self.show_cores:
                self.core_offset += 8
            else:
                self.row += 10
        elif key == curses.KEY_PPAGE:
            if self.show_cores:
                self.core_offset = max(0, self.core_offset - 8)
            else:
                self.row -= 10
        elif key == ord(" "):
            self.sampler.paused = not self.sampler.paused
            self.sampler.refresh()
        elif key in (ord("r"), curses.KEY_F5):
            self.sampler.refresh()
        elif key in (ord("+"), ord("=")):
            self.sampler.interval = max(
                (speed for speed in SPEEDS if speed < self.sampler.interval),
                default=SPEEDS[0],
            )
            self.sampler.refresh()
        elif key == ord("-"):
            self.sampler.interval = next(
                (speed for speed in SPEEDS if speed > self.sampler.interval), SPEEDS[-1]
            )
            self.sampler.refresh()
        elif ord("1") <= key <= ord("6"):
            self.sampler.interval = SPEEDS[key - ord("1")]
            self.sampler.refresh()
        self.row = max(0, self.row)
        return True

    def loop(self) -> None:
        self.sampler.start()
        try:
            drawn_revision = -1
            drawn_second = -1
            dirty = True
            while True:
                second = int(time.time())
                if (
                    dirty
                    or self.sampler.revision != drawn_revision
                    or second != drawn_second
                ):
                    self.draw()
                    drawn_revision = self.sampler.revision
                    drawn_second = second
                    dirty = False
                key = self.screen.getch()
                if key != -1:
                    if not self.key(key):
                        break
                    dirty = True
        finally:
            self.sampler.close()


def run(collector: Collector, interval: float) -> None:
    curses.wrapper(lambda screen: App(screen, Sampler(collector, interval)).loop())
