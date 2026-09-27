# gputop

An htop-style view of your GPUs. It shows load, memory, temperature and power when the driver reports them, plus the processes doing the work.

It runs on macOS, Linux and Windows. The Apple Silicon collector has been exercised on an M5 Mac. Linux and Windows have fixture tests, but need more testing on real hardware. [What each number means](docs/metrics.md) covers the platform details and known gaps.

Requires Python 3.10 or newer.

## Run it

From this checkout:

```sh
python3 -m gputop
```

To install the `gputop` command:

```sh
python3 -m pip install .
gputop
```

Windows needs the curses shim: `py -m pip install ".[windows]"`. Then run `py -m gputop` in Windows Terminal or PowerShell.

No GPU handy? `python3 -m gputop --demo` opens a sample dashboard.

## Controls

| Key | What it does |
| --- | --- |
| `Tab` or `←`/`→` | Pick a GPU |
| `a` | Show processes from every GPU |
| `c` | Expand or collapse the selected GPU's core view; use `↑`/`↓` or Page Up/Page Down to scroll |
| `s` | Cycle process sort order |
| `/` | Search process names and PIDs |
| `↑`/`↓` or `j`/`k` | Move through processes |
| `Space` | Pause sampling |
| `r` | Refresh now |
| `+`/`-` | Step through polling speeds |
| `1`–`6` | Pick a speed directly; press `?` to see the mapping |
| `q` | Quit |

The function-key bar at the bottom covers the common actions. Polling can run from 0.1 to 60 seconds; the top line shows both the requested speed and the interval actually achieved.

For scripts, use `--once` for a text snapshot or `--json` for structured data. Both take two samples so process rates have time to settle. `--interval 0.25` changes the sample period, and `--doctor` shows which sources and metrics were found.

## A few things to know

- `--` means the OS or driver did not provide that reading. It does not mean zero.
- Apple Silicon process GPU time comes from AGX driver counters and works without root on the macOS version tested here. GPU power and clock readings from `powermetrics` need root access.
- The Apple GPU core count is real; the “core eq est” number is an **estimate from total GPU load**. Press `c` to see individual core readings when the driver reports them. On the Apple Silicon Mac tested here, macOS reports only aggregate load, so the core view shows `--` for each core rather than inventing values.
- NVIDIA process data from `nvidia-smi` covers compute jobs. Linux DRM and Windows WDDM can also show graphics processes when their counters are available.

See [metric and platform notes](docs/metrics.md) if a value looks odd or a process is missing.

## Working on gputop

```sh
python3 -m unittest discover -s tests -v
python3 -m gputop --demo
python3 -m gputop --doctor
```

The code is standard-library Python, with `windows-curses` as the only Windows extra. [Development notes](docs/development.md) explain how the collectors fit together.

MIT licensed; see [LICENSE](LICENSE).
