"""
Launcher - opens the apps that sit next to it and keeps their versions and change logs.

Apps tab lists every <number>.<name> folder that has a main.py, with its version and a
description you can edit. Open starts the app and the launcher closes. Versions tab bumps an
app's version by 0.1 (1.8, 1.9, 2.0, 2.1 ...) and needs a change log note. The bump writes
version.toml, appends to changelog.toml and adds the entry to the top of CHANGELOG.md in that
app's folder. Commit Log tab shows every bump of every app from their changelog.toml files.

Usage:
    python main.py
    Settings: Service/Service_configs/launcher/launcher_config.toml
    Apps are found in the folder above this one (set [paths] apps_dir to change it).

Requirements: Python 3.11+, tkinter, customtkinter (pip install -r requirements.txt)
"""
import tkinter as tk
from tkinter import messagebox

from core import core

CONFIG_FILE = "Service/Service_configs/launcher/launcher_config.toml"
APP_VERSION = core.read_version(core.resolve_path(core.VERSION_FILE))


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
