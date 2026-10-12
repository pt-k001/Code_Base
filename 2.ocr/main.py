import multiprocessing

from core import core

CONFIG_FILE = "Service/Service_configs/rf_ocr/rf_ocr_config.toml"
VERSION_FILE = "version.toml"
APP_VERSION = core.read_version(core.resolve_path(VERSION_FILE))


def main() -> None:
    from ui import App

    def load_config() -> dict:
        return core.load_config(CONFIG_FILE)

    try:
        app = App(load_config, APP_VERSION)
    except core.ConfigError as exc:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Configuration error", str(exc))
        raise SystemExit(1)
    app.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
