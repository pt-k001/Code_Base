# TX Test Orchestration

Tools for automated Wi-Fi transmit tests on an OpenWrt device under test (DUT).
A headless orchestrator (`mediator.py`) sweeps channel and TX power combinations,
and for each one runs an iperf3 test while a capture station records the result.
Each part is a separate process that talks to the others over a simple
line-based JSON TCP protocol.

## Components

| File | Role | Default TCP port |
|------|------|------------------|
| `managers/mediator.py` | Orchestrator. Runs channel x txpower sweeps and drives the three managers below. | 9003 |
| `managers/ssh_manager.py` | Persistent SSH link to the DUT. Sets channel (`hostapd_cli`) and txpower (`iw`) live. | 9000 |
| `managers/iperf_manager.py` | Runs iperf3 tests and reports live Mbps and the final result. | 9001 |
| `managers/capture_manager.py` | Tkinter capture station. Records screen regions into a SQLite database. | 9002 |
| `managers/jump_tunnel.py` | GUI SSH port forward, for reaching a DUT behind a jump server. | n/a |
| `main.py` | Entry point. Opens `gui/gui.py`. | n/a |
| `gui/gui.py` | Minimal GUI launcher for any console manager script. | n/a |
| `core/core.py` | Shared infrastructure: folders, TOML config, logging, TCP protocol. | n/a |

```
mediator ──► ssh_manager     (radio SET / STATUS / APPLY)       ──► DUT
         ──► iperf_manager   (PERF START / STOP / STATUS / SET)
         ──► capture_manager (SESSION / START / STOP / NUM / STATUS)
```

## Requirements

- Python 3.11 or newer (`tomllib` is used for reading TOML)
- tkinter (for `gui.py`, `jump_tunnel.py`, `capture_manager.py`)
- An iperf3 binary for `iperf_manager.py`
- Python packages:

```
pip install paramiko opencv-python numpy mss Pillow
```

`ssh_manager.py` and `jump_tunnel.py` need `paramiko`. `capture_manager.py`
needs `opencv-python`, `numpy`, `mss` and `Pillow`. The other scripts use only
the standard library.

## Repository layout

```
.
├── core/
│   ├── __init__.py
│   └── core.py
├── managers/
│   ├── __init__.py
│   ├── mediator.py
│   ├── ssh_manager.py
│   ├── iperf_manager.py
│   ├── capture_manager.py
│   └── jump_tunnel.py
├── gui/
│   ├── __init__.py
│   └── gui.py
├── main.py
├── version.toml
├── bin/                      iperf3 binary (plus Cygwin DLLs on Windows)
└── Service/                  created at first run, not committed
    ├── Service_configs/<app>/<app>_config.toml
    ├── Service_logs/<app>/<app>_<timestamp>.log
    ├── Service_profiles/<app>/<app>_profiles.toml
    └── Service_DBs/DBs/<db_name>/<db_name>.db      (capture_manager)
```

Every app gets its own sub-folder under `Service/`. Folders and default config
files are created automatically on first start.

## Quick start

1. Install the requirements above and place the iperf3 binary in `bin/`.
2. Start the managers, each in its own terminal (or from the launcher GUI, `python main.py`):

   ```
   python managers/ssh_manager.py
   python managers/iperf_manager.py
   python managers/capture_manager.py
   ```

3. Edit the config files in `Service/Service_configs/<app>/` (DUT address and
   credentials, iperf3 command, ports). They are created on first run.
4. Start the orchestrator and run a sweep:

   ```
   python managers/mediator.py
   sweep check
   sweep show combos
   sweep start db my_first_run
   ```

### DUT behind a jump server

Start `managers/jump_tunnel.py`, fill in the jump host and the target (the DUT), press
Start, then set this in the `ssh_manager` config:

```
dut_host = "127.0.0.1"
dut_port = <tunnel local port>
```

Keep `dut_user` and `dut_pass` as the DUT's own credentials.

### Launcher GUI

`python main.py` opens `gui/gui.py`, which runs any manager script as a child process. Its output appears
in the log pane and whatever you type in the command bar goes to the script's
prompt. Up/Down recalls earlier commands.

## Controlling a manager over TCP

One command per line, JSON reply. Example:

```
echo STATUS | ncat 127.0.0.1 9000
echo "PERF STATUS" | ncat 127.0.0.1 9001
echo sweep status | ncat 127.0.0.1 9003
```

### ssh_manager

| Command | Description |
|---------|-------------|
| `SSHALIVE` | Is the SSH link up. |
| `STATUS` | Live channel and txpower of every configured radio. |
| `APPLY <radio> <chan> <txpower_dBm>` | Change channel if needed, set txpower, read back and verify. |

### iperf_manager

| Command | Description |
|---------|-------------|
| `PERF START` | Run the stored command. |
| `PERF STOP` | Stop the running test. |
| `PERF STATUS` | State, live Mbps and last result. |
| `PERF SET CMD <iperf3 args>` | Store a new command (`CLEAR` empties it). |

Last result states: `SUCCESS`, `ZERO_TRAFFIC`, `SERVER_STOPPED`, `SERVER_BUSY`,
`ERROR`, `STOPPED`.

### capture_manager

| Command | Description |
|---------|-------------|
| `SESSION <name>` | Set the session name (version is bumped if reused). |
| `DBNAME [<name>]` | Set the database name, or report the current one. |
| `START` / `STOP` | Start or stop capturing. `START` is refused if neither primary nor a secondary folder is enabled. |
| `NUM` | Total frame count. |
| `STATUS` | Full state. |

Save locations (Settings tab): **primary** (`Service/Service_DBs/DBs`) has its own checkbox,
and a secondary folder can be added. At least one must be on; Save Settings, the Start
button and `START` all refuse otherwise. Config keys: `primary_enabled`, `secondary_enabled`,
`secondary_dir`. `db_path` in `DBNAME` / `STATUS` replies is the secondary path when primary is off.

### mediator

```
sweep start [profile] [from <n>] db <name>
sweep stop | pause | resume
sweep confirm y|n
sweep status | check
sweep show combos [profile]
sweep profile list | show [name] | use <name> | new <name> | copy <src> <dst>
sweep profile set <param> <value>
sweep set <param> <value>
manual <ssh cmd> | manual perf <cmd>
```

`db <name>` is required on every `sweep start`. The mediator sends it to
capture_manager (`DBNAME`), checks that it was accepted, and only then starts
the sweep, so each run is stored in a database you named. Allowed characters:
letters, digits, `_` and `-` (max 64). If the capture DB cannot be set, the
sweep does not start.

After every sweep a summary (including the DB name) is written next to the run log as
`Service/Service_logs/mediator/lastrun_<timestamp>.toml`.

## Configuration

All tunables live in `Service/Service_configs/<app>/<app>_config.toml`. See the
docstring at the top of each script for the full list of keys. The DUT defaults
for `ssh_manager` can also be set with the environment variables `DUT_HOST`,
`DUT_PORT`, `DUT_USER` and `DUT_PASS`.

## Versioning

The repository has one version, stored in `version.toml`:

```
version = "1.0.1"
```

Edit that number by hand when you change something. `core.get_version()`
reads it, and every manager, `jump_tunnel.py`, `gui.py` and `core.py` report
the same value.

## Security notes

- Do not commit the `Service/` folder. Configs can contain credentials
  (`jump_tunnel` stores the jump password in plain text if "Remember password"
  is ticked), and the capture databases can be large.
- Replace the default `dut_pass` in `ssh_manager.py` before publishing, or
  supply it through `DUT_PASS`.
- `jump_tunnel` listens on `127.0.0.1` only.

Suggested `.gitignore`:

```
Service/
__pycache__/
*.pyc
```

## Working with Git

Main PC, after changing code:

```
git status
git add .
git commit -m "Describe the change"
git push
```

Any other PC:

```
git pull
```
