# SSH Log

Tkinter tool that runs commands on one or more devices over SSH on a schedule and
records the output to a log file. The log is the input of the **SSH Dashboard**
(`../ssh_dashboard`), which turns it into charts.

## Features

- One tab per device, each with its own connection, commands and log file.
- Per-command interval (seconds) and optional tag. "Run Once" for a quick test.
- Auto-reconnect with a watchdog. Logging resumes after the link is back.
- Start a new log file, or continue an existing one.
- Live log viewer with filter, highlight, auto-scroll and bounded memory use.
- Profiles: host, user, commands and reconnect settings.
- Password or private-key login (an encrypted key's passphrase goes in the Password field).

## Requirements

- Python 3.11 or newer
- tkinter (Linux: `sudo apt install python3-tk`)
- `pip install -r requirements.txt` (paramiko)

## Run

```
python main.py
```

## Repository layout

```
.
├── main.py                 entry point (loads config, starts the window)
├── ui.py                   tkinter interface (App, ConnectionPane, CommandRow, StatsBar)
├── core/
│   ├── core.py             config loading, version, Service/ paths
│   └── ssh_core.py         validators, SSH, host-key store, profiles, logger, runner
├── version.toml
├── requirements.txt
└── Service/
    ├── Service_configs/ssh_log/ssh_log_config.toml    all tunables
    └── Service_profiles/ssh_log/                      created at first run
        ├── profiles.json
        └── known_hosts
```

## Log format

Each result is appended as one block. The dashboard parses exactly this:

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

Output lines that equal `[record]` or `[/record]` are prefixed with `\` so remote
output cannot forge a record. Do not change the field names or the `---` separator
without changing the dashboard's `core/data_feed.py` as well.

## Configuration

Every tunable is in `Service/Service_configs/ssh_log/ssh_log_config.toml`
(timeouts, output size limit, interval limits, reconnect defaults, viewer limits,
window size). A missing section or key stops the app with a message naming it.

The last log file and private key chosen in file dialogs are remembered in the `[paths]`
section of the same file, and the next dialog opens there. The app rewrites only those keys.

## Versioning

One version for the app, stored in `version.toml`:

```
version = "1.0.0"
```

Edit it by hand when you change something. It is shown in the window title.

## Security notes

- Host keys are trusted on first connect and pinned. A changed key aborts the
  connection with a mismatch message (possible man-in-the-middle). To accept a
  legitimately changed key, remove that host's line from `known_hosts`.
- `profiles.json` is written atomically and readable only by the current user (on
  Windows, file permissions are not enforced this way). A password is saved only
  if you answer Yes when saving a profile, and it is stored as plain text. Prefer
  key login.
- Log files are created owner-only on first write. They contain whatever the
  commands print, so treat them as sensitive.
- Commands run on the device exactly as typed, with the logged-in user's rights.
  Remote output is limited per command (`max_output_bytes`) and every command has a
  timeout (`command_timeout`).
- DSA keys are not supported (deprecated and removed from current paramiko).
- Do not commit `Service/Service_profiles/` or any `.log` file (see `.gitignore`).
