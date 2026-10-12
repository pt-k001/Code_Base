"""
SSH Dashboard - turns SSH Log files into live, zoomable charts.

Click Browse, pick a log file written by SSH Log (or any file in the same
[record] format) and press Load. Each enabled module in modules/ parses the
records of its command into SQLite and the charts update on a timer. Export
the history to Excel, save a snapshot of the database, or open a saved one
later without the log. New metrics are added by dropping a file into
modules/ (see README) and pressing Rescan modules/ in the Settings tab.

Usage:
    python main.py
    Settings: Service/Service_configs/ssh_dashboard/ssh_dashboard_config.toml
    State:    Service/Service_profiles/ssh_dashboard/ (metrics.sqlite,
              module_state.json)

Requirements: Python 3.11+, tkinter, matplotlib, pandas, openpyxl
(pip install -r requirements.txt)
"""
import tkinter as tk
from tkinter import messagebox

from core import core

CONFIG_FILE = "Service/Service_configs/ssh_dashboard/ssh_dashboard_config.toml"
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
    root = tk.Tk()
    App(root, cfg, APP_VERSION)
    root.mainloop()


if __name__ == "__main__":
    main()
