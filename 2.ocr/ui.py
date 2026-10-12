import os
import queue
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import core, export_core, ocr_core

PREVIEW_KEYS = {"gray_mode", "lang", "psm", "stage", "regex"}
STATUS_LABELS = {"idle": "Idle", "running": "Running", "stopping": "Stopping...",
                 "done": "Finished", "stopped": "Stopped", "incomplete": "Incomplete",
                 "error": "Error"}
STAT_FIELDS = (("status", "Status"), ("processed", "Processed"),
               ("low_conf", "Low confidence"), ("cores", "Cores in use"),
               ("ram", "RAM"), ("cpu", "CPU"))


class App(tk.Tk):

    def __init__(self, load_config, app_version=""):
        cfg = load_config()
        super().__init__()
        self._load_config, self.cfg = load_config, cfg
        self._state = core.load_state()
        gui = cfg["gui"]
        self.title(f'{gui["title"]} v{app_version}' if app_version else gui["title"])
        self.geometry(gui["window_geometry"])
        self.minsize(int(gui["min_width"]), int(gui["min_height"]))
        self._refresh_ms = int(gui["refresh_ms"])
        self._debounce_ms = int(gui["preview_debounce_ms"])
        self._preview_size = (int(gui["preview_max_width"]), int(gui["preview_max_height"]))
        self._sample_size = (int(gui["sample_max_width"]), int(gui["sample_max_height"]))
        self.export_cfg = cfg["export"]

        self.db_path, self.info, self.tasks = None, None, []
        self.runner, self.results = None, []
        self._reported, self._preparing, self._closing = True, False, False
        self._bgq = queue.Queue()
        self._ocr_seq, self._prev_job, self.preview_row = 0, None, None
        self.preview_img = self.info_img = None
        self.grp = {}

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        tabs = []
        for title in ("1. Files & DB info", "2. OCR settings", "3. Export"):
            frame = ttk.Frame(self.nb)
            self.nb.add(frame, text=title)
            tabs.append(frame)
        self.nb.tab(1, state="disabled")
        self._build_files(tabs[0])
        self._build_settings(tabs[1])
        self._build_export(tabs[2])
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        ocr_core.system_usage()
        self.refresh_files()
        self.after(self._refresh_ms, self._tick)

    @staticmethod
    def _project_dir():
        return os.path.normpath(core.resolve_path("."))

    def _saved_dir(self, key, fallback):
        path = self._state.get(key, "")
        return path if path and os.path.isdir(path) else fallback

    def _remember(self, **values):
        self._state.update(values)
        core.save_state(**values)

    def bg(self, fn, cb):
        def work():
            try:
                outcome = (fn(), None)
            except Exception as exc:
                outcome = (None, exc)
            self._bgq.put((cb,) + outcome)
        threading.Thread(target=work, daemon=True).start()

    def _tick(self):
        while True:
            try:
                cb, res, exc = self._bgq.get_nowait()
            except queue.Empty:
                break
            cb(res, exc)
        self._update_status()
        self.after(self._refresh_ms, self._tick)

    def _tree(self, parent, columns, headings, widths, **kw):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, **kw)
        for col, heading, width in zip(("#0",) + tuple(columns), headings, widths):
            tree.heading(col, text=heading)
            tree.column(col, width=width, anchor="w" if col == "#0" else "e", stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return frame, tree

    def _on_close(self):
        self._closing = True
        if self.runner and self.runner.is_active:
            if not messagebox.askyesno("Quit", "A run is in progress. Stop it and quit?"):
                self._closing = False
                return
            self.runner.stop()
            self._wait_for_stop(time.monotonic() + float(self.cfg["runtime"]["shutdown_wait_s"]))
            return
        self._shutdown()

    def _wait_for_stop(self, deadline):
        if self.runner.finished.is_set() or time.monotonic() >= deadline:
            self._shutdown()
        else:
            self.after(self._refresh_ms, self._wait_for_stop, deadline)

    def _shutdown(self):
        ocr_core.release_preview()
        self.destroy()

    def _build_files(self, tab):
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Label(bar, text="Folder").pack(side="left")
        self.root_var = tk.StringVar(value=self._saved_dir("input_folder", self._project_dir()))
        ttk.Entry(bar, textvariable=self.root_var, width=60).pack(side="left", padx=4)
        ttk.Button(bar, text="Browse...", command=self._browse_root).pack(side="left")
        ttk.Button(bar, text="Refresh", command=self.refresh_files).pack(side="left", padx=6)
        ttk.Button(bar, text="Load", command=self.load_selected).pack(side="left")
        self.files_lbl = ttk.Label(bar, text="")
        self.files_lbl.pack(side="left", padx=10)

        frame, self.ftree = self._tree(tab, ("size", "modified"), ("Database", "Size", "Modified"),
                                       (620, 120, 170), selectmode="browse", height=6)
        frame.pack(fill="x", padx=8)
        self.ftree.bind("<Double-1>", lambda e: self.load_selected(quiet=True))

        self.info_lbl = ttk.Label(tab, text="No database loaded", justify="left")
        self.info_lbl.pack(anchor="w", padx=8, pady=6)
        body = ttk.PanedWindow(tab, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        frame, self.stree = self._tree(body, ("images", "seq", "gaps", "ocr"),
                                       ("Session / version / region", "Images", "Seq range",
                                        "Gaps", "OCR?"), (320, 70, 90, 50, 70))
        body.add(frame, weight=3)
        self.stree.bind("<<TreeviewSelect>>", self._on_session_select)
        side = ttk.LabelFrame(body, text="Sample image (first of selected region)")
        body.add(side, weight=1)
        self.info_prev = ttk.Label(side, text="Select a region")
        self.info_prev.pack(padx=8, pady=8)
        self.info_meta = ttk.Label(side, text="", justify="left")
        self.info_meta.pack(padx=8, anchor="w")

    def _browse_root(self):
        folder = filedialog.askdirectory(initialdir=self.root_var.get(), title="Folder with databases")
        if folder:
            self.root_var.set(folder)
            self.refresh_files()

    def refresh_files(self):
        root = self.root_var.get().strip()
        if not os.path.isdir(root):
            self.files_lbl.config(text=f"Not a folder: {root}")
            return
        self._remember(input_folder=root)
        self.files_lbl.config(text="Scanning...")
        cfg = self.cfg
        self.bg(lambda: ocr_core.find_databases(root, cfg), self._fill_files)

    def _fill_files(self, dbs, exc):
        tree = self.ftree
        tree.delete(*tree.get_children())
        if exc:
            self.files_lbl.config(text=f"Scan failed: {exc}")
            return
        for db in dbs:
            tree.insert("", "end", iid=db["path"], text=db["rel"],
                        values=(core.fmt_size(db["size"]), core.fmt_time(db["mtime"])))
        self.files_lbl.config(text=f"{len(dbs)} database file(s) found")

    def load_selected(self, quiet=False):
        if self.runner and self.runner.is_active:
            messagebox.showwarning("Busy", "Stop the current run before loading another database.")
            return
        selection = self.ftree.selection()
        if not selection:
            if not quiet:
                messagebox.showinfo("Load", "Select a database file first.")
            return
        path, cfg = selection[0], self.cfg
        self.files_lbl.config(text=f"Loading {os.path.basename(path)} ...")
        self.bg(lambda: ocr_core.get_db_info(path, cfg["naming"]["strip_prefix"], cfg),
                lambda info, exc: self._on_loaded(path, info, exc))

    def _on_loaded(self, path, info, exc):
        if exc:
            self.files_lbl.config(text="")
            messagebox.showerror("Load failed", f"{path}\n\n{exc}")
            return
        self.db_path, self.info, self.tasks = path, info, info["tasks"]
        self.results = []
        self.files_lbl.config(text=f"Loaded: {os.path.basename(path)}")
        groups = info["groups"]
        self.info_lbl.config(text=(
            f"{path}   ({core.fmt_size(info['size'])})\n"
            f"{len(groups)} session/version/region groups   |   "
            f"{sum(g['count'] for g in groups):,} images   |   "
            f"{len(self.tasks):,} to OCR (newest versions)   |   {len(info['columns'])} output columns"))
        self._fill_sessions()
        self.nb.tab(1, state="normal")
        self.db_lbl.config(text=f"Database: {path}")
        stem = os.path.splitext(os.path.basename(path))[0]
        self.exp_name.set(f"{stem}.{self.exp_fmt.get()}")
        self.exp_folder.set(self._saved_dir("output_folder", os.path.dirname(path)))
        self._refresh_export_state()
        self.random_preview()

    def _fill_sessions(self):
        tree = self.stree
        tree.delete(*tree.get_children())
        self.grp.clear()
        by_session = {}
        for g in self.info["groups"]:
            by_session.setdefault(g["session"], {}).setdefault(g["version"], []).append(g)
        for session in sorted(by_session, key=core.natural_sort_key):
            versions = by_session[session]
            sid = tree.insert("", "end", text=session, open=False, values=(
                f"{sum(g['count'] for v in versions.values() for g in v):,}", "", "",
                f"v{max(versions)} newest"))
            for version in sorted(versions):
                groups = versions[version]
                vid = tree.insert(sid, "end", text=f"version {version}", values=(
                    f"{sum(g['count'] for g in groups):,}", "", "",
                    "yes" if groups[0]["latest"] else "no"))
                for g in groups:
                    iid = tree.insert(vid, "end", text=g["region"], values=(
                        f"{g['count']:,}", f"{g['min_seq']}-{g['max_seq']}", g["gaps"] or "",
                        "yes" if g["latest"] else "no"))
                    self.grp[iid] = g

    def _on_session_select(self, _event=None):
        selection = self.stree.selection()
        group = self.grp.get(selection[0]) if selection else None
        if not group:
            return
        try:
            b64, meta = ocr_core.make_preview(self.db_path, group["first_id"],
                                              ocr_core.default_params(self.cfg), self.cfg,
                                              "gray", *self._sample_size)
            self.info_img = tk.PhotoImage(data=b64)
            self.info_prev.config(image=self.info_img, text="")
            self.info_meta.config(text=(f"session: {meta['session']}\nregion: {meta['region']}\n"
                                        f"version: {meta['version']}   seq: {meta['seq']}\n"
                                        f"size: {meta['w']}x{meta['h']}"))
        except Exception as exc:
            self.info_prev.config(image="", text=f"Preview failed: {exc}")

    def _build_settings(self, tab):
        defaults = ocr_core.default_params(self.cfg)
        left = ttk.Frame(tab)
        left.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        right = ttk.Frame(tab)
        right.pack(side="right", fill="y", padx=8, pady=8)

        self.db_lbl = ttk.Label(left, text="Database: -")
        self.db_lbl.pack(anchor="w", pady=(0, 6))
        self._build_status_panel(left)
        pages_nb = ttk.Notebook(left)
        pages_nb.pack(fill="both", expand=True)
        pages = []
        for title in ("OCR & run", "Image processing"):
            page = ttk.Frame(pages_nb)
            pages_nb.add(page, text=title)
            page.columnconfigure(0, weight=1)
            page.columnconfigure(1, weight=1)
            pages.append(page)
        self.pvars, self.widgets = {}, {}
        count = [0, 0]
        for section, title, items in ocr_core.PARAM_SPECS:
            is_img = section.startswith("image_")
            n = count[is_img]
            count[is_img] += 1
            group = ttk.LabelFrame(pages[is_img], text=title.replace("Image: ", ""))
            group.grid(row=n // 2, column=n % 2, sticky="nsew", padx=4, pady=4)
            for row, (key, label, kind, choices) in enumerate(items):
                ttk.Label(group, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=2)
                default = defaults[key]
                if kind == "bool":
                    var = tk.BooleanVar(value=bool(default))
                    widget = ttk.Checkbutton(group, variable=var)
                elif kind == "choice":
                    var = tk.StringVar(value=str(default))
                    widget = ttk.Combobox(group, textvariable=var, values=choices,
                                          state="readonly", width=16)
                else:
                    var = tk.StringVar(value=str(default))
                    widget = ttk.Entry(group, textvariable=var, width=26 if key == "regex" else 14)
                widget.grid(row=row, column=1, sticky="w", padx=6, pady=2)
                self.pvars[key], self.widgets[key] = var, (widget, kind)
                if key.startswith("img_") or key in PREVIEW_KEYS:
                    var.trace_add("write", lambda *a: self.show_preview_soon())

        pw, ph = self._preview_size
        prev_frame = ttk.LabelFrame(right, text="Preview - image fed to OCR")
        prev_frame.pack(fill="x")
        box = ttk.Frame(prev_frame, width=pw + 20, height=ph + 20, relief="sunken", borderwidth=1)
        box.pack(padx=8, pady=8)
        box.pack_propagate(False)
        self.prev_lbl = ttk.Label(box, text="No preview yet - press Random", anchor="center")
        self.prev_lbl.pack(expand=True)
        self.prev_info = ttk.Label(prev_frame, text="", justify="left")
        self.prev_info.pack(anchor="w", padx=8)
        result_frame = ttk.LabelFrame(prev_frame, text="OCR result")
        result_frame.pack(fill="x", padx=8, pady=(6, 0))
        self.ocr_text = ttk.Label(result_frame, text="-", font=("TkDefaultFont", 16, "bold"), anchor="w")
        self.ocr_text.pack(fill="x", padx=8, pady=(4, 0))
        self.ocr_info = ttk.Label(result_frame, text="", justify="left")
        self.ocr_info.pack(anchor="w", padx=8, pady=(0, 4))

        ttk.Button(right, text="Random", command=self.random_preview).pack(fill="x", pady=(10, 4))
        self.lock_var = tk.BooleanVar(value=True)
        self.b_lock = ttk.Checkbutton(right, text="Lock settings (no editing)",
                                      variable=self.lock_var, command=self._apply_lock)
        self.b_lock.pack(anchor="w", pady=4)
        self.b_reset = ttk.Button(right, text="Reload parameters from config file",
                                  command=self._reload_params)
        self.b_reset.pack(fill="x", pady=4)
        run_row = ttk.Frame(right)
        run_row.pack(fill="x", pady=(14, 4))
        self.b_run = ttk.Button(run_row, text="Run OCR", command=self.start_run)
        self.b_run.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.b_stop = ttk.Button(run_row, text="Stop", command=self._stop, state="disabled")
        self.b_stop.pack(side="left", expand=True, fill="x")
        self._apply_lock()

    def _build_status_panel(self, parent):
        panel = ttk.LabelFrame(parent, text="Run status")
        panel.pack(side="bottom", fill="x", pady=(8, 0))
        self.stat = {}
        for i, (key, label) in enumerate(STAT_FIELDS):
            row, col = divmod(i, 2)
            ttk.Label(panel, text=f"{label}:").grid(row=row, column=col * 2, sticky="w",
                                                    padx=(8, 2), pady=2)
            var = tk.StringVar(value="-")
            ttk.Label(panel, textvariable=var, font=("TkDefaultFont", 10, "bold")).grid(
                row=row, column=col * 2 + 1, sticky="w", padx=(0, 16))
            self.stat[key] = var
        self.progress_bar = ttk.Progressbar(panel, maximum=100)
        self.progress_bar.grid(row=3, column=0, columnspan=4, sticky="ew", padx=8, pady=(4, 6))
        panel.columnconfigure(1, weight=1)
        panel.columnconfigure(3, weight=1)

    def _apply_lock(self):
        locked = self.lock_var.get()
        for widget, kind in self.widgets.values():
            widget.configure(state="disabled" if locked else ("readonly" if kind == "choice" else "normal"))
        self.b_reset.configure(state="disabled" if locked else "normal")

    def _reload_params(self):
        try:
            self.cfg = self._load_config()
        except core.ConfigError as exc:
            messagebox.showerror("Configuration error", str(exc))
            return
        defaults = ocr_core.default_params(self.cfg)
        for key, var in self.pvars.items():
            var.set(bool(defaults[key]) if isinstance(var, tk.BooleanVar) else str(defaults[key]))

    def _get_params(self):
        return ocr_core.coerce_params({k: v.get() for k, v in self.pvars.items()}, self.cfg)

    def random_preview(self):
        if not self.tasks:
            return
        self.preview_row = random.choice(self.tasks)[2]
        self.show_preview()

    def show_preview_soon(self):
        if self._prev_job:
            self.after_cancel(self._prev_job)
        self._prev_job = self.after(self._debounce_ms, self.show_preview)

    def show_preview(self):
        self._prev_job = None
        if self.preview_row is None or not self.db_path:
            return
        try:
            params = self._get_params()
        except ValueError as exc:
            self.ocr_text.config(text="-")
            self.ocr_info.config(text=f"Invalid parameter: {exc}")
            return
        try:
            b64, meta = ocr_core.make_preview(self.db_path, self.preview_row, params, self.cfg,
                                              params["stage"], *self._preview_size)
        except Exception as exc:
            self.prev_lbl.config(image="", text=f"Preview failed: {exc}")
            return
        self.preview_img = tk.PhotoImage(data=b64)
        self.prev_lbl.config(image=self.preview_img, text="")
        self.prev_info.config(text=f"{meta['session']} / {meta['region']}  #{meta['seq']}   "
                                   f"{meta['w']}x{meta['h']} px   stage: {meta['stage']}")
        self._ocr_seq += 1
        seq, db, row, cfg = self._ocr_seq, self.db_path, self.preview_row, self.cfg
        self.ocr_info.config(text="Running OCR...")
        self.bg(lambda: ocr_core.ocr_preview(db, row, params, cfg),
                lambda res, exc: self._ocr_done(seq, res, exc))

    def _ocr_done(self, seq, res, exc):
        if seq != self._ocr_seq:
            return
        if exc:
            self.ocr_text.config(text="-")
            self.ocr_info.config(text=f"OCR failed: {exc}")
            return
        self.ocr_text.config(text=res["text"] or "(empty)")
        self.ocr_info.config(text=f"confidence {res['confidence']:.1f}   pass: {res['preprocess']}"
                                  + ("   SUSPICIOUS" if res["suspicious"] else ""))

    def _set_run_controls(self, running):
        self.lock_var.set(running)
        self._apply_lock()
        self.b_lock.configure(state="disabled" if running else "normal")
        self.b_run.configure(state="disabled" if running else "normal")
        self.b_stop.configure(state="disabled")

    def start_run(self):
        if not self.db_path:
            return
        if self.runner and self.runner.is_active:
            messagebox.showwarning("Busy", "A run is already in progress.")
            return
        try:
            params = self._get_params()
        except ValueError as exc:
            messagebox.showerror("Invalid parameter", str(exc))
            return
        self._set_run_controls(True)
        self.runner, self.results, self._preparing = None, [], True
        self._refresh_export_state()
        db, cfg = self.db_path, self.cfg
        self.bg(lambda: ocr_core.scan_tasks(db, params["strip_prefix"], cfg),
                lambda scanned, exc: self._launch_runner(params, scanned, exc))

    def _launch_runner(self, params, scanned, exc):
        self._preparing = False
        if exc:
            self._set_run_controls(False)
            messagebox.showerror("Run failed", f"Could not read the database:\n{exc}")
            return
        tasks, _columns = scanned
        self.runner = ocr_core.OcrRunner(self.db_path, params, tasks, self.cfg)
        self._reported = False
        self.b_stop.configure(state="normal")
        self.runner.start()

    def _stop(self):
        if self.runner and self.runner.is_active:
            self.runner.stop()

    def _update_status(self):
        usage = ocr_core.system_usage()
        stat = self.stat
        stat["ram"].set(f"{usage['ram_percent']:.1f}%  ({core.fmt_size(usage['ram_used'])} "
                        f"/ {core.fmt_size(usage['ram_total'])})")
        stat["cpu"].set(f"{usage['cpu_percent']:.1f}%")
        run = self.runner
        if run is None:
            stat["status"].set("Preparing..." if self._preparing else STATUS_LABELS["idle"])
            stat["processed"].set("-")
            stat["low_conf"].set("-")
            stat["cores"].set(f"- / {usage['cores_total']}")
            self.progress_bar["value"] = 0
            return
        snap = run.snapshot()
        status = STATUS_LABELS.get(snap["state"], snap["state"])
        if snap["paused"] and snap["state"] == "running":
            status += " (paused: RAM limit)"
        done, total = snap["processed"], snap["total"]
        stat["status"].set(status)
        stat["processed"].set(f"{done:,} / {total:,}")
        stat["low_conf"].set(f"{snap['low_confidence']:,} / {total:,}")
        stat["cores"].set(f"{snap['workers_used']} / {snap['cores_total']}")
        self.progress_bar["value"] = 100.0 * done / total if total else 0
        if not run.is_active and run.finished.is_set() and not self._reported and not self._closing:
            self._reported = True
            self._on_run_done(run)

    def _on_run_done(self, run):
        self.results = run.rows
        self._set_run_controls(False)
        self._refresh_export_state()
        if run.state == "error":
            messagebox.showerror("Run failed", run.error or "unknown error")
            return
        done, total = run.progress()
        suspicious = sum(1 for row in run.rows if row[core.SUSPICIOUS_IDX])
        messagebox.showinfo("Run finished",
                            f"State: {run.state}\nProcessed: {done:,} / {total:,}\n"
                            f"Low confidence: {run.low_confidence:,} / {total:,}\n"
                            f"Suspicious: {suspicious:,}\n\nOpen the Export tab to export.")

    def _build_export(self, tab):
        source = ttk.LabelFrame(tab, text="Results")
        source.pack(fill="x", padx=8, pady=6)
        self.exp_info = ttk.Label(source, text="No results yet - run OCR on tab 2.")
        self.exp_info.pack(anchor="w", padx=8, pady=8)

        out = ttk.LabelFrame(tab, text="Output (one file)")
        out.pack(fill="x", padx=8, pady=6)
        self.exp_folder = tk.StringVar()
        self.exp_name = tk.StringVar(value=self.export_cfg["file_name"])
        self.exp_fmt = tk.StringVar(value=self.export_cfg["format"])
        ttk.Label(out, text="Folder").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(out, textvariable=self.exp_folder, width=70).grid(row=0, column=1, padx=4)
        ttk.Button(out, text="Browse...", command=self._browse_out).grid(row=0, column=2)
        ttk.Label(out, text="File name").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(out, textvariable=self.exp_name, width=40).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(out, text="Format").grid(row=2, column=0, sticky="w", padx=6, pady=3)
        ttk.Combobox(out, textvariable=self.exp_fmt, values=list(export_core.FORMATS),
                     state="readonly", width=8).grid(row=2, column=1, sticky="w", padx=4)

        row = ttk.Frame(tab)
        row.pack(fill="x", padx=8, pady=8)
        self.b_export = ttk.Button(row, text="Export", command=self.do_export, state="disabled")
        self.b_export.pack(side="left")
        ttk.Button(row, text="Open folder", command=self._open_folder).pack(side="left", padx=6)
        self.exp_status = ttk.Label(row, text="")
        self.exp_status.pack(side="left", padx=10)

    def _browse_out(self):
        folder = filedialog.askdirectory(initialdir=self.exp_folder.get() or self._project_dir())
        if folder:
            self.exp_folder.set(folder)
            self._remember(output_folder=folder)

    def _refresh_export_state(self):
        n = len(self.results)
        self.exp_info.config(text=f"{n:,} OCR result(s) ready to export." if n
                             else "No results yet - run OCR on tab 2.")
        self.b_export.config(state="normal" if n else "disabled")

    def do_export(self):
        if not self.results:
            messagebox.showerror("Export", "Run OCR first.")
            return
        if not self.exp_name.get().strip() or not self.exp_folder.get().strip():
            messagebox.showerror("Export", "Folder and file name are required.")
            return
        target = os.path.join(self.exp_folder.get().strip(), self.exp_name.get().strip())
        rows, fmt, cfg = list(self.results), self.exp_fmt.get(), self.cfg
        self.b_export.config(state="disabled")
        self.exp_status.config(text="Exporting...")
        self.bg(lambda: export_core.export_results(rows, target, fmt, cfg), self._export_done)

    def _export_done(self, path, exc):
        self.b_export.config(state="normal")
        if isinstance(exc, export_core.ExportTooLarge):
            self.exp_status.config(text="Too many rows for xlsx")
            messagebox.showwarning("Too many rows", str(exc))
            return
        if exc:
            self.exp_status.config(text="Export failed")
            messagebox.showerror("Export failed", str(exc))
            return
        self._remember(output_folder=os.path.dirname(os.path.abspath(path)))
        self.exp_status.config(text=f"Wrote {os.path.basename(path)}")
        messagebox.showinfo("Export finished", path)

    def _open_folder(self):
        folder = self.exp_folder.get()
        if not os.path.isdir(folder):
            return
        if os.name == "nt":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
