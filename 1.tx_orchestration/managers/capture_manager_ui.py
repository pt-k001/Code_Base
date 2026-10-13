#!/usr/bin/env python3
"""
capture_manager_ui.py - Tkinter front end for capture_manager.

Holds every widget: the tooltip, the region selection overlay, the main
window (Capture / Log / Settings tabs) and the TCP command dispatch that
drives the window. All capture, database and state logic lives in
capture_manager.py; start the application with:

    python capture_manager.py

Sharp (high-DPI) rendering:
    The Windows block below tells the OS that this process handles scaling
    itself (per-monitor DPI awareness). Without it Windows renders the whole
    window at 96 DPI and bitmap-stretches it, which looks blurry. With it
    Tk draws text and widgets at the real monitor resolution, and mss /
    the selection overlay report true physical pixel coordinates, so the
    selected regions match the captured pixels exactly. The preview
    thumbnails are also downscaled with LANCZOS, which keeps them crisp.
"""

import ctypes
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, simpledialog, messagebox, filedialog

import mss
import numpy as np
from PIL import Image, ImageTk

# Must run before the first Tk() window is created.
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from capture_manager import *  # noqa: F401,F403  (state, core, helper_* ...)
from capture_manager import (  # noqa: F401  (private names are not star-exported)
    _helper_num, _helper_natural_key,
)

BG, SURFACE, PANEL, ACCENT, ACCENT2 = ("SystemButtonFace",) * 5
TEXT, TEXT_DIM = "black", "gray40"
RED, GREEN, BORDER, YELLOW = "red", "dark green", "gray70", "#ffffe0"

PREVIEW_THUMB_W = 96
PREVIEW_BOX_W   = 130
PREVIEW_BOX_H   = 150

MAX_LOG_LINES = 2000

INTERVAL_OPTIONS = sorted(set(
    [round(x * 0.01, 3) for x in range(1, 20)] +
    [round(x * 0.05, 2) for x in range(4, 40)] +
    [round(x * 0.25, 2) for x in range(8, 41)]
))


# ─────────────────────────────────────────────────────────────────────────────
# Tooltip
# ─────────────────────────────────────────────────────────────────────────────
class Tooltip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text   = text
        self.tip    = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _=None):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry("+%d+%d" % (x, y))
        tk.Label(tw, text=self.text, bg=YELLOW, fg="black",
                 font=("Segoe UI", 8), padx=6, pady=3,
                 relief="solid", bd=1, wraplength=260).pack()

    def _hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ─────────────────────────────────────────────────────────────────────────────
# Selection Overlay
# ─────────────────────────────────────────────────────────────────────────────
class SelectionOverlay:
    def __init__(self, parent: tk.Tk, callback):
        self.parent   = parent
        self.callback = callback
        self.sx = self.sy = 0
        self.rect = None

        self.win = tk.Toplevel(parent)

        with mss.mss() as sct:
            vm = sct.monitors[0]
        self.ox, self.oy = vm["left"], vm["top"]
        sw, sh = vm["width"], vm["height"]

        self.win.overrideredirect(True)
        self.win.geometry("%dx%d+%d+%d" % (sw, sh, self.ox, self.oy))
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.30)
        self.win.config(cursor="cross")
        self.win.lift()
        self.win.update_idletasks()

        self.canvas = tk.Canvas(self.win, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.create_text(sw // 2, 30,
            text="Drag to select a region  \u2022  ESC to cancel",
            fill="white", font=("Segoe UI", 14, "bold"))

        self.canvas.bind("<ButtonPress-1>",   self._press)
        self.canvas.bind("<B1-Motion>",       self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Escape>", self._cancel)
        self.win.focus_force()

    def _press(self, e):
        self.sx, self.sy = e.x, e.y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            self.sx, self.sy, e.x, e.y,
            outline="white", fill="", width=2)

    def _drag(self, e):
        if self.rect:
            self.canvas.coords(self.rect, self.sx, self.sy, e.x, e.y)

    def _release(self, e):
        x1, y1 = min(self.sx, e.x), min(self.sy, e.y)
        x2, y2 = max(self.sx, e.x), max(self.sy, e.y)
        x1 += self.ox; x2 += self.ox
        y1 += self.oy; y2 += self.oy
        self.win.destroy()
        if (x2 - x1) > 10 and (y2 - y1) > 10:
            name = simpledialog.askstring(
                "Name this region",
                "Enter a label for the captured area:",
                parent=self.parent)
            name = helper_clean_label(name or "")
            if name:
                self.callback(name, (x1, y1, x2 - x1, y2 - y1))

    def _cancel(self, _=None):
        self.win.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# Widget helpers
# ─────────────────────────────────────────────────────────────────────────────
def helper_styled_button(parent, text, command, color=ACCENT, **kw):
    opts = dict(relief="raised", bd=2, padx=8, pady=4,
                font=("Segoe UI", 8), cursor="hand2")
    opts.update(kw)
    return tk.Button(parent, text=text, command=command, **opts)

def helper_label(parent, text, size=8, color=TEXT, bold=False, **kw):
    weight = "bold" if bold else "normal"
    return tk.Label(parent, text=text, fg=color,
                    font=("Segoe UI", size, weight), **kw)


# ─────────────────────────────────────────────────────────────────────────────
# Main Application
# ─────────────────────────────────────────────────────────────────────────────
class CaptureGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Capture Version - %s " % APP_VERSION)
        self.geometry("860x600")
        self.minsize(700, 400)

        load_settings()
        state.active_db_name = helper_make_active_db_name(state.db_name)
        state.last_change_time = time.time()
        self._profiles: dict = load_profiles()

        self.store   = CaptureStore(self)
        self.engine  = CaptureEngine(self)
        self._preview_imgs: dict   = {}
        self._preview_counts: dict = {}
        self._log_queue: "queue.Queue"     = queue.Queue()   # any thread -> UI
        self._preview_queue: "queue.Queue" = queue.Queue()   # workers  -> UI
        self._closing = False

        self._apply_styles()
        self._build_ui()
        self._bind_shortcuts()
        self.attributes("-topmost", state.always_on_top)

        helper_prune_old_logs(state.log_retention_days)

        self.store.start(helper_primary_db_path(state.active_db_name),
                         helper_secondary_db_path(state.active_db_name))

        core.start_run_log(APP)
        core.logmsg(APP, "=== capture session start (v%s) ===" % APP_VERSION)
        threading.Thread(
            target=core.run_tcp_server,
            args=(APP, DEFAULT_HOST, state.port, self._handle_tcp),
            daemon=True,
        ).start()

        threading.Thread(target=self.engine.run_loop, daemon=True).start()
        self.after(150, self._ui_tick)
        self.after(300, self._maybe_notify_db_name)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def report_callback_exception(self, exc, val, tb):
        core.logmsg(APP, "UI callback error: %s: %s" % (exc.__name__, val))
        self.queue_log("UI error: %s" % val, level="ERROR")

    def _maybe_notify_db_name(self):
        if state.last_stopped and not state.auto_timestamp_db_name:
            messagebox.showwarning(
                "DB name unchanged",
                "The previous run was stopped with DB name '%s'.\n\n"
                "New captures will continue in this same database unless "
                "you set a new DB name in the Settings tab (Ctrl+B)."
                % state.db_name,
                parent=self)

    # ── Styles ───────────────────────────────────────────────────────────────
    def _apply_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("default")
        except Exception:
            pass

    # ── UI Construction ──────────────────────────────────────────────────────
    def _build_ui(self):
        self._build_topbar()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)
        self.tab_main     = tk.Frame(self.notebook)
        self.tab_log      = tk.Frame(self.notebook)
        self.tab_settings = tk.Frame(self.notebook)
        self.notebook.add(self.tab_main,     text="Capture")
        self.notebook.add(self.tab_log,      text="Log")
        self.notebook.add(self.tab_settings, text="Settings")
        self._build_main_area(self.tab_main)
        self._build_log_tab(self.tab_log)
        self._build_settings_tab(self.tab_settings)

    def _build_topbar(self):
        bar = tk.Frame(self, pady=4)
        bar.pack(side="top", fill="x")

        self.lbl_status = tk.Label(bar, text="\u25cf IDLE", fg=RED,
                                   font=("Segoe UI", 9, "bold"))
        self.lbl_status.pack(side="left", padx=(8, 0))

        self.btn_start = helper_styled_button(bar, "\u25b6  Start", self._start)
        self.btn_start.pack(side="left", padx=(8, 2))
        Tooltip(self.btn_start, "Start capturing all defined regions (Ctrl+R)")

        self.btn_stop = helper_styled_button(bar, "\u23f9  Stop", self._stop)
        self.btn_stop.pack(side="left", padx=2)
        Tooltip(self.btn_stop, "Pause capture without losing session (Ctrl+S)")

        tk.Frame(bar, bg=BORDER, width=1).pack(side="left", fill="y", padx=6, pady=2)

        btn_ns = helper_styled_button(bar, "\u2295 New Session", self._new_session)
        btn_ns.pack(side="left", padx=2)
        Tooltip(btn_ns, "Set the RF combo channel/txpower for this session (bumps version if reused).")

        self.lbl_session = tk.Label(bar, text="Session: unnamed (v1)",
                                    font=("Segoe UI", 8, "bold"))
        self.lbl_session.pack(side="left", padx=8)

        right = tk.Frame(bar)
        right.pack(side="right", padx=8)

        tk.Label(right, text="Interval:", fg=TEXT_DIM,
                 font=("Segoe UI", 8)).pack(side="left")
        self.interval_cb = ttk.Combobox(right,
                                        values=[str(i) for i in INTERVAL_OPTIONS],
                                        width=6, state="readonly")
        self.interval_cb.set(str(state.interval))
        self.interval_cb.bind("<<ComboboxSelected>>", self._on_interval)
        self.interval_cb.pack(side="left", padx=(2, 10))

        tk.Label(right, text="DB:", fg=TEXT_DIM,
                 font=("Segoe UI", 8)).pack(side="left")
        self.lbl_db_dir = tk.Label(right, text=self._db_label(),
                                   fg=TEXT_DIM, font=("Segoe UI", 8))
        self.lbl_db_dir.pack(side="left", padx=(2, 0))

    def _build_main_area(self, parent):
        main = tk.Frame(parent)
        main.pack(fill="both", expand=True)

        sidebar = tk.Frame(main, width=160)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        tk.Label(sidebar, text="CAPTURE AREAS", fg=TEXT_DIM,
                 font=("Segoe UI", 7, "bold")).pack(pady=(6, 2))

        list_frame = tk.Frame(sidebar)
        list_frame.pack(fill="both", expand=True, padx=4)

        sb = ttk.Scrollbar(list_frame, orient="vertical")
        self.region_list = tk.Listbox(list_frame, bg=PANEL,
                                      relief="sunken", bd=1,
                                      font=("Segoe UI", 9),
                                      yscrollcommand=sb.set,
                                      activestyle="dotbox")
        sb.config(command=self.region_list.yview)
        sb.pack(side="right", fill="y")
        self.region_list.pack(fill="both", expand=True)
        self.region_list.bind("<Delete>", self._delete_selected_region)

        btn_row = tk.Frame(sidebar)
        btn_row.pack(fill="x", padx=4, pady=4)
        b_add = helper_styled_button(btn_row, "\uff0b Add Area", self._open_selector)
        b_add.pack(fill="x", pady=1)
        Tooltip(b_add, "Draw a rectangle on-screen to define a capture region (Ctrl+A)")

        b_del = helper_styled_button(btn_row, "\u2715 Remove", self._delete_selected_region)
        b_del.pack(fill="x", pady=1)

        b_clear = helper_styled_button(btn_row, "Clear All", self._clear_regions)
        b_clear.pack(fill="x", pady=1)

        tk.Frame(sidebar, bg=BORDER, height=1).pack(fill="x", padx=4, pady=4)
        tk.Label(sidebar, text="PROFILES", fg=TEXT_DIM,
                 font=("Segoe UI", 7, "bold")).pack()

        self.profile_var = tk.StringVar()
        self.profile_cb  = ttk.Combobox(sidebar, textvariable=self.profile_var,
                                        state="readonly", width=20)
        self.profile_cb.pack(padx=4, pady=2)
        self._refresh_profile_list()

        p_row = tk.Frame(sidebar)
        p_row.pack(fill="x", padx=4, pady=2)
        helper_styled_button(p_row, "Load", self._load_profile
                      ).pack(side="left", fill="x", expand=True, padx=(0, 1))
        helper_styled_button(p_row, "Save", self._save_profile
                      ).pack(side="left", fill="x", expand=True, padx=(1, 0))

        right = tk.Frame(main)
        right.pack(side="right", fill="both", expand=True)

        stats_bar = tk.Frame(right, pady=3)
        stats_bar.pack(fill="x")
        self.lbl_total = tk.Label(stats_bar, text="Total: 0",
                                  font=("Segoe UI", 8, "bold"))
        self.lbl_total.pack(side="left", padx=10)
        self.lbl_regions_count = tk.Label(stats_bar, text="Regions: 0",
                                          fg=TEXT_DIM, font=("Segoe UI", 8))
        self.lbl_regions_count.pack(side="left", padx=10)
        self.lbl_pending = tk.Label(stats_bar, text="DB queue: 0",
                                    fg=TEXT_DIM, font=("Segoe UI", 8))
        self.lbl_pending.pack(side="left", padx=10)

        self.canvas = tk.Canvas(right, highlightthickness=0)
        h_sb = ttk.Scrollbar(right, orient="horizontal", command=self.canvas.xview)
        v_sb = ttk.Scrollbar(right, orient="vertical",   command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=h_sb.set, yscrollcommand=v_sb.set)
        h_sb.pack(side="bottom", fill="x")
        v_sb.pack(side="right",  fill="y")
        self.canvas.pack(fill="both", expand=True)

        self.preview_frame = tk.Frame(self.canvas)
        self._canvas_win   = self.canvas.create_window(
            (0, 0), window=self.preview_frame, anchor="nw")
        self.preview_frame.bind("<Configure>",
            lambda _: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")))

    def _build_log_tab(self, parent):
        tk.Label(parent,
                 text="TCP commands received and replies sent.",
                 fg=TEXT_DIM, font=("Segoe UI", 8)).pack(
                     anchor="w", padx=6, pady=(6, 2))
        self.log_box = tk.Text(parent, state="disabled",
                               font=("Consolas", 9), bg=PANEL,
                               relief="sunken", bd=1)
        self.log_box.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def _build_settings_tab(self, parent):
        sc = tk.Canvas(parent, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient="vertical", command=sc.yview)
        sc.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        sc.pack(side="left", fill="both", expand=True)
        frm = tk.Frame(sc, padx=12, pady=12)
        sc.create_window((0, 0), window=frm, anchor="nw")
        frm.bind("<Configure>",
                 lambda _: sc.configure(scrollregion=sc.bbox("all")))

        def _wheel(e):
            sc.yview_scroll(-1 if (e.delta > 0 or e.num == 4) else 1, "units")
        def _bind_wheel(_):
            sc.bind_all("<MouseWheel>", _wheel)
            sc.bind_all("<Button-4>", _wheel)
            sc.bind_all("<Button-5>", _wheel)
        def _unbind_wheel(_):
            sc.unbind_all("<MouseWheel>")
            sc.unbind_all("<Button-4>")
            sc.unbind_all("<Button-5>")
        sc.bind("<Enter>", _bind_wheel)
        sc.bind("<Leave>", _unbind_wheel)

        tk.Label(frm, text="Settings file: %s" % core._require_env(APP)["config_file"],
                 fg=TEXT_DIM,
                 font=("Segoe UI", 8)).grid(
                     row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        tk.Label(frm, text="DB Name:").grid(row=1, column=0, sticky="w", pady=3)
        self.dbname_entry = tk.Entry(frm, width=20)
        self.dbname_entry.insert(0, state.db_name)
        self.dbname_entry.grid(row=1, column=1, sticky="w")
        helper_styled_button(frm, "Set", self._apply_db_name
                      ).grid(row=1, column=2, sticky="w", padx=6)
        tk.Label(frm,
                 text="next DB is saved as DBs/<name>/<name>.db \u2014 applies immediately",
                 fg=TEXT_DIM, font=("Segoe UI", 7)).grid(
                     row=2, column=0, columnspan=3, sticky="w")

        self.auto_ts_var = tk.BooleanVar(value=state.auto_timestamp_db_name)
        tk.Checkbutton(frm, text="Auto-append timestamp to DB name on each start",
                       variable=self.auto_ts_var,
                       font=("Segoe UI", 8)).grid(
                           row=3, column=0, columnspan=3, sticky="w", pady=(2, 10))

        tk.Label(frm, text="TCP Port:").grid(row=4, column=0, sticky="w", pady=3)
        self.port_entry = tk.Entry(frm, width=10)
        self.port_entry.insert(0, str(state.port))
        self.port_entry.grid(row=4, column=1, sticky="w")
        tk.Label(frm, text="(restart to apply)", fg=TEXT_DIM,
                 font=("Segoe UI", 7)).grid(row=4, column=2, sticky="w", padx=6)

        tk.Label(frm, text="Queue batch size:").grid(row=5, column=0, sticky="w", pady=3)
        self.qbatch_entry = tk.Entry(frm, width=10)
        self.qbatch_entry.insert(0, str(state.queue_batch))
        self.qbatch_entry.grid(row=5, column=1, sticky="w")
        tk.Label(frm, text="frames per DB flush (default 100)", fg=TEXT_DIM,
                 font=("Segoe UI", 7)).grid(row=5, column=2, sticky="w", padx=6)

        tk.Label(frm, text="Queue timeout (s):").grid(row=6, column=0, sticky="w", pady=3)
        self.qtimeout_entry = tk.Entry(frm, width=10)
        self.qtimeout_entry.insert(0, str(state.queue_timeout))
        self.qtimeout_entry.grid(row=6, column=1, sticky="w")
        tk.Label(frm, text="max wait before flushing a partial batch", fg=TEXT_DIM,
                 font=("Segoe UI", 7)).grid(row=6, column=2, sticky="w", padx=6)

        tk.Label(frm, text="PNG compression (0-9):").grid(row=7, column=0, sticky="w", pady=3)
        self.pngc_entry = tk.Entry(frm, width=10)
        self.pngc_entry.insert(0, str(state.png_compression))
        self.pngc_entry.grid(row=7, column=1, sticky="w")
        tk.Label(frm, text="lower = faster, same lossless quality", fg=TEXT_DIM,
                 font=("Segoe UI", 7)).grid(row=7, column=2, sticky="w", padx=6)

        tk.Label(frm, text="Log level:").grid(row=8, column=0, sticky="w", pady=3)
        self.loglevel_cb = ttk.Combobox(frm, values=list(LOG_LEVELS.keys()),
                                        width=8, state="readonly")
        self.loglevel_cb.set(state.log_level)
        self.loglevel_cb.grid(row=8, column=1, sticky="w")
        tk.Label(frm, text="filters the Log tab / console", fg=TEXT_DIM,
                 font=("Segoe UI", 7)).grid(row=8, column=2, sticky="w", padx=6)

        tk.Label(frm, text="Log retention (days):").grid(row=9, column=0, sticky="w", pady=3)
        self.logretention_entry = tk.Entry(frm, width=10)
        self.logretention_entry.insert(0, str(state.log_retention_days))
        self.logretention_entry.grid(row=9, column=1, sticky="w")
        tk.Label(frm, text="0 = keep forever; pruned on startup", fg=TEXT_DIM,
                 font=("Segoe UI", 7)).grid(row=9, column=2, sticky="w", padx=6)

        self.aot_var = tk.BooleanVar(value=state.always_on_top)
        tk.Checkbutton(frm, text="Keep window always on top",
                       variable=self.aot_var, command=self._toggle_always_on_top,
                       font=("Segoe UI", 8)).grid(
                           row=10, column=0, columnspan=3, sticky="w", pady=(8, 10))

        tk.Label(frm, text="\u2500\u2500 STOPME (orchestrator) \u2500\u2500", fg=TEXT_DIM,
                 font=("Segoe UI", 7, "bold")).grid(
                     row=11, column=0, columnspan=3, sticky="w")

        self.stopme_var = tk.BooleanVar(value=state.stopme_enabled)
        tk.Checkbutton(frm, text="Send STOPME when all regions stop changing",
                       variable=self.stopme_var,
                       font=("Segoe UI", 8)).grid(
                           row=12, column=0, columnspan=3, sticky="w", pady=(2, 3))

        tk.Label(frm, text="Orchestrator host:port:").grid(row=13, column=0, sticky="w", pady=3)
        hp = tk.Frame(frm)
        hp.grid(row=13, column=1, sticky="w")
        self.orch_host_entry = tk.Entry(hp, width=13)
        self.orch_host_entry.insert(0, state.orchestrator_host)
        self.orch_host_entry.pack(side="left")
        tk.Label(hp, text=":").pack(side="left")
        self.orch_port_entry = tk.Entry(hp, width=6)
        self.orch_port_entry.insert(0, str(state.orchestrator_port))
        self.orch_port_entry.pack(side="left")

        tk.Label(frm, text="Idle threshold (s):").grid(row=14, column=0, sticky="w", pady=3)
        self.stopme_idle_entry = tk.Entry(frm, width=10)
        self.stopme_idle_entry.insert(0, str(state.stopme_idle_seconds))
        self.stopme_idle_entry.grid(row=14, column=1, sticky="w")
        tk.Label(frm, text="sends 'STOPME' once all region hashes are unchanged this long",
                 fg=TEXT_DIM, font=("Segoe UI", 7)).grid(
                     row=14, column=2, sticky="w", padx=6)

        tk.Label(frm, text="\u2500\u2500 SAVE LOCATIONS (at least one required) \u2500\u2500", fg=TEXT_DIM,
                 font=("Segoe UI", 7, "bold")).grid(
                     row=15, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self.pri_var = tk.BooleanVar(value=state.primary_enabled)
        tk.Checkbutton(frm, text="Save every capture to primary (Service/Service_DBs/DBs)",
                       variable=self.pri_var,
                       font=("Segoe UI", 8)).grid(
                           row=16, column=0, columnspan=3, sticky="w", pady=(2, 0))

        self.sec_var = tk.BooleanVar(value=state.secondary_enabled)
        tk.Checkbutton(frm, text="Also save every capture to a secondary folder",
                       variable=self.sec_var,
                       font=("Segoe UI", 8)).grid(
                           row=17, column=0, columnspan=3, sticky="w", pady=(0, 3))

        tk.Label(frm, text="Secondary folder:").grid(row=18, column=0, sticky="w", pady=3)
        sp = tk.Frame(frm)
        sp.grid(row=18, column=1, columnspan=2, sticky="w")
        self.sec_dir_entry = tk.Entry(sp, width=36)
        self.sec_dir_entry.insert(0, state.secondary_dir)
        self.sec_dir_entry.pack(side="left")
        helper_styled_button(sp, "Browse\u2026", self._browse_secondary).pack(side="left", padx=4)

        helper_styled_button(frm, "Save Settings", self._save_settings_tab
                      ).grid(row=19, column=0, sticky="w", pady=(12, 0))
        self.settings_status = tk.Label(frm, text="", fg=GREEN, font=("Segoe UI", 8))
        self.settings_status.grid(
            row=19, column=1, columnspan=2, sticky="w", padx=6, pady=(12, 0))

    # ── Keyboard shortcuts ────────────────────────────────────────────────────
    def _bind_shortcuts(self):
        self.bind("<Control-r>", lambda _: self._start())
        self.bind("<Control-s>", lambda _: self._stop())
        self.bind("<Control-a>", lambda _: self._open_selector())
        self.bind("<Control-n>", lambda _: self._new_session())
        self.bind("<Control-b>", lambda _: self._focus_db_name())

    # ── UI tick ───────────────────────────────────────────────────────────────
    def _ui_tick(self):
        try:
            # logs
            for _ in range(200):
                try:
                    self._write_log(self._log_queue.get_nowait())
                except queue.Empty:
                    break

            # previews (latest frame per region), rebuilt once per tick
            latest = {}
            while True:
                try:
                    name, rgb, count = self._preview_queue.get_nowait()
                    latest[name] = (rgb, count)
                except queue.Empty:
                    break
            if latest:
                for name, (rgb, count) in latest.items():
                    self._render_preview(name, rgb, count)
                self._rebuild_preview_grid()

            running = state.is_running
            self.lbl_status.config(
                text="\u25cf CAPTURING" if running else "\u25cf IDLE",
                fg=GREEN if running else RED)
            self.lbl_session.config(
                text="Session: %s (v%d)" % (state.current_session, state.session_version))
            self.lbl_total.config(text="Total: %d" % state.total_captured)
            self.lbl_regions_count.config(text="Regions: %d" % len(state.regions))
            pending  = self.store.pending()
            busy_tag = "  [DB BUSY]" if self.store.is_busy() else ""
            sec_tag  = (" | 2nd: %d" % self.store.secondary_pending()
                        if self.store.secondary else "")
            self.lbl_pending.config(text="DB queue: %d%s%s" % (pending, sec_tag, busy_tag))
        except Exception as e:
            core.logmsg(APP, "UI tick error: %s" % e)
        finally:
            if not self._closing:
                self.after(150, self._ui_tick)

    # ── Thread-safe helpers ───────────────────────────────────────────────────
    def queue_log(self, msg: str, level: str = "INFO"):
        if LOG_LEVELS.get(level, 20) >= LOG_LEVELS.get(state.log_level, 20):
            self._log_queue.put(msg)
        if LOG_LEVELS.get(level, 20) >= LOG_LEVELS["WARNING"]:
            try:
                core.logmsg(APP, "%s: %s" % (level, msg))
            except Exception:
                pass

    def receive_preview(self, name: str, rgb: np.ndarray, count: int):
        self._preview_queue.put((name, rgb, count))

    # ── Preview rendering ─────────────────────────────────────────────────────
    def _render_preview(self, name: str, rgb: np.ndarray, count: int):
        # drop previews for regions removed while the frame was in flight
        with state.lock:
            if not any(r["name"] == name for r in state.regions):
                return
        h, w   = rgb.shape[:2]
        aspect = w / max(h, 1)
        nw     = PREVIEW_THUMB_W
        nh     = max(1, int(nw / aspect))
        nh     = min(nh, PREVIEW_BOX_H - 50)
        img_pil = Image.fromarray(rgb).resize((nw, nh), Image.LANCZOS)
        self._preview_imgs[name]   = ImageTk.PhotoImage(img_pil)
        self._preview_counts[name] = count

    def _rebuild_preview_grid(self):
        for w in self.preview_frame.winfo_children():
            w.destroy()
        for name in sorted(self._preview_imgs, key=_helper_natural_key):
            img_tk = self._preview_imgs[name]
            cnt    = self._preview_counts.get(name, 0)
            card = tk.Frame(self.preview_frame, bd=1, relief="groove",
                            width=PREVIEW_BOX_W, height=PREVIEW_BOX_H)
            card.pack(side="left", padx=4, pady=4, anchor="nw")
            card.pack_propagate(False)
            tk.Label(card, text=name,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=4, pady=(4, 0))
            img_holder = tk.Frame(card)
            img_holder.pack(expand=True, fill="both")
            tk.Label(img_holder, image=img_tk, bd=0).place(
                relx=0.5, rely=0.5, anchor="center")
            tk.Label(card, text="#%04d" % cnt, fg=TEXT_DIM,
                     font=("Consolas", 8)).pack(anchor="e", padx=4, pady=(0, 4))
        self.preview_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    # ── Logging ───────────────────────────────────────────────────────────────
    def _log(self, msg: str, level: str = "INFO"):
        self.queue_log(msg, level)

    def _write_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.config(state="normal")
        self.log_box.insert("end", "[%s] %s\n" % (ts, msg))
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_box.delete("1.0", "%d.0" % (lines - MAX_LOG_LINES))
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    # ── Control handlers ──────────────────────────────────────────────────────
    def _start(self, *_):
        if not state.regions:
            messagebox.showwarning("No regions",
                "Add at least one capture region before starting.",
                parent=self)
            return
        if not helper_has_storage(state.active_db_name):
            messagebox.showwarning("No storage", NO_STORAGE_MSG, parent=self)
            return
        state.is_running       = True
        state.last_change_time = time.time()
        state.stopme_sent      = False
        self._log("Capture started")

    def _stop(self, *_):
        state.is_running = False
        self._log("Capture stopped")

    def _set_session(self, name: str) -> dict:
        name = helper_clean_label(name)
        if not name:
            raise ValueError("invalid session name (empty, >%d chars, or control chars)"
                             % MAX_LABEL_LEN)
        cur_dir = helper_session_db_path(state.active_db_name)
        version = helper_next_session_version(cur_dir, name)
        with state.lock:
            state.current_session = name
            state.session_version = version
            state.counters        = {r["name"]: 1 for r in state.regions}
            state.total_captured  = 0
            state.last_hash       = {}
        self._preview_imgs.clear()
        self._preview_counts.clear()
        self._rebuild_preview_grid()
        self._log("Session set: %s (version %d)  db: %s" % (name, version, cur_dir))
        return {"session": name, "version": version}

    def _set_db_name(self, name: str) -> dict:
        name = helper_clean_label(name)
        if not name:
            raise ValueError("invalid DB name (empty, >%d chars, or control chars)"
                             % MAX_LABEL_LEN)
        active   = helper_make_active_db_name(name)
        new_path = helper_primary_db_path(active)
        sec_path = helper_secondary_db_path(active)
        if new_path and sec_path and os.path.normcase(os.path.abspath(sec_path)) == \
                                     os.path.normcase(os.path.abspath(new_path)):
            raise ValueError("secondary folder resolves to the primary DB")
        if not (new_path or sec_path):
            raise ValueError(NO_STORAGE_MSG)
        version = helper_next_session_version(new_path or sec_path, state.current_session)
        with state.lock:
            state.db_name         = name
            state.active_db_name  = active
            state.session_version = version
            state.counters        = {r["name"]: 1 for r in state.regions}
            state.total_captured  = 0
            state.last_hash       = {}
        self._preview_imgs.clear()
        self._preview_counts.clear()
        self._rebuild_preview_grid()
        self.store.change_target(new_path, sec_path)
        self.lbl_db_dir.config(text=self._db_label())
        self.dbname_entry.delete(0, tk.END)
        self.dbname_entry.insert(0, name)
        save_settings()
        self._log("DB name set: %s  db: %s" %
                  (name, "  +  ".join(p for p in (new_path, sec_path) if p)))
        return {"db_name": name, "active_db_name": active,
                "db_path": new_path or sec_path, "secondary_path": sec_path,
                "version": version}

    def _run_on_main(self, fn, timeout: float = 5.0):
        if threading.current_thread() is threading.main_thread():
            return fn()
        if self._closing:
            raise RuntimeError("application is shutting down")
        box, done = {}, threading.Event()
        def call():
            try:
                box["v"] = fn()
            except Exception as e:
                box["e"] = e
            finally:
                done.set()
        self.after(0, call)
        if not done.wait(timeout):
            raise TimeoutError("GUI thread did not respond")
        if "e" in box:
            raise box["e"]
        return box["v"]

    def _browse_secondary(self):
        d = filedialog.askdirectory(parent=self, title="Secondary save folder")
        if d:
            self.sec_dir_entry.delete(0, tk.END)
            self.sec_dir_entry.insert(0, d)

    def _db_label(self) -> str:
        name = state.active_db_name
        return "  +  ".join(p for p in (helper_primary_db_path(name), helper_secondary_db_path(name)) if p) \
               or "(no storage)"

    def _check_storage(self) -> str:
        primary = self.pri_var.get()
        enabled = self.sec_var.get()
        d       = self.sec_dir_entry.get().strip()
        if enabled and not d:
            return "Secondary folder is empty"
        if not (primary or enabled):
            return "Enable primary or a secondary folder"
        if enabled:
            new_path = helper_db_file_path(state.active_db_name, d)
            if primary and os.path.normcase(os.path.abspath(new_path)) == \
               os.path.normcase(os.path.abspath(helper_db_file_path(state.active_db_name))):
                return "Secondary folder must differ from the primary DBs folder"
            try:
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
            except OSError as e:
                return "Secondary folder not usable: %s" % e
        return ""

    def _apply_storage(self):
        state.primary_enabled   = self.pri_var.get()
        state.secondary_enabled = self.sec_var.get()
        state.secondary_dir     = self.sec_dir_entry.get().strip()
        name = state.active_db_name
        self.store.set_primary(helper_primary_db_path(name))
        self.store.set_secondary(helper_secondary_db_path(name))
        self.lbl_db_dir.config(text=self._db_label())

    def _new_session(self, *_):
        name = simpledialog.askstring(
            "New / Re-enter Session",
            "Enter session name (RF combo channel/txpower):",
            parent=self)
        if not name:
            return
        try:
            self._set_session(name)
        except ValueError as e:
            messagebox.showwarning("Invalid session name", str(e), parent=self)

    def _focus_db_name(self):
        self.notebook.select(self.tab_settings)
        self.dbname_entry.focus_set()
        self.dbname_entry.selection_range(0, tk.END)

    def _apply_db_name(self, *_):
        name = self.dbname_entry.get().strip()
        if not name:
            messagebox.showwarning("No name", "Enter a DB name first.", parent=self)
            return
        try:
            self._set_db_name(name)
        except ValueError as e:
            self.settings_status.config(text=str(e), fg=RED)
            return
        self.settings_status.config(text="DB name set to '%s'." % name, fg=GREEN)

    def _on_interval(self, *_):
        state.interval = _helper_num(self.interval_cb.get(), float, state.interval, 0.0, 3600.0)
        save_settings()

    def _save_settings_tab(self):
        try:
            port          = int(self.port_entry.get())
            qbatch        = int(self.qbatch_entry.get())
            qtimeout      = float(self.qtimeout_entry.get())
            pngc          = int(self.pngc_entry.get())
            log_retention = int(self.logretention_entry.get())
            orch_port     = int(self.orch_port_entry.get())
            stopme_idle   = float(self.stopme_idle_entry.get())
        except ValueError:
            self.settings_status.config(text="Invalid value(s): numbers expected", fg=RED)
            return

        problems = []
        if not 1 <= port <= 65535:        problems.append("port 1-65535")
        if not 1 <= orch_port <= 65535:   problems.append("orchestrator port 1-65535")
        if not 1 <= qbatch <= 100000:     problems.append("batch size 1-100000")
        if not 0.1 <= qtimeout <= 3600:   problems.append("queue timeout 0.1-3600")
        if not 0 <= pngc <= 9:            problems.append("PNG compression 0-9")
        if not 0 <= log_retention <= 36500: problems.append("retention 0-36500")
        if not 0 <= stopme_idle <= 86400: problems.append("idle threshold 0-86400")
        host = self.orch_host_entry.get().strip()
        if not host:                      problems.append("orchestrator host")
        if problems:
            self.settings_status.config(
                text="Not saved - check: " + ", ".join(problems), fg=RED)
            return

        storage_err = self._check_storage()
        if storage_err:
            self.settings_status.config(text="Not saved - " + storage_err, fg=RED)
            return

        state.port            = port
        state.queue_batch     = qbatch
        state.queue_timeout   = qtimeout
        state.png_compression = pngc
        state.auto_timestamp_db_name = self.auto_ts_var.get()
        state.log_level          = self.loglevel_cb.get() \
                                   if self.loglevel_cb.get() in LOG_LEVELS else "INFO"
        state.log_retention_days = log_retention
        state.always_on_top      = self.aot_var.get()
        state.stopme_enabled      = self.stopme_var.get()
        state.orchestrator_host   = host
        state.orchestrator_port   = orch_port
        state.stopme_idle_seconds = stopme_idle
        self.attributes("-topmost", state.always_on_top)
        self._apply_storage()
        save_settings()
        self.settings_status.config(
            text="Saved. Port change needs a restart; everything else is live.", fg=GREEN)
        self._log("Settings saved: port=%d queue_batch=%d qtimeout=%.1f pngc=%d" %
                  (port, qbatch, qtimeout, pngc))

    def _toggle_always_on_top(self):
        state.always_on_top = self.aot_var.get()
        self.attributes("-topmost", state.always_on_top)
        save_settings()

    # ── Region management ─────────────────────────────────────────────────────
    def _open_selector(self, *_):
        self.withdraw()
        self.after(200, self._launch_overlay)

    def _launch_overlay(self):
        try:
            overlay = SelectionOverlay(self, self._add_region)
        except Exception as e:
            self.deiconify()
            self._log("Could not open region selector: %s" % e, level="ERROR")
            return
        overlay.win.bind("<Destroy>", lambda _: self.after(50, self.deiconify))

    def _add_region(self, name: str, rect: tuple):
        with state.lock:
            existing = {r["name"] for r in state.regions}
            orig = name
            i = 2
            while name in existing:
                name = "%s_%d" % (orig, i)
                i += 1
            state.regions.append({"name": name, "rect": tuple(int(v) for v in rect)})
            state.counters[name] = 1
        self.region_list.insert(tk.END, name)
        self._log("Region added: %s  %s" % (name, rect))
        self.deiconify()

    def _delete_selected_region(self, *_):
        sel = self.region_list.curselection()
        if not sel:
            return
        idx  = sel[0]
        name = self.region_list.get(idx)
        if not messagebox.askyesno("Remove region",
                                   "Remove '%s'?" % name, parent=self):
            return
        with state.lock:
            state.regions = [r for r in state.regions if r["name"] != name]
            state.counters.pop(name, None)
            state.last_hash.pop(name, None)
        self.region_list.delete(idx)
        self._preview_imgs.pop(name, None)
        self._preview_counts.pop(name, None)
        self._rebuild_preview_grid()
        self._log("Region removed: %s" % name)

    def _clear_regions(self):
        if not messagebox.askyesno("Clear all",
                                   "Remove all capture regions?", parent=self):
            return
        with state.lock:
            state.regions   = []
            state.counters  = {}
            state.last_hash = {}
        self.region_list.delete(0, tk.END)
        self._preview_imgs.clear()
        self._preview_counts.clear()
        self._rebuild_preview_grid()
        self._log("All regions cleared")

    # ── Profile management ────────────────────────────────────────────────────
    def _refresh_profile_list(self):
        self.profile_cb["values"] = sorted(self._profiles.keys(), key=_helper_natural_key)

    def _save_profile(self):
        name = simpledialog.askstring(
            "Save Profile", "Profile name (saves current regions):", parent=self)
        if name is None:
            return
        name = helper_clean_label(name)
        if not name:
            messagebox.showwarning("Invalid name",
                "Profile name must be 1-%d characters, no control characters."
                % MAX_LABEL_LEN, parent=self)
            return
        if name in self._profiles and not messagebox.askyesno(
                "Overwrite profile", "Profile '%s' exists. Overwrite?" % name,
                parent=self):
            return
        with state.lock:
            regions_copy = [dict(r) for r in state.regions]
        previous = self._profiles.get(name)
        self._profiles[name] = regions_copy
        try:
            save_profiles(self._profiles)
        except OSError as e:
            if previous is None:
                self._profiles.pop(name, None)
            else:
                self._profiles[name] = previous
            messagebox.showerror("Save failed", "Could not write profiles: %s" % e,
                                 parent=self)
            return
        self._refresh_profile_list()
        self.profile_var.set(name)
        self._log("Profile saved: %s (%d regions)" % (name, len(regions_copy)))

    def _load_profile(self):
        name = self.profile_var.get()
        if not name or name not in self._profiles:
            messagebox.showwarning("No profile", "Select a profile first.", parent=self)
            return
        if not messagebox.askyesno("Load Profile",
                "Replace current regions with profile '%s'?" % name, parent=self):
            return
        self._load_profile_by_name(name)

    def _load_profile_by_name(self, name: str):
        if name not in self._profiles:
            return
        regions = [dict(r) for r in self._profiles[name]]
        with state.lock:
            state.regions   = regions
            state.counters  = {r["name"]: 1 for r in regions}
            state.last_hash = {}
        self.region_list.delete(0, tk.END)
        for r in regions:
            self.region_list.insert(tk.END, r["name"])
        self._preview_imgs.clear()
        self._preview_counts.clear()
        self._rebuild_preview_grid()
        self._log("Profile loaded: %s (%d regions)" % (name, len(regions)))

    # ── TCP dispatch (core.py protocol) ──────────────────────────────────────
    def _handle_tcp(self, line: str) -> dict:
        if len(line) > 512:
            return core.send_response(False, "ERROR", "command too long")
        words = line.split()
        if not words:
            return core.send_response(False, "ERROR", "empty command")

        cmd = words[0].upper()

        if cmd == "SESSION":
            if len(words) < 2:
                return core.send_response(False, "ERROR", "usage: SESSION <name>")
            name = " ".join(words[1:]).strip()
            try:
                info = self._run_on_main(lambda: self._set_session(name))
            except Exception as e:
                return core.send_response(False, "ERROR",
                                          "session change failed: %s" % e)
            return core.send_response(True, "OK", "session accepted", info)

        if cmd == "DBNAME":
            if len(words) < 2:
                return core.send_response(True, "OK", "current db name", {
                    "db_name":        state.db_name,
                    "active_db_name": state.active_db_name,
                    "db_path":        helper_session_db_path(state.active_db_name),
                    "secondary_path": helper_secondary_db_path(state.active_db_name),
                })
            name = " ".join(words[1:]).strip()
            try:
                info = self._run_on_main(lambda: self._set_db_name(name))
            except Exception as e:
                return core.send_response(False, "ERROR",
                                          "db name change failed: %s" % e)
            self._log("DB name set (TCP): %s" % info["active_db_name"])
            return core.send_response(True, "OK", "db name accepted", info)

        if cmd == "START":
            if not state.regions:
                return core.send_response(False, "ERROR", "no capture regions defined")
            if not helper_has_storage(state.active_db_name):
                return core.send_response(False, "ERROR", NO_STORAGE_MSG)
            state.last_change_time = time.time()
            state.stopme_sent      = False
            state.is_running       = True
            self._log("Capture started (TCP)")
            return core.send_response(True, "CAPTURING", "capture started",
                                      {"session": state.current_session})

        if cmd == "STOP":
            state.is_running = False
            self._log("Capture stopped (TCP)")
            return core.send_response(True, "IDLE", "capture stopped",
                                      {"total": state.total_captured})

        if cmd == "NUM":
            return core.send_response(True, "OK", "frame count",
                                      {"total": state.total_captured})

        if cmd == "STATUS":
            with state.lock:
                data = {
                    "running":    state.is_running,
                    "session":    state.current_session,
                    "version":    state.session_version,
                    "total":      state.total_captured,
                    "regions":    [r["name"] for r in state.regions],
                    "db_name":    state.active_db_name,
                }
            data.update({
                "db_pending": self.store.pending(),
                "db_path":    self.store.db_path or self.store.secondary_path,
                "secondary_path":    self.store.secondary_path,
                "secondary_pending": self.store.secondary_pending(),
                "app_version": APP_VERSION,
            })
            return core.send_response(True, "OK", "status ok", data)

        return core.send_response(False, "ERROR",
                                  "unknown command %r" % words[0],
                                  {"commands": ["SESSION", "DBNAME", "START",
                                                "STOP", "NUM", "STATUS"]})

    # ── Close ─────────────────────────────────────────────────────────────────
    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        was_running      = state.is_running
        state.is_running = False
        state.last_stopped = not was_running
        try:
            save_settings()
            self.update_idletasks()
            self.store.stop()            # flushes pending DB writes
            self.engine.pool.shutdown(wait=False)
            core.logmsg(APP, "=== capture session end ===")
        except Exception as e:
            try:
                core.logmsg(APP, "Error during shutdown: %s" % e)
            except Exception:
                pass
        finally:
            self.destroy()


