# Working on the collectors

The UI reads one `Snapshot` at a time from [`model.py`](../gputop/model.py). [`manager.py`](../gputop/collectors/manager.py) asks the native OS collector for GPUs and processes, then merges NVIDIA data when `nvidia-smi` is available. [`ui.py`](../gputop/ui.py) samples in a background thread so the terminal stays responsive while a utility runs.

The collectors live in `gputop/collectors/`:

| File | Source |
| --- | --- |
| `macos.py` | System Profiler, IOKit, optional powermetrics |
| `linux.py` | DRM sysfs and process fdinfo |
| `windows.py` | PowerShell/WDDM performance counters |
| `nvidia.py` | nvidia-smi |
| `common.py` | Command execution, number parsing and size formatting |

## Collector rules

Return `None` for a missing measurement. Zero means the source measured zero. Keep a sensor's unit in its field name or convert it at collection time: memory is bytes, power is watts, temperature is °C, clocks are MHz, and cumulative GPU time is nanoseconds.

Some counters are cumulative. Keep the previous value in the collector, compare it with the current value, then divide by elapsed monotonic time. A new client or a counter reset has no rate until the next sample. Use a stable device or client ID when merging data, and leave ambiguous matches alone.

External utilities can fail or take too long. `common.command` applies a timeout and returns `None` on failure. A collector should still return whatever it can read from other sources. Avoid adding an expensive command to every 0.1-second poll; cache slow inventory reads or sample them less often.

## Tests and checks

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q gputop tests
python3 -m gputop --demo
python3 -m gputop --interval 0.25 --doctor
```

The tests use small OS-output fixtures so they run without a GPU. A fixture proves the parser and merge behavior, not the driver behavior on every machine. If you add a collector path, include a fixture for an ordinary reading and for the failure or missing-field case that path is meant to handle.
