"""
SSH Log - run commands on devices over SSH on a schedule and record the output.

Open one tab per device, connect, add the commands to run (each with its own
interval and optional tag) and press Start Logging. Every result is appended to
a log file as a [record] block that the SSH Dashboard can read. A lost
connection is re-established automatically and logging resumes. Profiles
(host, user, commands, reconnect settings) can be saved and loaded.

Usage:
    python main.py
    Settings: Service/Service_configs/ssh_log/ssh_log_config.toml
    Profiles: Service/Service_profiles/ssh_log/profiles.json (owner-only file;
              a password is stored only if you confirm it when saving)
    Host keys: Service/Service_profiles/ssh_log/known_hosts (trusted on first
              connect, a changed key aborts the connection)

Requirements: Python 3.11+, tkinter, paramiko (pip install -r requirements.txt)
"""
import tkinter as tk
from tkinter import messagebox

from core import core

CONFIG_FILE = "Service/Service_configs/ssh_log/ssh_log_config.toml"
VERSION_FILE = "version.toml"
APP_VERSION = core.read_version(core.resolve_path(VERSION_FILE))


def _fail(title: str, message: str) -> None:
    tk.Tk().withdraw()
    messagebox.showerror(title, message)
    raise SystemExit(1)


def main() -> None:
    try:
        cfg = core.load_config(CONFIG_FILE)
    except core.ConfigError as exc:
        _fail("Configuration error", str(exc))
    try:
        from ui import App
    except ImportError as exc:
        _fail("Missing dependency", f"{exc}\n\nRun: pip install -r requirements.txt")
    App(cfg, APP_VERSION).mainloop()


if __name__ == "__main__":
    main()
