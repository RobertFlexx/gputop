# Reading the numbers

GPU telemetry is uneven. A blank sensor on one card may work on another card with the same vendor, depending on the driver and OS. gputop leaves missing readings as `--` so an unavailable temperature cannot be mistaken for 0°C.

## Load and process time

| On screen | Meaning |
| --- | --- |
| GPU % | Device busy time from the driver, or the busiest engine when only engine counters are available. |
| Engine % | Activity for a named engine such as render, tiler, copy or video. Engine names come from the driver. |
| Process GPU % | On Apple and Linux, a rate calculated from GPU-time counters. On Windows, the busiest WDDM engine counter for that process. NVIDIA compute-process listings do not include a GPU percentage. Delta-based rates are blank on the first sample. |
| GPU ms/s | Milliseconds of GPU execution time charged to the process per second, when a source provides it. 100 ms/s is about 10% of one GPU-time stream. |
| GPU time | On Apple Silicon, cumulative GPU execution time recorded for the selected process's current Metal clients. |
| Core eq est | GPU core count × overall GPU busy fraction. Four core equivalents on an eight-core GPU means roughly half of its aggregate capacity was busy; it does **not** identify which four cores worked. |
| Core view (`c`) | Individual core percentages when the driver exposes them. `--` means there is no reading for that core. Arrow keys and Page Up/Page Down scroll longer lists. |

These counters use different sampling windows. The device percentage and the sum of process percentages will not always match, especially while load is changing. Short intervals are more responsive but noisier. The top line shows the requested period and the period actually achieved.

On the Apple Silicon Mac tested here, the AGX registry exposes a GPU core count and aggregate utilization but no live busy percentage for each physical shader core. The expanded core view shows the cores with `--` in that case. If another driver exposes named per-core utilization counters, the view, `--once`, and `--json` show those readings; `--doctor` reports the measured count. Apple offers [shader-core profiling in Xcode](https://developer.apple.com/documentation/xcode/analyzing-the-performance-of-your-metal-app/) for an application's own work; that is different from a system-wide per-core monitor.

## Memory and sensors

The memory bar shows allocated capacity when both used and total are known. Memory-controller activity is a different measurement: it remains available as `memory_utilization` in JSON and is not substituted for occupied capacity. Apple GPUs share system RAM, so gputop shows the in-use amount without inventing a separate VRAM limit. “GPU Mem” in the process table means GPU memory attributed to that process. The selected-process detail may also show **RAM**, which is ordinary system memory and a different reading.

Temperature, hotspot, memory temperature, power, power limit, fan and clock only appear if their source reports them. The JSON snapshot includes extra sensor fields even when the terminal is too narrow to show all of them.

## Device type and multiple GPUs

Device type is independent of vendor and display name. On Linux, `amdgpu` exposes an [APU/fusion flag](https://github.com/torvalds/linux/blob/master/include/uapi/drm/amdgpu_drm.h), which distinguishes AMD integrated GPUs from dedicated cards even when an APU reserves several GiB of memory. The query needs access to a DRM device node. For NVIDIA devices, the [CUDA integrated-device attribute](https://docs.nvidia.com/cuda/cuda-driver-api/cuda_driver_api/group__CUDA__TYPES.html) supplies the type, matched to `nvidia-smi` devices by PCI bus ID. Other Linux drivers may leave the type unknown when they do not provide usable classification metadata.

On macOS, [Metal's `hasUnifiedMemory`](https://developer.apple.com/documentation/metal/mtldevice/hasunifiedmemory) supplies memory architecture. Each GPU is queried independently, so a Mac can have both an integrated GPU and a dedicated AMD GPU. Older devices can fall back to System Profiler's explicit shared-memory or VRAM fields. A built-in Apple GPU on an Apple silicon Mac is also classified as integrated when Metal inventory is unavailable. On Windows, [DXCore's `IsIntegrated`](https://learn.microsoft.com/en-us/windows/win32/api/dxcore_interface/ne-dxcore_interface-dxcoreadapterproperty) supplies the classification, with a positive dedicated-adapter-memory property as a fallback for dedicated GPUs. If those APIs or properties are unavailable, gputop reports `unknown` rather than guessing from the name.

Hardware identities keep devices and their processes separate even when the GPU names or process PIDs repeat. When an older inventory source cannot be matched to a counter group, both remain visible separately; they may describe the same physical GPU. The source/ID fields explain the distinction. Shared Linux DRM clients can appear under more than one PID, but device totals count each client once. Linux engine percentages also account for the driver's engine-capacity field.

## Where readings come from

- **Apple Silicon:** `IOAccelerator` device statistics and `AGXDeviceUserClient` process counters. The latter report cumulative `accumulatedGPUTime` for each Metal client, which gputop samples twice to calculate a rate. This is an undocumented driver interface, so a macOS update could change it. The [metalps project](https://github.com/LoganBarnett/metalps) documents the same counters. `powermetrics` provides optional power, frequency and temperature values when run as root and when the OS reports them.
- **Intel and AMD Macs:** `IOAccelerator` statistics when present, matched to Metal registry IDs. A unique vendor match is a fallback when IDs cannot be matched; ambiguous counter groups remain separate.
- **Linux:** DRM/sysfs inventory and sensors. [DRM fdinfo](https://www.kernel.org/doc/html/latest/gpu/drm-usage-stats.html) supplies per-process engine time on supporting drivers; [amdgpu sysfs](https://docs.kernel.org/gpu/amdgpu/thermal.html) supplies busy, memory and hardware sensors. Reading other users' fdinfo may require permission.
- **NVIDIA:** [`nvidia-smi`](https://docs.nvidia.com/deploy/nvidia-smi/) supplies device readings and compute-process memory across supported operating systems. Other process types may appear through the native Linux or Windows collector.
- **Windows:** WDDM GPU Engine and GPU Process Memory counters, matched to DXCore inventory by LUID. When DXCore is unavailable and the fallback inventory cannot establish a match, gputop lists the counter group separately. The English counter paths may also be unavailable on localized Windows installations. [Microsoft's counter overview](https://learn.microsoft.com/en-us/windows/win32/direct3dtools/pix/articles/timing-captures/pix-timing-captures) explains the engine and memory counters.

## When something is missing

Run `python3 -m gputop --doctor` first. It shows detected GPUs, available utilities and which metrics each GPU supplied. A missing process on Linux may be a `/proc` permission issue; a missing NVIDIA device reading may mean `nvidia-smi` is not installed or cannot talk to the driver. On Apple Silicon, process percentages need two samples, so wait for the next refresh.

If a sensor stays blank, `--json` is useful when reporting it: it preserves the GPU ID, source and missing fields. Please include your OS, GPU model and driver version, but remove process names if they are private.
