"""
Evaluation of Tesseract models.

Pick a folder of name.png + name.gt.txt pairs (e.g. the dataset maker's eval folder)
and one or more models, press Run. Per model it reports:

    exact match   reading == ground truth (whitespace collapsed)
    value match   first number of the reading == first number of the truth (units ignored)
    CER           character edits / ground-truth characters over the whole set

and lists the wrong readings and the most frequent character mistakes.
The images are fed to Tesseract as they are (they are already preprocessed).

Usage:
    python main.py
    Settings: Service/Service_configs/evaluation/evaluation_config.toml

Requirements: Python 3.11+, opencv-python, numpy, tesserocr. Nothing else.
"""
import tkinter as tk
from tkinter import messagebox

from core import core

CONFIG_FILE = "Service/Service_configs/evaluation/evaluation_config.toml"
VERSION_FILE = "version.toml"
APP_VERSION = core.read_version(core.resolve_path(VERSION_FILE))


def main() -> None:
    from ui import App

    try:
        app = App(core.load_config(CONFIG_FILE), APP_VERSION)
    except (core.ConfigError, KeyError) as exc:
        if tk._default_root is None:
            tk.Tk().withdraw()
        messagebox.showerror("Configuration error", f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)
    app.mainloop()


if __name__ == "__main__":
    main()
