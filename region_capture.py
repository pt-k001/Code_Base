"""Region Capture - compact monochrome tkinter app.

Select a screen region once, then press Capture to save it as PNG.
Settings are stored in config.toml next to this script.

Requires: Python 3.11+ and Pillow  (pip install pillow)
"""

import json
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

import tomllib
from PIL import Image, ImageGrab, ImageTk

# Make screen coordinates match real pixels on high-DPI Windows displays
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.toml"

DEFAULT_CONFIG = {
    "output_dir": str(APP_DIR / "captures"),
    "filename_pattern": "capture_%Y%m%d_%H%M%S",
    "rect": None,  # (x1, y1, x2, y2) or None
}

# Monochrome palette
WHITE, LIGHT, MID, DARK, BLACK = "#ffffff", "#f0f0f0", "#808080", "#404040", "#000000"
FONT = ("Segoe UI", 8)

PREVIEW_W, PREVIEW_H = 260, 120


# ---------------------------------------------------------------- config ----
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
            cfg["output_dir"] = data.get("output_dir", cfg["output_dir"])
            cfg["filename_pattern"] = data.get("filename_pattern", cfg["filename_pattern"])
            r = data.get("rect")
            if r and all(k in r for k in ("x1", "y1", "x2", "y2")):
                cfg["rect"] = (int(r["x1"]), int(r["y1"]), int(r["x2"]), int(r["y2"]))
        except (tomllib.TOMLDecodeError, OSError, ValueError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    lines = [
        f"output_dir = {json.dumps(cfg['output_dir'])}",
        f"filename_pattern = {json.dumps(cfg['filename_pattern'])}",
    ]
    if cfg["rect"]:
        x1, y1, x2, y2 = cfg["rect"]
        lines += ["", "[rect]", f"x1 = {x1}", f"y1 = {y1}", f"x2 = {x2}", f"y2 = {y2}"]
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------- app ----
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.photo = None  # keep reference so the image isn't garbage collected

        root.title("Region Capture")
        root.configure(bg=WHITE)
        root.resizable(False, False)
        root.geometry("280x260")

        self._build_ui()
        self._refresh_output_label()
        self._update_status("Ready" if self.cfg["rect"] else "No area selected")

    # ---- UI ----
    def _button(self, parent, text, command):
        return tk.Button(
            parent, text=text, command=command, font=FONT,
            bg=LIGHT, fg=BLACK, activebackground=MID, activeforeground=BLACK,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=DARK, highlightcolor=BLACK,
            padx=6, pady=2, cursor="hand2",
        )

    def _build_ui(self):
        pad = {"padx": 8}

        # Preview
        frame = tk.Frame(self.root, bg=WHITE, highlightthickness=1,
                         highlightbackground=DARK, width=PREVIEW_W, height=PREVIEW_H)
        frame.pack(pady=(8, 6), **pad)
        frame.pack_propagate(False)
        self.preview = tk.Label(frame, text="No preview", font=FONT,
                                bg=WHITE, fg=MID)
        self.preview.pack(expand=True, fill="both")

        # Output row
        row1 = tk.Frame(self.root, bg=WHITE)
        row1.pack(fill="x", pady=2, **pad)
        self.out_label = tk.Label(row1, text="", font=FONT, bg=WHITE, fg=DARK,
                                  anchor="w")
        self.out_label.pack(side="left", fill="x", expand=True)
        self._button(row1, "Output", self.choose_output).pack(side="right")

        # Action row
        row2 = tk.Frame(self.root, bg=WHITE)
        row2.pack(fill="x", pady=6, **pad)
        row2.columnconfigure((0, 1), weight=1, uniform="b")
        self._button(row2, "Select", self.select_area).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        self._button(row2, "Capture", self.capture).grid(
            row=0, column=1, sticky="ew", padx=(3, 0))

        # Status
        self.status = tk.Label(self.root, text="", font=FONT, bg=WHITE,
                               fg=MID, anchor="w")
        self.status.pack(fill="x", **pad)

    def _update_status(self, text: str):
        self.status.config(text=text)

    def _refresh_output_label(self):
        path = self.cfg["output_dir"]
        short = path if len(path) <= 32 else "..." + path[-29:]
        self.out_label.config(text=short)

    # ---- preview ----
    def _show_preview(self, img: Image.Image):
        thumb = img.copy()
        thumb.thumbnail((PREVIEW_W - 4, PREVIEW_H - 4))
        self.photo = ImageTk.PhotoImage(thumb)
        self.preview.config(image=self.photo, text="")

    # ---- actions ----
    def choose_output(self):
        folder = filedialog.askdirectory(
            initialdir=self.cfg["output_dir"], title="Output folder")
        if folder:
            self.cfg["output_dir"] = folder
            save_config(self.cfg)
            self._refresh_output_label()
            self._update_status("Output folder set")

    def select_area(self):
        self.root.withdraw()
        self.root.update()
        self.root.after(200, self._open_overlay)

    def _open_overlay(self):
        Overlay(self.root, self._on_area_selected)

    def _on_area_selected(self, rect):
        if rect:
            self.cfg["rect"] = rect
            save_config(self.cfg)
            # Overlay is gone; grab the region before the window returns
            self.root.after(150, self._grab_for_preview)
        else:
            self.root.deiconify()
            self._update_status("Selection cancelled")

    def _grab_for_preview(self):
        try:
            img = ImageGrab.grab(bbox=self.cfg["rect"])
            self._show_preview(img)
            self._update_status("Area selected")
        except Exception as e:
            self._update_status(f"Error: {e}")
        self.root.deiconify()

    def capture(self):
        if not self.cfg["rect"]:
            self._update_status("Select an area first")
            return
        self.root.withdraw()
        self.root.update()
        time.sleep(0.25)  # let the window disappear from the screen
        try:
            img = ImageGrab.grab(bbox=self.cfg["rect"])
            out = Path(self.cfg["output_dir"])
            out.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime(self.cfg["filename_pattern"]) + ".png"
            img.save(out / name, "PNG")
            msg = f"Saved: {name}"
        except Exception as e:
            img, msg = None, f"Error: {e}"
        self.root.deiconify()
        if img:
            self._show_preview(img)
        self._update_status(msg)


class Overlay:
    """Fullscreen translucent layer for dragging a rectangle."""

    def __init__(self, master: tk.Tk, callback):
        self.callback = callback
        self.start = None
        self.rect_id = None

        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        w, h = master.winfo_screenwidth(), master.winfo_screenheight()
        self.win.geometry(f"{w}x{h}+0+0")
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.3)
        self.win.configure(bg=BLACK)

        self.canvas = tk.Canvas(self.win, bg=BLACK, highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Escape>", lambda e: self._finish(None))
        self.win.focus_force()

    def _press(self, e):
        self.start = (e.x, e.y, e.x_root, e.y_root)
        self.rect_id = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline=WHITE, width=2)

    def _drag(self, e):
        if self.start:
            self.canvas.coords(self.rect_id, self.start[0], self.start[1], e.x, e.y)

    def _release(self, e):
        if not self.start:
            return
        x1, y1 = self.start[2], self.start[3]
        x2, y2 = e.x_root, e.y_root
        rect = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        if rect[2] - rect[0] < 5 or rect[3] - rect[1] < 5:
            rect = None
        self._finish(rect)

    def _finish(self, rect):
        self.win.destroy()
        self.callback(rect)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
