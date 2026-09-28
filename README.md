# gputop

An htop-style view of your GPUs. It shows load, memory, temperature and power when the driver reports them, plus the processes doing the work.

It runs on macOS, Linux and Windows. The Apple Silicon collector has been exercised on an M5 Mac. Linux and Windows have fixture tests, but need more testing on real hardware. [What each number means](docs/metrics.md) covers the platform details and known gaps.

Requires Python 3.10 or newer.

## Install

macOS, Linux and BSD:

```sh
curl -fsSL https://raw.githubusercontent.com/RobertFlexx/gputop/main/install.sh | sh
```

Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/RobertFlexx/gputop/main/install.ps1 | iex
```

You can literally also:

```sh
pipx install .
```
In the repo root.

> (if you have pipx.)


---------------------------------

Either installer puts a private Python environment and a `gputop` command in your user directories, never system ones, and needs no administrator rights. Run the shell installer again to update or remove a detected install; it checks the active launcher, including custom install directories, and shows a menu even when run through `curl | sh`. Use `--yes` for unattended installs.

Useful flags, and the environment variables that do the same thing when the script is piped:

| Flag | Environment | What it does |
| --- | --- | --- |
| `-i`, `--install` | | Install gputop, the default |
| `-u`, `--update` | | Update an install made by the script |
| `--uninstall` | | Remove an install made by the script |
| `-m pip`, `-m uv` | `GPUTOP_METHOD` | Choose the installer; `uv` can also provide Python |
| `--dir PATH` | `GPUTOP_DIR` | Where the environment, source and state live |
| `--bin-dir PATH` | `GPUTOP_BIN_DIR` | Where the `gputop` command is linked |
| `--python PATH` | `GPUTOP_PYTHON` | Use a specific interpreter |
| `--ref REF` | `GPUTOP_REF` | Branch, tag or commit to install, `main` by default |
| `--source PATH` | `GPUTOP_SOURCE` | Install from a local checkout |
| `--dry-run` | | Print the plan and change nothing |
| `-y`, `--yes` | `GPUTOP_YES` | Never prompt |
| `--no-path` | | Do not offer to change the shell profile or user PATH |

Run the downloaded script with `--help` for the full list. `curl` and `wget` are used for downloads, `git` when it is installed, and `tar` for release archives; a source archive is fetched when `git` is missing.

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

Windows needs the curses shim: `py -m pip install ".[windows]"`. Then run `py -m gputop` in Windows Terminal or PowerShell. The PowerShell installer already installs that extra for you.

No GPU handy? `python3 -m gputop --demo` opens a sample dashboard.

## Controls

| Key | What it does |
| --- | --- |
| `Tab` or `←`/`→` | Pick a GPU |
| `a` | Show processes from every GPU |
| `c` | Expand or collapse the selected GPU's core view; use `↑`/`↓` or Page Up/Page Down to scroll |
| `s` | Cycle process sort order |
| `/` | Search process names and PIDs; Enter applies, Esc cancels |
| `↑`/`↓` or `j`/`k` | Move through processes |
| `x` or `F9` | Confirm termination of the selected process |
| `X` | Confirm an immediate kill of the selected process |
| `Space` | Pause sampling |
| `r` | Refresh now, including while paused |
| `+`/`-` | Step through polling speeds |
| `1`–`6` | Pick a speed directly; press `?` to see the mapping |
| `q` | Quit |

The function-key bar at the bottom covers the common actions. Polling can run from 0.1 to 60 seconds; the top line shows both the requested speed and the interval actually achieved.

Process signals require confirmation and the same permissions as the current user. On Unix, `x` sends `SIGTERM` and `X` sends `SIGKILL`. On Windows, both keys terminate the process immediately.

For scripts, use `--once` for a text snapshot or `--json` for structured data. Both take two samples so process rates have time to settle. `--interval 0.25` changes the sample period, and `--doctor` shows which sources and metrics were found.

## A few things to know

- `--` means the OS or driver did not provide that reading. It does not mean zero.
- Apple Silicon process GPU time comes from AGX driver counters and works without root on the macOS version tested here. GPU power and clock readings from `powermetrics` need root access.
- The Apple GPU core count is real; the “core eq est” number is an **estimate from total GPU load**. Press `c` to see individual core readings when the driver reports them. On the Apple Silicon Mac tested here, IOKit reports only aggregate load, so the core view shows `--` for each core. `--doctor` reports how many individual cores have measured readings, and `--json` exposes those readings in `core_utilization`.
- NVIDIA process data from `nvidia-smi` covers compute jobs. Linux DRM and Windows WDDM can also show graphics processes when their counters are available.
- Integrated versus dedicated detection uses driver metadata: AMD's APU flag on Linux, CUDA's integrated attribute for NVIDIA GPUs, Metal's memory architecture on macOS, and DXCore on Windows. GPU model names and VRAM-size thresholds do not determine the type. If the source cannot establish it, the type is `unknown`; `--doctor` shows the evidence used.
- Multiple GPUs are tracked by hardware identifiers where available, including GPUs with identical names. The selection follows the device when inventory order changes. Expensive inventory discovery is cached for up to 60 seconds, so newly connected devices may take that long to appear with their full details.

See [metric and platform notes](docs/metrics.md) if a value looks odd or a process is missing.

## Working on gputop

```sh
python3 -m unittest discover -s tests -v
python3 -m gputop --demo
python3 -m gputop --doctor
```

The code is standard-library Python, with `windows-curses` as the only Windows extra. [Development notes](docs/development.md) explain how the collectors fit together.

MIT licensed; see [LICENSE](LICENSE).
