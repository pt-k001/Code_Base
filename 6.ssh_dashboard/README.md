# SSH Dashboard

Tkinter tool that turns the log written by **SSH Log** (`../ssh_log`) into live,
zoomable charts: CPU load, memory, uptime / load average and per-device Wi-Fi
throughput, plus a plugin system for your own metrics. Any file in the
`[record]` format below works, whichever tool wrote it.

## Features

- One plugin per metric in `modules/`. Drop in a file, press **Rescan modules/**, get a chart.
- Auto-refresh (30 sec / 1 min / 5 min) and time windows from 3 minutes to 7 days.
- Per-device chart sets for grouped metrics (one set per Wi-Fi MAC, created on demand).
- Mouse-wheel zoom, Shift+wheel pan, Ctrl for fine zoom, right-click reset, hover values, PNG export.
- Parsed records are stored in SQLite, so history survives restarts.
- Excel export (one sheet per module), save a database snapshot, open one later without the log.
- Large logs are read incrementally and committed in batches.

## Requirements

- Python 3.11 or newer
- tkinter (Linux: `sudo apt install python3-tk`)
- `pip install -r requirements.txt` (matplotlib, pandas, openpyxl)

## Run

```
python main.py
```

## Usage

1. **Browse...** and select a log file, then **Load**. Loading clears the previous data and parses the file from the start.
2. **Refresh Now** reads only what was appended since the last read. **Window** changes the time span of every chart. **Stop/Start Auto-Refresh** pauses the timer.
3. The tree on the left shows or hides single charts or a whole module. Hidden charts are still parsed and stored.
4. Charts: wheel = zoom Y, Shift+wheel = move X, Ctrl+wheel = zoom instead of move, right-click or ⟲ = reset, ⛶ = enlarge, 💾 = save PNG.
5. **Export to Excel**, **Save Database** and the PNG button write to `<logdir>/<logname>/` (fallback: next to the app's database if that folder is not writable).
6. **Load Database** opens a saved `*_metrics.sqlite` without its log. Auto-refresh is paused for it. **Load** a log again to return to live data.
7. **Settings** tab: enable/disable modules (remembered), **Rescan modules/** after adding or editing a module file. A module with an error shows it next to its name.

Troubleshooting: "No data in this window" means no samples fall in the selected window, so try a longer one. If auto-refresh sees nothing new, check that the log is still growing at the path shown.

## Repository layout

```
.
├── main.py                 entry point (loads config, starts the window)
├── ui.py                   tkinter interface (App)
├── core/
│   ├── core.py             config loading, version, Service/ paths
│   ├── module_base.py      MetricModule / ChartSpec contract
│   ├── module_loader.py    discovers and hot-reloads modules/
│   ├── data_feed.py        incremental log reader and record parser
│   ├── db.py               per-module SQLite storage
│   ├── chart_engine.py     zoomable chart widget
│   └── excel_exporter.py   one sheet per module
├── modules/                cpu_stat, meminfo, proc_uptime, uptime, wifi_station
├── version.toml
├── requirements.txt
└── Service/
    ├── Service_configs/ssh_dashboard/ssh_dashboard_config.toml   all tunables
    └── Service_profiles/ssh_dashboard/                           created at first run
        ├── metrics.sqlite
        └── module_state.json
```

## Log format

```
[record]
time=2026-08-13T10:15:00
host=192.168.1.50
cmd=cat /proc/stat
tag=cpu
---
<command output>
[/record]
```

`time` and `cmd` are required. Output lines escaped as `\[record]` / `\[/record]` by SSH Log
are restored before a module sees them. Do not change the field names or the `---` separator
without changing `SSH Log` as well.

## Built-in modules

| Module | Command | Charts |
|---|---|---|
| `cpu_stat` | `cat /proc/stat` | CPU load % from jiffy deltas |
| `meminfo` | `cat /proc/meminfo` | Memory used % (total minus free) |
| `proc_uptime` | `cat /proc/uptime` | Uptime and cumulative idle seconds |
| `uptime` | `uptime` | Uptime, load average 1/5/15 min |
| `wifi_station` | `iw dev <iface> station dump` | Per device: RX/TX Mbps, signal, connected time |

## Writing a module

One file in `modules/` with a module-level `MODULE`:

```python
from core.module_base import MetricModule, ChartSpec


class MyModule(MetricModule):
    id = "my_metric"
    name = "My Metric"
    db_schema = {"value": "REAL"}
    chart_specs = [ChartSpec(key="value", title="My Metric", ylabel="units", value_col="value")]

    def matches(self, cmd):
        return cmd == "my custom command"

    def parse(self, body, ts):
        return {"value": float(body.strip())}


MODULE = MyModule()
```

- `id` is stable and names the table `mod_<id>`. `db_schema` maps column to SQLite type; `time` is added for you.
- `parse` returns a dict, a list of dicts, or `None` to skip. Its keys must match `db_schema`.
- `ChartSpec`: `ylim=(lo, hi)` fixes the range, `group_by="col"` draws one chart per value of that column, and `{group}` in the title is replaced by it.
- Optional hooks: `excel_rename()`, `log_line(row)`, `reset()` (called when a new log is loaded).

## Configuration

Every tunable is in `Service/Service_configs/ssh_dashboard/ssh_dashboard_config.toml`
(refresh options, time windows, chart sizes, inactivity limit, window size). A missing
section or key stops the app with a message naming it.

Folders chosen in file dialogs are remembered in the `[paths]` section of the same file
(last log file, database file and chart image). The app rewrites only those keys.

## Versioning

One version for the app, stored in `version.toml` and shown in the window title:

```
version = "1.0.0"
```

## Notes

- Log files and databases contain whatever the logged commands printed, so treat them as sensitive.
- Do not commit `Service/Service_profiles/` (see `.gitignore`).
