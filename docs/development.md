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
| `hardware.py` | Shared hardware identity and PCI vendor identifiers |
| `amdgpu.py` | Linux AMD integrated-device flag via a bounded DRM information query |
| `cuda.py` | NVIDIA integrated-device flag via the CUDA driver API |
| `metal.py` | macOS registry IDs and unified-memory properties |
| `dxcore.py` | Windows LUIDs, vendor IDs and integrated-device properties |

Each OS collector owns its sampling state; the manager owns source merging. Shared parsing and command execution are functions in `common.py`. The small native API readers are isolated from metric parsing so failure to read optional inventory does not prevent collecting other available measurements. They use the standard library and release their native resources.

## Collector rules

Return `None` for a missing measurement. Zero means the source measured zero. Keep a sensor's unit in its field name or convert it at collection time: memory is bytes, power is watts, temperature is °C, clocks are MHz, and cumulative GPU time is nanoseconds.

Some counters are cumulative. Keep the previous value in the collector, compare it with the current value, then convert using elapsed monotonic time. Calculate the conversion factor once per sample. A new macOS client or a counter reset has no rate until the next valid sample. Linux DRM counters can temporarily decrease: retain the previous high-water value until the counter catches up, as specified by the [fdinfo ABI](https://docs.kernel.org/gpu/drm-usage-stats.html). Honor engine capacity and count shared clients once in device totals.

Use a stable device or client ID when merging data. Registry IDs identify macOS devices, LUIDs identify Windows adapters, and PCI addresses identify Linux/NVIDIA devices. Match by name only when the correspondence is unambiguous in both inventories. Preserve unmatched counter groups separately rather than attributing their work to another GPU. A PID can have independent readings on several GPUs.

Read device names and integrated/dedicated properties from the driver or OS. Do not add model-name lists or infer device type from vendor, bus placement, or memory size. ABI field IDs, PCI vendor IDs, and unit conversions are named constants, not product-specific detection rules. The `extras.kind_source` field records classification evidence; unknown types remain `unknown`.

External utilities can fail or take too long. `common.command` applies a timeout and returns `None` on failure. A collector should still return whatever it can read from other sources. Avoid adding an expensive command to every 0.1-second poll; cache slow inventory reads or sample them less often.

Commands use argument lists without a shell, cannot consume terminal input, and tolerate invalid UTF-8 output. Human-readable output filters terminal control characters from device/process names and driver messages. JSON retains the original strings with JSON escaping. Never interpolate telemetry into executable commands.

## Tests and checks

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q gputop tests
python3 -m gputop --demo
python3 -m gputop --interval 0.25 --doctor
```

The tests use small OS-output fixtures so they run without a GPU. A fixture proves the parser and merge behavior, not the driver behavior on every machine. If you add a collector path, include a fixture for an ordinary reading and for the failure or missing-field case that path is meant to handle.

Regression coverage includes AMD integrated/dedicated metadata, repeated GPU names, multiple adapters, shared DRM clients, counter resets, missing and zero readings, malformed payloads, terminal escape characters, and sampler wakeups. Native API fixtures also check resource cleanup and the AMD ioctl buffer layout. Linux AMD and Windows DXCore still need testing on physical machines; the Metal path has been exercised on an Apple M5.
