import tkinter as tk
from tkinter import messagebox

from core import core

CONFIG_FILE = "Service/Service_configs/trainer/trainer_config.toml"
VERSION_FILE = "version.toml"
APP_VERSION = core.read_version(core.resolve_path(VERSION_FILE))


def main() -> None:
    from ui import App

    try:
        app = App(core.load_config(CONFIG_FILE), APP_VERSION)
    except core.ConfigError as exc:
        if tk._default_root is None:
            tk.Tk().withdraw()
        messagebox.showerror("Configuration error", str(exc))
        raise SystemExit(1)
    app.mainloop()


if __name__ == "__main__":
    main()
