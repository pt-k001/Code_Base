import os
import queue
import shutil
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from core import core
from core.chart_engine import ChartWidget, save_chart_png
from core.data_feed import DataFeed
from core.db import ModuleDB
from core.excel_exporter import export_to_excel
from core.module_loader import ModuleLoader

_MOD_PREFIX = "mod:"
_CHART_PREFIX = "chart:"
_CHECK_ON = "☑"
_CHECK_OFF = "☐"
_CHECK_MIXED = "▣"
_BOLD = ("TkDefaultFont", 11, "bold")


class App:
    def __init__(self, root, cfg, version=""):
        self.root = root
        self.cfg = cfg
        self.windows = cfg["time_windows"]
        self.refresh_options = cfg["refresh_options"]

        title = cfg["gui"]["title"]
        root.title(f"{title} v{version}" if version else title)
        root.geometry(cfg["gui"]["window_geometry"])
        root.minsize(cfg["gui"]["min_width"], cfg["gui"]["min_height"])
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.live_db_path = core.service_file("Service_profiles", cfg["storage"]["db_file"])
        self.loader = ModuleLoader(
            os.path.join(core.repo_root(), "modules"),
            core.service_file("Service_profiles", cfg["storage"]["state_file"]))
        self.feed = DataFeed()
        self.db = ModuleDB(self.live_db_path)
        self.msg_queue = queue.Queue()
        self.chart_widgets: dict[str, ChartWidget] = {}
        self.grouped_sections: dict = {}
        self.grouped_frames: dict = {}
        self.hidden_charts: set[str] = set()
        self.loaded_db_path = None
        self.running = True
        self._cycle_running = False
        self._refresh_job = None
        self.auto_refresh_enabled = True

        self._build_notebook()
        self._build_dashboard_tab()
        self._build_settings_tab()
        self._discover_modules(initial=True)
        self._schedule_next_cycle()
        root.after(cfg["refresh"]["queue_poll_ms"], self._poll_queue)

    # ── modules ───────────────────────────────────────────────────────────────
    def _discover_modules(self, initial=False):
        for message in self.loader.refresh():
            self.append_log(message)
        for lm in self.loader.all_modules():
            if lm.enabled and lm.load_error is None:
                self.db.ensure_schema(lm.id, lm.instance.db_schema)
        self._rebuild_module_list()
        self._rebuild_chart_tree()
        if not initial:
            self._rebuild_charts()

    def _on_toggle_module(self, mod_id, enabled):
        self.loader.set_enabled(mod_id, enabled)
        if enabled:
            self.db.ensure_schema(mod_id, self.loader.modules[mod_id].instance.db_schema)
        self.append_log(f"Module {mod_id} {'enabled' if enabled else 'disabled'}.")
        self._rebuild_chart_tree()
        self._rebuild_charts()
        self._render_charts()

    # ── layout ────────────────────────────────────────────────────────────────
    def _build_notebook(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        self.dashboard_tab = tk.Frame(self.notebook)
        self.settings_tab = tk.Frame(self.notebook)
        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.settings_tab, text="Settings")

    def _build_dashboard_tab(self):
        top = tk.Frame(self.dashboard_tab)
        top.pack(fill="x", padx=8, pady=6)

        tk.Label(top, text="Log file:").grid(row=0, column=0, sticky="w")
        self.path_var = tk.StringVar(value=self.cfg["paths"]["log_file"])
        tk.Entry(top, textvariable=self.path_var, width=50).grid(row=0, column=1, padx=4)
        tk.Button(top, text="Browse...", command=self._on_browse).grid(row=0, column=2)
        tk.Button(top, text="Load", command=self._on_load).grid(row=0, column=3, padx=4)

        tk.Label(top, text="Window:").grid(row=0, column=4, padx=(12, 2))
        self.window_var = tk.StringVar(value=self.cfg["charts"]["default_window"])
        tk.OptionMenu(top, self.window_var, *self.windows,
                      command=lambda _: self._request_refresh()).grid(row=0, column=5)

        tk.Button(top, text="Refresh Now", command=self._request_refresh).grid(row=0, column=6, padx=8)
        tk.Button(top, text="Export to Excel", command=self._on_export).grid(row=0, column=7)
        tk.Button(top, text="Save Database", command=self._on_save_database).grid(row=0, column=8, padx=(4, 0))
        tk.Button(top, text="Load Database", command=self._on_load_database).grid(row=0, column=9, padx=(4, 0))

        row2 = tk.Frame(self.dashboard_tab)
        row2.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(row2, text="Auto-refresh every:").pack(side="left")
        self.refresh_var = tk.StringVar(value=self.cfg["refresh"]["default"])
        tk.OptionMenu(row2, self.refresh_var, *self.refresh_options,
                      command=lambda _: self.append_log(
                          f"Auto-refresh interval set to {self.refresh_var.get()}.")
                      ).pack(side="left", padx=(2, 12))
        self.auto_refresh_btn = tk.Button(row2, text="Stop Auto-Refresh", command=self._toggle_auto_refresh)
        self.auto_refresh_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Idle")
        tk.Label(self.dashboard_tab, textvariable=self.status_var, anchor="w").pack(fill="x", padx=8)

        split = tk.Frame(self.dashboard_tab)
        split.pack(fill="both", expand=True, padx=8, pady=4)
        self._build_chart_tree(split)
        self._build_chart_area(split)

        log_frame = tk.Frame(self.dashboard_tab)
        log_frame.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(log_frame, text="Parser log:").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=self.cfg["gui"]["log_height"], wrap="none", state="disabled")
        self.log_text.pack(fill="x")

    def _build_chart_area(self, parent):
        outer = tk.Frame(parent)
        outer.pack(side="left", fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.scroll_frame = tk.Frame(canvas)
        window = canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.scroll_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        canvas.bind("<Button-4>", lambda _e: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda _e: canvas.yview_scroll(1, "units"))

    def _build_settings_tab(self):
        wrap = tk.Frame(self.settings_tab)
        wrap.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(wrap, text="Attached Modules", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        tk.Button(wrap, text="Rescan modules/", command=self._discover_modules).pack(anchor="w", pady=(4, 10))
        self.modules_list_frame = tk.Frame(wrap)
        self.modules_list_frame.pack(fill="x")

    def _rebuild_module_list(self):
        for child in self.modules_list_frame.winfo_children():
            child.destroy()
        for lm in self.loader.all_modules():
            row = tk.Frame(self.modules_list_frame)
            row.pack(fill="x", pady=2)
            var = tk.BooleanVar(value=lm.enabled and lm.load_error is None)
            tk.Checkbutton(
                row, variable=var, state="disabled" if lm.load_error else "normal",
                command=lambda mid=lm.id, v=var: self._on_toggle_module(mid, v.get()),
            ).pack(side="left")
            status = f"  [{lm.load_error}]" if lm.load_error else ""
            tk.Label(row, text=f"{lm.instance.name}  (v{lm.instance.version}){status}").pack(side="left")

    # ── chart visibility tree ─────────────────────────────────────────────────
    def _build_chart_tree(self, parent):
        wrap = tk.Frame(parent, width=self.cfg["gui"]["tree_width"])
        wrap.pack(side="left", fill="y", padx=(0, 8))
        wrap.pack_propagate(False)
        tk.Label(wrap, text="Charts", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")

        frame = tk.Frame(wrap)
        frame.pack(fill="both", expand=True)
        vsb = tk.Scrollbar(frame, orient="vertical")
        self.chart_tree = ttk.Treeview(frame, show="tree", selectmode="none", yscrollcommand=vsb.set)
        vsb.config(command=self.chart_tree.yview)
        self.chart_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.chart_tree.bind("<Button-1>", self._on_tree_click)

        buttons = tk.Frame(wrap)
        buttons.pack(fill="x", pady=(4, 0))
        tk.Button(buttons, text="Show all", command=self._show_all_charts).pack(side="left")
        tk.Button(buttons, text="Hide all", command=self._hide_all_charts).pack(side="left", padx=(4, 0))

    def _rebuild_chart_tree(self):
        self.chart_tree.delete(*self.chart_tree.get_children())
        self._tree_titles = {}
        for module in self.loader.enabled_modules():
            mod_iid = f"{_MOD_PREFIX}{module.id}"
            self._tree_titles[mod_iid] = module.name
            self.chart_tree.insert("", "end", iid=mod_iid, open=True)
            for spec in module.chart_specs:
                chart_id = f"{module.id}:{spec.key}"
                iid = f"{_CHART_PREFIX}{chart_id}"
                self._tree_titles[iid] = spec.title.replace("{group}", "all") if spec.group_by else spec.title
                self.chart_tree.insert(mod_iid, "end", iid=iid)
                self._refresh_tree_item(iid)
            self._refresh_tree_item(mod_iid)

    def _child_chart_ids(self, mod_iid):
        return [c[len(_CHART_PREFIX):] for c in self.chart_tree.get_children(mod_iid)]

    def _refresh_tree_item(self, iid):
        if iid.startswith(_CHART_PREFIX):
            hidden = iid[len(_CHART_PREFIX):] in self.hidden_charts
            glyph = _CHECK_OFF if hidden else _CHECK_ON
        else:
            ids = self._child_chart_ids(iid)
            count = sum(1 for cid in ids if cid in self.hidden_charts)
            glyph = _CHECK_ON if count == 0 else _CHECK_OFF if count == len(ids) else _CHECK_MIXED
        self.chart_tree.item(iid, text=f"{glyph} {self._tree_titles[iid]}")

    def _on_tree_click(self, event):
        iid = self.chart_tree.identify_row(event.y)
        if not iid:
            return
        if iid.startswith(_CHART_PREFIX):
            self.hidden_charts ^= {iid[len(_CHART_PREFIX):]}
            self._refresh_tree_item(iid)
            self._refresh_tree_item(self.chart_tree.parent(iid))
        else:
            ids = self._child_chart_ids(iid)
            if ids and all(cid in self.hidden_charts for cid in ids):
                self.hidden_charts -= set(ids)
            else:
                self.hidden_charts |= set(ids)
            for child in self.chart_tree.get_children(iid):
                self._refresh_tree_item(child)
            self._refresh_tree_item(iid)
        self._rebuild_charts()
        self._render_charts()

    def _show_all_charts(self):
        self.hidden_charts.clear()
        self._rebuild_chart_tree()
        self._rebuild_charts()
        self._render_charts()

    def _hide_all_charts(self):
        for module in self.loader.enabled_modules():
            self.hidden_charts.update(f"{module.id}:{spec.key}" for spec in module.chart_specs)
        self._rebuild_chart_tree()
        self._rebuild_charts()

    # ── chart building ────────────────────────────────────────────────────────
    def _rebuild_charts(self):
        for widget in self.chart_widgets.values():
            widget.destroy()
        self.chart_widgets.clear()
        self.grouped_frames.clear()
        self.grouped_sections.clear()
        for child in self.scroll_frame.winfo_children():
            child.destroy()

        height = self.cfg["charts"]["height"]
        for module in self.loader.enabled_modules():
            visible = [s for s in module.chart_specs if f"{module.id}:{s.key}" not in self.hidden_charts]
            if not visible:
                continue
            tk.Label(self.scroll_frame, text=module.name, font=_BOLD).pack(anchor="w", pady=(10, 0))
            for spec in (s for s in visible if not s.group_by):
                chart_id = f"{module.id}:{spec.key}"
                self.chart_widgets[chart_id] = ChartWidget(
                    self.scroll_frame, chart_id, height=height,
                    on_save=self._save_chart, on_save_as=self._save_chart_as)
            grouped = [s for s in visible if s.group_by]
            if grouped:
                container = tk.Frame(self.scroll_frame)
                container.pack(fill="both", expand=True)
                self.grouped_sections[module.id] = {"container": container, "specs": grouped}

    def _ensure_group_frame(self, module, group_val):
        key = (module.id, group_val)
        if key not in self.grouped_frames:
            section = self.grouped_sections[module.id]
            frame = tk.LabelFrame(section["container"], text=str(group_val))
            frame.pack(fill="both", expand=True, pady=(6, 2))
            widgets = {
                spec.key: ChartWidget(frame, f"{module.id}:{spec.key}:{group_val}",
                                      height=self.cfg["charts"]["group_height"],
                                      on_save=self._save_chart, on_save_as=self._save_chart_as)
                for spec in section["specs"]
            }
            self.grouped_frames[key] = {"frame": frame, "widgets": widgets}
        return self.grouped_frames[key]

    # ── load / refresh cycle ──────────────────────────────────────────────────
    def _on_browse(self):
        path = filedialog.askopenfilename(
            filetypes=[("Log files", "*.log"), ("All files", "*.*")],
            **core.dialog_start(self.path_var.get().strip()))
        if path:
            self.path_var.set(path)
            self._remember("log_file", path)

    def _busy(self) -> bool:
        if self._cycle_running:
            messagebox.showinfo("Busy", "A load/refresh is in progress. Please wait for it to finish.")
        return self._cycle_running

    def _on_load(self):
        path = self.path_var.get().strip()
        if not os.path.isfile(path):
            messagebox.showerror("Invalid path", "Select a valid log file.")
            return
        if self._busy():
            return
        if self.loaded_db_path:
            self.db.close()
            self.db = ModuleDB(self.live_db_path)
            self.loaded_db_path = None
            for lm in self.loader.all_modules():
                if lm.enabled and lm.load_error is None:
                    self.db.ensure_schema(lm.id, lm.instance.db_schema)
        self._remember("log_file", path)
        self.feed.set_filepath(path)
        for lm in self.loader.all_modules():
            lm.instance.reset()
            self.db.clear_table(lm.id)
        self._rebuild_charts()
        self.status_var.set("Loading...")
        self._start_cycle()

    def _request_refresh(self):
        if self._cycle_running:
            self.append_log("Refresh already in progress, skipping.")
        else:
            self._start_cycle()

    def _start_cycle(self):
        self._cycle_running = True
        threading.Thread(target=self._do_cycle, daemon=True).start()

    def _toggle_auto_refresh(self):
        self.auto_refresh_enabled = not self.auto_refresh_enabled
        self.auto_refresh_btn.config(
            text="Stop Auto-Refresh" if self.auto_refresh_enabled else "Start Auto-Refresh")
        self.append_log("Auto-refresh " + ("resumed." if self.auto_refresh_enabled else "stopped."))
        if self.auto_refresh_enabled:
            self._schedule_next_cycle()
        elif self._refresh_job is not None:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None

    def _schedule_next_cycle(self):
        if self._refresh_job is not None:
            self.root.after_cancel(self._refresh_job)
        seconds = self.refresh_options[self.refresh_var.get()]
        if self.feed.reader.filepath and not self._cycle_running:
            self.status_var.set(f"Waiting for next refresh ({self.refresh_var.get()})...")
        self._refresh_job = self.root.after(seconds * 1000, self._trigger_cycle)

    def _trigger_cycle(self):
        self._refresh_job = None
        if not self.running or not self.auto_refresh_enabled:
            return
        if self.feed.reader.filepath and not self._cycle_running:
            self._start_cycle()
        self._schedule_next_cycle()

    def _do_cycle(self):
        q = self.msg_queue
        processed = total = 0
        log_every = 1
        try:
            enabled = self.loader.enabled_modules()
            q.put(("status", "Reading log file..."))
            for item in self.feed.poll_with_progress():
                if item[0] == "count":
                    total = item[1]
                    if total:
                        q.put(("log", f"Found {total} record(s) to process."))
                        log_every = max(1, total // self.cfg["feed"]["progress_lines"])
                    else:
                        q.put(("status", "No new records."))
                    continue

                _, processed, total, ts, cmd, body = item
                for module in enabled:
                    if not module.matches(cmd):
                        continue
                    result = module.parse(body, ts)
                    for row in (result if isinstance(result, list) else [result] if result else []):
                        self.db.insert_row(module.id, ts.isoformat(), row, commit=False)
                        line = module.log_line(row)
                        if line:
                            q.put(("log", line))

                if processed % log_every == 0 or processed == total:
                    self.db.commit()
                    q.put(("log", f"Processing {processed} of {total}..."))
                    q.put(("status", f"Parsing {processed} of {total}..."))

            self.db.commit()
            if total:
                q.put(("log", f"Done: processed {processed} of {total} record(s)."))
            q.put(("status", "Rendering charts..."))
            q.put(("refresh_charts", None))
            q.put(("status", "Last updated: " + datetime.now().strftime("%H:%M:%S")))
        except Exception as exc:
            q.put(("log", f"Error: {exc}"))
            q.put(("refresh_charts", None))
        finally:
            self._cycle_running = False

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                try:
                    if kind == "refresh_charts":
                        self._render_charts()
                    elif kind == "status":
                        self.status_var.set(payload)
                    elif kind == "log":
                        self.append_log(payload)
                except Exception as exc:
                    self.append_log(f"UI update error ({kind}): {exc}")
        except queue.Empty:
            pass
        finally:
            if self.running:
                self.root.after(self.cfg["refresh"]["queue_poll_ms"], self._poll_queue)

    # ── rendering ─────────────────────────────────────────────────────────────
    def _render_charts(self):
        minutes = self.windows[self.window_var.get()]
        inactive_after = timedelta(minutes=self.cfg["charts"]["inactive_after_min"])

        for module in self.loader.enabled_modules():
            latest = self.db.latest_time(module.id)
            try:
                now = datetime.fromisoformat(latest) if latest else datetime.now()
            except ValueError:
                now = datetime.now()
            cutoff = (now - timedelta(minutes=minutes)).isoformat()

            for spec in module.chart_specs:
                widget = self.chart_widgets.get(f"{module.id}:{spec.key}")
                if spec.group_by or widget is None:
                    continue
                rows = self.db.query_window(module.id, [spec.value_col], cutoff)
                times, values = self._rows_to_series(rows)
                widget.plot(times, values, spec.title, spec.ylabel, spec.ylim, minutes, now)

            section = self.grouped_sections.get(module.id)
            if not section:
                continue
            group_col = section["specs"][0].group_by
            for group_val in self.db.distinct_values(module.id, group_col):
                entry = self._ensure_group_frame(module, group_val)
                inactive = False
                last_seen = self.db.latest_time_for_value(module.id, group_col, group_val)
                if last_seen:
                    try:
                        inactive = now - datetime.fromisoformat(last_seen) > inactive_after
                    except ValueError:
                        pass
                entry["frame"].configure(text=f"{group_val}" + ("  (inactive)" if inactive else ""))

                for spec in section["specs"]:
                    rows = self.db.query_window_grouped(
                        module.id, [spec.value_col], cutoff, group_col, group_val)
                    times, values = self._rows_to_series(rows)
                    entry["widgets"][spec.key].plot(
                        times, values, spec.title.replace("{group}", str(group_val)),
                        spec.ylabel, spec.ylim, minutes, now)

    def _rows_to_series(self, rows):
        times, values = [], []
        for time_str, value in rows:
            if value is None:
                continue
            try:
                times.append(datetime.fromisoformat(time_str))
            except ValueError:
                continue
            values.append(value)
        if len(times) < 3:
            return times, values

        deltas = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        positive = sorted(d for d in deltas if d > 0)
        if not positive:
            return times, values
        threshold = positive[len(positive) // 2] * self.cfg["feed"]["max_gap_samples"]
        out_t, out_v = [times[0]], [values[0]]
        for i in range(1, len(times)):
            if deltas[i - 1] > threshold:
                out_t.append(times[i - 1] + (times[i] - times[i - 1]) / 2)
                out_v.append(float("nan"))
            out_t.append(times[i])
            out_v.append(values[i])
        return out_t, out_v

    # ── output files ──────────────────────────────────────────────────────────
    def _output_folder(self):
        source = self.feed.reader.filepath or self.loaded_db_path
        if not source:
            return None
        base = os.path.splitext(os.path.basename(source))[0]
        folder = os.path.join(os.path.dirname(source) or ".", base)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            folder = os.path.join(os.path.dirname(self.live_db_path), base)
            os.makedirs(folder, exist_ok=True)
        return folder, base

    def _save_chart(self, fig, chart_id):
        result = self._output_folder()
        if not result:
            self.append_log("Save failed: load a log file or database first.")
            return
        save_chart_png(fig, chart_id, *result, self.cfg["charts"]["png_dpi"], log_cb=self.append_log)

    def _save_chart_as(self, fig, title):
        start = core.dialog_start(self.cfg["paths"]["chart_file"])
        path = ChartWidget._save_as(fig, title, initialdir=start.get("initialdir"))
        if path:
            self._remember("chart_file", path)

    def _remember(self, key, value):
        self.cfg["paths"][key] = value
        core.remember_path(key, value)

    def _on_export(self):
        result = self._output_folder()
        if not result:
            messagebox.showerror("Export failed", "Load a log file or database first.")
            return
        folder, base = result
        path = os.path.join(folder, f"{base}.xlsx")
        self.status_var.set("Exporting to Excel...")
        self.root.update_idletasks()
        try:
            export_to_excel(path, self.db, self.loader.enabled_modules())
        except Exception as exc:
            self.status_var.set("Export failed.")
            messagebox.showerror("Export failed", str(exc))
            return
        self.status_var.set(f"Export complete: {os.path.basename(path)}")
        self.append_log(f"Exported Excel workbook: {path}")
        messagebox.showinfo("Export complete", f"Saved:\n{path}")

    def _on_save_database(self):
        result = self._output_folder()
        if not result:
            messagebox.showerror("Save failed", "Load a log file or database first.")
            return
        folder, base = result
        dest = os.path.join(folder, f"{base}_metrics.sqlite")
        self.status_var.set("Saving database...")
        self.root.update_idletasks()
        try:
            self.db.commit()
            if os.path.exists(dest) and os.path.samefile(self.db.path, dest):
                raise ValueError("The database is already stored at that location.")
            shutil.copy2(self.db.path, dest)
        except Exception as exc:
            self.status_var.set("Database save failed.")
            messagebox.showerror("Save failed", str(exc))
            return
        self.status_var.set(f"Database saved: {os.path.basename(dest)}")
        self.append_log(f"Saved database copy: {dest}")
        messagebox.showinfo("Database saved", f"Saved:\n{dest}")

    def _on_load_database(self):
        path = filedialog.askopenfilename(
            filetypes=[("SQLite database", "*.sqlite"), ("All files", "*.*")],
            **core.dialog_start(self.cfg["paths"]["database_file"]))
        if not path or self._busy():
            return
        try:
            new_db = ModuleDB(path)
            new_db.conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        except Exception as exc:
            messagebox.showerror("Load failed", f"Could not open database:\n{exc}")
            return

        self._remember("database_file", path)
        self.db.close()
        self.db = new_db
        self.loaded_db_path = path
        for lm in self.loader.all_modules():
            if lm.enabled and lm.load_error is None:
                self.db.ensure_schema(lm.id, lm.instance.db_schema)
        self.feed.reader.filepath = None
        self.path_var.set("")
        self._rebuild_charts()
        self._render_charts()
        self.status_var.set(f"Loaded database: {os.path.basename(path)}")
        self.append_log(f"Loaded database: {path}")

    # ── shutdown / log ────────────────────────────────────────────────────────
    def _on_close(self):
        if self._cycle_running:
            proceed = messagebox.askyesno(
                "Exit", "A log refresh is still in progress. Exit anyway?", icon="warning")
        else:
            proceed = messagebox.askokcancel("Exit", "Are you sure you want to exit?")
        if not proceed:
            return
        self.running = False
        try:
            self.db.commit()
        except Exception:
            pass
        self.root.destroy()

    def append_log(self, message):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n"

        def write():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        try:
            self.root.after(0, write)
        except Exception:
            pass
