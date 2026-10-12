import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from core import analysis_core as ac, core, export_core

ORDER_ASC, ORDER_DESC = "Lowest first", "Highest first"
MATRIX_KEYS = {"Pass %": "pass_pct", "Pass count": "n_pass",
               "Rows": "total", "Best of sort column": "best"}


def fmt(v, nd=3):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def pct(v):
    return "-" if v is None else f"{v:.1f}%"


def tone(p):
    return None if p is None else "good" if p >= 99.5 else "warn" if p >= 50 else "bad"


def make_table(parent, show="headings"):
    fr = ttk.Frame(parent)
    t = ttk.Treeview(fr, show=show)
    sy = ttk.Scrollbar(fr, orient="vertical", command=t.yview)
    sx = ttk.Scrollbar(fr, orient="horizontal", command=t.xview)
    t.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    sy.pack(side="right", fill="y")
    sx.pack(side="bottom", fill="x")
    t.pack(side="left", fill="both", expand=True)
    return fr, t


def _sort_tree(tree, col, reverse):
    items = [(tree.set(i, col), i) for i in tree.get_children("")]

    def key(x):
        s = x[0].replace("%", "").replace(",", "")
        try:
            return (0, float(s), "")
        except ValueError:
            return (1, 0.0, x[0].lower())
    items.sort(key=key, reverse=reverse)
    for idx, (_, iid) in enumerate(items):
        tree.move(iid, "", idx)
    tree.heading(col, command=lambda: _sort_tree(tree, col, not reverse))


def fill_table(tree, columns, rows, width=90, first_width=170, nd=3, tags=None, widths=None):
    tree.tag_configure("good", background="#e3f3e6", foreground="#14532d")
    tree.tag_configure("warn", background="#fdf1d8", foreground="#7a4b00")
    tree.tag_configure("bad", background="#fbe4e4", foreground="#7f1d1d")
    tree.delete(*tree.get_children())
    tree["columns"] = list(columns)
    widths = widths or {}
    for i, c in enumerate(columns):
        tree.heading(c, text=c, command=lambda c=c: _sort_tree(tree, c, False))
        w = widths.get(c, first_width if i == 0 else width)
        tree.column(c, width=w, anchor="w" if i == 0 or widths.get(c, 0) > 200 else "e",
                    stretch=True)
    for n, r in enumerate(rows):
        tree.insert("", "end", values=[fmt(v, nd) for v in r],
                    tags=(tags[n],) if tags and tags[n] else ())


class MetricFilterTable(ttk.Frame):

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self._rows = []
        self.saved = {}
        self.saved_sort = ""
        self._sort_var = tk.StringVar(value="")
        hdr = ttk.Frame(self)
        hdr.pack(fill="x")
        for text, w in (("Use", 5), ("Metric", 30), ("Min", 11), ("Max", 11)):
            ttk.Label(hdr, text=text, width=w).pack(side="left", padx=3, pady=3)
        ttk.Separator(self).pack(fill="x")
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._inner = ttk.Frame(self._canvas)
        self._win = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(self._win, width=e.width))

    @property
    def sort_var(self):
        return self._sort_var

    @property
    def rows(self):
        return self._rows

    @staticmethod
    def _txt(v):
        return "" if v in ("", None) else f"{float(v):g}"

    def populate(self, metric_names, defaults, default_sort):
        for w in self._inner.winfo_children():
            w.destroy()
        self._rows.clear()
        self._sort_var.set("")
        lowered = {k.lower(): v for k, v in defaults.items()}
        for i, name in enumerate(metric_names):
            d = lowered.get(name.lower(), {})
            en = tk.BooleanVar(value=bool(d.get("enabled", False)))
            mn = tk.StringVar(value=self._txt(d.get("min", "")))
            mx = tk.StringVar(value=self._txt(d.get("max", "")))
            if name in self.saved:
                e0, lo0, hi0 = self.saved[name]
                en.set(bool(e0))
                mn.set(lo0)
                mx.set(hi0)
            row = ttk.Frame(self._inner)
            row.pack(fill="x", pady=1)
            ttk.Checkbutton(row, variable=en, width=3).pack(side="left", padx=(6, 2))
            ttk.Label(row, text=name, width=30).pack(side="left", padx=3)
            ttk.Entry(row, textvariable=mn, width=11).pack(side="left", padx=3)
            ttk.Entry(row, textvariable=mx, width=11).pack(side="left", padx=3)
            if i == 0 or name.lower() == str(default_sort).lower():
                self._sort_var.set(name)
            self._rows.append(dict(col=name, en=en, min=mn, max=mx))
        if self.saved_sort in metric_names:
            self._sort_var.set(self.saved_sort)
        if not self._rows:
            ttk.Label(self._inner, text="No metrics loaded - load a file on the Data tab first."
                      ).pack(padx=8, pady=8)

    def restore(self, saved, saved_sort):
        if isinstance(saved, dict):
            for name, v in saved.items():
                if isinstance(v, list) and len(v) == 3:
                    self.saved[str(name)] = [bool(v[0]), str(v[1]), str(v[2])]
        if isinstance(saved_sort, str):
            self.saved_sort = saved_sort

    def remember(self):
        if not self._rows:
            return
        for r in self._rows:
            self.saved[r["col"]] = [r["en"].get(), r["min"].get(), r["max"].get()]
        self.saved_sort = self._sort_var.get()

    def get_filters(self):
        out = []
        for r in self._rows:
            enabled = r["en"].get()
            b = {}
            for k in ("min", "max"):
                s = r[k].get().strip()
                if not s:
                    b[k] = None
                    continue
                try:
                    b[k] = float(s)
                except ValueError:
                    if enabled:
                        raise ValueError(f"Metric '{r['col']}': {k.capitalize()} '{s}' is not a number.")
                    b[k] = None
            out.append(dict(col=r["col"], enabled=enabled, min=b["min"], max=b["max"]))
        return out, (self._sort_var.get() or None)

    def set_all(self, flag):
        for r in self._rows:
            r["en"].set(flag)

    def clear_ranges(self):
        for r in self._rows:
            r["min"].set("")
            r["max"].set("")


class App(tk.Tk):

    def __init__(self, load_config, app_version=""):
        cfg = load_config()
        super().__init__()
        self.cfg = cfg
        self._state = core.load_state()
        gui, self.opt, self.inp, self.exp = cfg["gui"], cfg["analysis"], cfg["input"], cfg["export"]
        self.filt = cfg["filters"]
        self.title(f'{gui["title"]} v{app_version}' if app_version else gui["title"])
        self.geometry(gui["window_geometry"])
        self.minsize(int(gui["min_width"]), int(gui["min_height"]))
        self._poll_ms = int(gui["poll_ms"])
        self._preview_rows = int(gui["preview_rows"])
        self._wrap = int(gui["summary_wrap"])

        self.df = self.pivot = self.analysis = self.result = None
        self.non_numeric = 0
        self.running = self.loading = False
        self._t0 = 0.0
        self._auto_folder = self._auto_stem = None
        self._bgq, self._logq = queue.Queue(), queue.Queue()
        self._dist = {}

        last_file = self._state.get("input_file", "")
        self.path = tk.StringVar(value=last_file if os.path.isfile(last_file) else "")
        self.load_status = tk.StringVar(value="No data loaded.")
        top_n = str(self._state.get("top_n", ""))
        order = self._state.get("sort_order")
        self.top_n = tk.StringVar(
            value=top_n if top_n.isdigit() and int(top_n) >= 1 else str(self.opt["default_top_n"]))
        self.sort_order = tk.StringVar(
            value=order if order in (ORDER_ASC, ORDER_DESC)
            else ORDER_ASC if self.opt["sort_ascending"] else ORDER_DESC)
        self.out_folder = tk.StringVar(
            value=self.exp["folder"] or self._saved_dir("output_folder", ""))
        self.stem = tk.StringVar(value=self.exp["name_stem"])
        self.w_reshaped = tk.BooleanVar(value=bool(self.exp["write_reshaped"]))
        self.w_summary = tk.BooleanVar(value=bool(self.exp["write_summary"]))
        self.status = tk.StringVar(value="Ready.")
        self.summary_line = tk.StringVar(value="No data loaded.")
        self.dist_metric = tk.StringVar()
        self.dist_bin = tk.StringVar(value=f"{self.opt['distribution_bin_width']:g}")
        self.matrix_key = tk.StringVar(value="Pass %")

        self._build_log()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        tabs = []
        for title in ("1. Data", "2. Filters & Analysis", "3. Results"):
            frame = ttk.Frame(self.nb)
            self.nb.add(frame, text=title)
            tabs.append(frame)
        self.tabs = tabs
        self._build_data(tabs[0])
        self._build_work(tabs[1])
        self._build_results(tabs[2])
        self.ftable.restore(self._state.get("filters"), self._state.get("sort_col"))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(self._poll_ms, self._tick)

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
        while True:
            try:
                self._log(self._logq.get_nowait())
            except queue.Empty:
                break
        self.after(self._poll_ms, self._tick)

    def _qlog(self, msg):
        self._logq.put(msg)

    def _build_log(self):
        lf = ttk.LabelFrame(self, text="Log")
        lf.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.log_box = scrolledtext.ScrolledText(lf, state="disabled", height=7,
                                                 font=("Courier", 9), wrap="word")
        self.log_box.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        ttk.Button(lf, text="Clear log", command=self._clear_log).pack(side="right", padx=4, anchor="n", pady=4)

    def _log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _saved_dir(self, key, fallback):
        path = self._state.get(key, "")
        return path if path and os.path.isdir(path) else fallback

    def _remember(self, **values):
        self._state.update(values)
        core.save_state(**values)

    def _save_filters(self):
        self.ftable.remember()
        self._remember(filters=self.ftable.saved, sort_col=self.ftable.saved_sort,
                       top_n=self.top_n.get().strip(), sort_order=self.sort_order.get())

    def _on_close(self):
        self._save_filters()
        self.destroy()

    def _ascending(self):
        return self.sort_order.get() != ORDER_DESC

    def _read_filters(self, title="Invalid filter value"):
        try:
            return self.ftable.get_filters()
        except ValueError as exc:
            messagebox.showerror(title, str(exc))
            return None

    def _build_data(self, tab):
        src = ttk.LabelFrame(tab, text="Source (xlsx or csv exported by the OCR app)")
        src.pack(fill="x", padx=8, pady=6)
        top = ttk.Frame(src)
        top.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Entry(top, textvariable=self.path, width=70).pack(side="left")
        ttk.Button(top, text="Browse...", command=self._browse).pack(side="left", padx=6)
        self.load_btn = ttk.Button(top, text="Load", command=self.load)
        self.load_btn.pack(side="left", padx=4)
        self.load_bar = ttk.Progressbar(top, mode="indeterminate", length=140)
        self.load_bar.pack(side="left", padx=8)
        ttk.Label(src, textvariable=self.load_status, font=("TkDefaultFont", 10, "bold")
                  ).pack(anchor="w", padx=8, pady=(0, 6))

        mid = ttk.Frame(tab)
        mid.pack(fill="both", expand=True, padx=8)
        dn = ttk.Notebook(mid)
        dn.pack(side="left", fill="both", expand=True)
        f = ttk.Frame(dn)
        dn.add(f, text="Overview")
        fr, self.ov_tree = make_table(f, show="tree headings")
        self.ov_tree["columns"] = ("value",)
        self.ov_tree.heading("#0", text="Property")
        self.ov_tree.heading("value", text="Value")
        self.ov_tree.column("#0", width=230, anchor="w")
        self.ov_tree.column("value", width=330, anchor="w")
        fr.pack(fill="both", expand=True, padx=4, pady=4)
        f = ttk.Frame(dn)
        dn.add(f, text="Data preview")
        fr, self.t_preview = make_table(f)
        fr.pack(fill="both", expand=True, padx=4, pady=4)

    def _browse(self):
        p = filedialog.askopenfilename(
            title="OCR results",
            initialdir=self._saved_dir("input_folder", None) or os.path.dirname(self.path.get()) or None,
            filetypes=[("OCR results", "*.xlsx *.csv"), ("All files", "*.*")])
        if p:
            self.path.set(p)
            self.load()

    def _browse_out(self):
        d = filedialog.askdirectory(initialdir=self.out_folder.get() or None)
        if d:
            self.out_folder.set(d)
            self._remember(output_folder=d)

    def load(self):
        path = self.path.get().strip()
        if not os.path.isfile(path):
            messagebox.showerror("Load", "Choose an existing xlsx or csv file.")
            return
        if self.loading:
            return
        self.loading = True
        self._t0 = time.time()
        self.load_btn.config(state="disabled")
        self.load_bar.start(12)
        self.load_status.set("Loading...")
        self.bg(lambda: ac.load_table(path, self.inp["sheet_name"], self.inp["filename_aliases"]),
                lambda r, e: self._loaded(path, r, e))

    def _loaded(self, path, res, exc):
        self.loading = False
        self.load_bar.stop()
        self.load_btn.config(state="normal")
        if exc:
            self.load_status.set(f"Load failed: {exc}")
            self._log(f"Load failed: {exc}")
            messagebox.showerror("Load", str(exc))
            return
        self.df, self.non_numeric = res
        self._remember(input_file=path, input_folder=os.path.dirname(path))
        self.pivot = self.result = self.analysis = None
        ncols = len(self.df.columns) - 1
        dt = time.time() - self._t0
        self.load_status.set(f"Loaded {len(self.df):,} rows x {ncols} columns in {dt:.1f} s"
                             f"   |   {self.non_numeric:,} unreadable cells")
        self._log(f"Loaded {path}: {len(self.df):,} rows, {ncols} columns, {dt:.1f} s")

        t = self.ov_tree
        t.delete(*t.get_children())
        secs = {}
        rows = ac.dataset_overview(self.df) + [
            ("Quality", "Cells that were not numeric", f"{self.non_numeric:,}")]
        for sec, k, v in rows:
            if sec not in secs:
                secs[sec] = t.insert("", "end", text=sec, open=True)
            t.insert(secs[sec], "end", text=k, values=(v,))
        head = self.df.head(self._preview_rows)
        fill_table(self.t_preview, list(head.columns),
                   head.itertuples(index=False, name=None), width=110, first_width=180)

        folder = self.exp["folder"] or os.path.dirname(path)
        stem = self.exp["name_stem"] or os.path.splitext(os.path.basename(path))[0]
        if not self.out_folder.get().strip() or self.out_folder.get() == self._auto_folder:
            self.out_folder.set(folder)
        if not self.stem.get().strip() or self.stem.get() == self._auto_stem:
            self.stem.set(stem)
        self._auto_folder, self._auto_stem = folder, stem

        self._populate_filters()
        self.refresh_analysis()

    def _build_work(self, tab):
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=6)

        of = ttk.LabelFrame(top, text="Output")
        of.pack(side="left", fill="y", padx=(0, 6))
        ttk.Label(of, text="Folder").grid(row=0, column=0, sticky="e", padx=6, pady=3)
        ttk.Entry(of, textvariable=self.out_folder, width=36).grid(row=0, column=1, padx=6)
        ttk.Button(of, text="Browse...", command=self._browse_out).grid(row=0, column=2, padx=(0, 6))
        ttk.Label(of, text="Name stem").grid(row=1, column=0, sticky="e", padx=6, pady=3)
        ttk.Entry(of, textvariable=self.stem, width=20).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Checkbutton(of, variable=self.w_reshaped, text="Write <stem>_reshaped.xlsx"
                        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=6)
        ttk.Checkbutton(of, variable=self.w_summary, text="Write <stem>_summary.xlsx"
                        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 4))

        rn = ttk.LabelFrame(top, text="Run")
        rn.pack(side="left", fill="y", padx=6)
        self.run_btn = ttk.Button(rn, text="Run", command=self.run)
        self.run_btn.pack(padx=10, pady=(8, 4))
        ttk.Button(rn, text="Refresh analysis", command=self.refresh_analysis).pack(padx=10, pady=4)
        ttk.Label(rn, textvariable=self.status).pack(padx=10, pady=4)

        mid = ttk.PanedWindow(tab, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(mid)
        right = ttk.Frame(mid)
        mid.add(left, weight=0)
        mid.add(right, weight=1)

        self.ftable = MetricFilterTable(left)
        rk = ttk.Frame(left)
        rk.pack(fill="x", pady=(0, 4))
        ttk.Label(rk, text="Top-N per combo").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(rk, textvariable=self.top_n, width=5).grid(row=0, column=1, sticky="w")
        ttk.Label(rk, text="Order").grid(row=0, column=2, sticky="e", padx=(12, 4))
        ttk.Combobox(rk, textvariable=self.sort_order, state="readonly", width=13,
                     values=(ORDER_ASC, ORDER_DESC)).grid(row=0, column=3, sticky="w")
        ttk.Label(rk, text="Sort by column").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        self.sort_cb = ttk.Combobox(rk, textvariable=self.ftable.sort_var, state="readonly", width=28)
        self.sort_cb.grid(row=1, column=1, columnspan=3, sticky="w")
        tb = ttk.Frame(left)
        tb.pack(fill="x", pady=(0, 4))
        for text, cmd in (("Enable all", lambda: self.ftable.set_all(True)),
                          ("Disable all", lambda: self.ftable.set_all(False)),
                          ("Clear ranges", self.ftable.clear_ranges)):
            ttk.Button(tb, text=text, command=cmd).pack(side="left", padx=(0, 4))
        self.ftable.pack(fill="both", expand=True)
        self.ftable.populate([], {}, "")
        self._build_analysis(right)

    def _populate_filters(self):
        self.ftable.remember()
        names = ac.extract_metric_names(self.df.columns)
        self.ftable.populate(names, self.filt, self.opt["sort_metric"])
        self.sort_cb["values"] = names

    def _build_analysis(self, tab):
        ttk.Label(tab, textvariable=self.summary_line, wraplength=self._wrap, justify="left"
                  ).pack(fill="x", padx=4, pady=(0, 4))
        anb = ttk.Notebook(tab)
        anb.pack(fill="both", expand=True)

        f = ttk.Frame(anb)
        anb.add(f, text="Metric stats")
        fr, self.t_stats = make_table(f)
        fr.pack(fill="both", expand=True)

        f = ttk.Frame(anb)
        anb.add(f, text="Matrix")
        mb = ttk.Frame(f)
        mb.pack(fill="x", pady=4)
        ttk.Label(mb, text="Show:").pack(side="left", padx=4)
        cb = ttk.Combobox(mb, textvariable=self.matrix_key, state="readonly", width=20,
                          values=list(MATRIX_KEYS))
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._render_matrix())
        fr, self.t_matrix = make_table(f)
        fr.pack(fill="both", expand=True)

        f = ttk.Frame(anb)
        anb.add(f, text="Distribution")
        db = ttk.Frame(f)
        db.pack(fill="x", pady=4)
        ttk.Label(db, text="Metric:").pack(side="left", padx=4)
        self.dist_combo = ttk.Combobox(db, textvariable=self.dist_metric, state="readonly", width=28)
        self.dist_combo.pack(side="left", padx=(0, 10))
        self.dist_combo.bind("<<ComboboxSelected>>", lambda e: self._render_dist())
        ttk.Label(db, text="Bin width:").pack(side="left")
        ttk.Entry(db, textvariable=self.dist_bin, width=7).pack(side="left", padx=(4, 10))
        ttk.Button(db, text="Refresh", command=self._render_dist).pack(side="left")
        ttk.Label(db, text="Click a row on the left to see its histogram"
                  ).pack(side="right", padx=8)
        pw = ttk.PanedWindow(f, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left, right = ttk.Frame(pw), ttk.Frame(pw)
        pw.add(left, weight=1)
        pw.add(right, weight=1)
        fr, self.t_dist = make_table(left)
        fr.pack(fill="both", expand=True)
        self.t_dist.bind("<<TreeviewSelect>>", lambda e: self._show_bins())
        fr, self.t_bins = make_table(right)
        fr.pack(fill="both", expand=True)

    def refresh_analysis(self):
        if self.df is None or not self.ftable.rows:
            return
        read = self._read_filters()
        if read is None:
            return
        filters, sort_col = read
        self._save_filters()
        asc, df, cached = self._ascending(), self.df, self.pivot

        def work():
            pivot = cached if cached is not None else ac.reshape_data(df)
            return pivot, ac.analyze(pivot, filters, sort_col, asc)
        self.status.set("Analyzing...")
        self.bg(work, self._analysis_done)

    def _analysis_done(self, res, exc):
        if exc:
            self.status.set("Analysis failed")
            messagebox.showerror("Analysis error", str(exc))
            return
        self.pivot, a = res
        self.analysis = a
        self.status.set("Ready.")
        st = a["metric_stats"]
        fill_table(self.t_stats,
                   ("Metric", "Valid", "Blank %", "Spec", "In spec %", "Min", "P5", "Median",
                    "P95", "Max", "Mean", "Std", "Margin (median)", "Margin (worst)"),
                   [(s["metric"], s["valid"], pct(s["blank_pct"]), s["spec"] or "-",
                     pct(s["in_spec"]), s["min"], s["p05"], s["median"], s["p95"], s["max"],
                     s["mean"], s["std"], s["margin_med"], s["margin_worst"]) for s in st],
                   width=80, first_width=230, nd=2,
                   widths={"Spec": 110, "Margin (median)": 110, "Margin (worst)": 110},
                   tags=[tone(s["in_spec"]) for s in st])
        self.summary_line.set(ac.headline(a))
        self.dist_combo.configure(values=a["metrics"])
        if self.dist_metric.get() not in a["metrics"]:
            pref = [m for m in a["metrics"] if m.lower() == str(self.opt["sort_metric"]).lower()]
            self.dist_metric.set((pref or a["metrics"] or [""])[0])
        self._render_matrix()
        self._render_dist()

    def _render_matrix(self):
        if not self.analysis:
            return
        key = MATRIX_KEYS[self.matrix_key.get()]
        chs, pws, cells = ac.matrix_from_report(self.analysis["combos"], key)

        def show(v):
            if v is None:
                return "-"
            if key == "pass_pct":
                return f"{v:.0f}%"
            if key == "best":
                return f"{v:.2f}"
            return f"{v:,}"
        fill_table(self.t_matrix, ["ch \\ pwr"] + [f"pwr{p}" for p in pws],
                   [[f"ch{c}"] + [show(cells[(c, p)]) if (c, p) in cells else "" for p in pws]
                    for c in chs], width=70, first_width=80)

    def _render_dist(self):
        for t in (self.t_dist, self.t_bins):
            t.delete(*t.get_children())
        self._dist = {}
        if self.pivot is None or not self.dist_metric.get():
            return
        read = self._read_filters("Distribution error")
        if read is None:
            return
        try:
            combos = ac.build_distribution(self.pivot, self.dist_metric.get(),
                                           float(self.dist_bin.get().strip()), read[0])
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Distribution error", str(exc))
            return
        self._dist = {c["name"]: c for c in combos}

        def pass_pct(c):
            return 100.0 * c["n_pass"] / c["total"] if c["total"] else 0.0

        fill_table(self.t_dist, ("Combo", "Valid", "Pass %", "Min", "Median", "Max"),
                   [(c["name"], c["valid"], f"{pass_pct(c):.0f}%", c["vmin"], c["vmedian"], c["vmax"])
                    for c in combos],
                   width=75, first_width=110, nd=2, tags=[tone(pass_pct(c)) for c in combos])
        first = self.t_dist.get_children()
        if first:
            self.t_dist.selection_set(first[0])
        self._show_bins()

    def _show_bins(self):
        sel = self.t_dist.selection()
        if not sel or not self._dist:
            return
        c = self._dist.get(str(self.t_dist.item(sel[0])["values"][0]))
        if c is None:
            return
        rows = [(f"{s:g} to {e:g}", n, f"{pct:.1f}%", "\u2588" * int(round(pct / 3)))
                for s, e, n, pct in c["bins"]]
        fill_table(self.t_bins, ("Range", "Count", "% of valid", "Distribution"), rows,
                   width=90, first_width=130, widths={"Distribution": 300})

    def _build_results(self, tab):
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Open output folder", command=self._open_out).pack(side="left")
        sf = ttk.LabelFrame(tab, text="Summary (Top-N rows per combination)")
        sf.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        fr, self.t_summary = make_table(sf)
        fr.pack(fill="both", expand=True, padx=4, pady=4)

    def _open_out(self):
        p = self.out_folder.get()
        if not os.path.isdir(p):
            return
        if os.name == "nt":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])

    def run(self):
        if self.running:
            return
        if self.df is None:
            messagebox.showerror("Run", "Load a file first.")
            return
        read = self._read_filters("Invalid value")
        if read is None:
            return
        filters, sort_col = read
        try:
            top_n = int(self.top_n.get().strip())
            if top_n < 1:
                raise ValueError("Top-N must be at least 1.")
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc))
            return
        w_resh, w_summ = self.w_reshaped.get(), self.w_summary.get()
        folder, stem = self.out_folder.get().strip(), self.stem.get().strip() or "OCR_Results"
        if (w_resh or w_summ) and not folder:
            messagebox.showerror("Output", "Set an output folder or untick the files.")
            return
        self._save_filters()
        if w_resh or w_summ:
            self._remember(output_folder=folder)
        asc, df, pivot = self._ascending(), self.df, self.pivot
        params = dict(source=self.path.get().strip(), filters=filters, sort_col=sort_col,
                      top_n=top_n, ascending=asc)

        def work():
            res = ac.run_pipeline(df, filters, sort_col, top_n, asc, log=self._qlog, pivot=pivot)
            res["paths"] = export_core.export_results(res, params, folder, stem,
                                                      w_resh, w_summ, self._qlog)
            return res
        self.running = True
        self.run_btn.config(state="disabled")
        self.status.set("Running...")
        self._log("--- Run started ---")
        self.bg(work, lambda r, e: self._run_done(r, e, params))

    def _run_done(self, res, exc, params):
        self.running = False
        self.run_btn.config(state="normal")
        if exc:
            self.status.set("Error - see log")
            self._log(f"ERROR: {exc}")
            messagebox.showerror("Pipeline error", str(exc))
            return
        self.status.set("Done")
        self.result, self.pivot = res, res["pivot"]
        sdf = res["summary_df"]
        if sdf.empty:
            fill_table(self.t_summary, ("(no rows pass the filters)",), [])
        else:
            fill_table(self.t_summary, list(sdf.columns),
                       sdf.itertuples(index=False, name=None), width=95)
        failing = [n for n in res["sheet_names"]
                   if sdf.empty or n not in set(sdf["source_sheet"])]
        self._log(f"Combinations with no passing row ({len(failing)}): "
                  + (", ".join(failing[:25]) + (" ..." if len(failing) > 25 else "")
                     if failing else "none"))
        self._log(f"--- Finished {res['finished']} ---")
        self.refresh_analysis()
        self.nb.select(self.tabs[2])
        messagebox.showinfo("Run complete",
                            f"Combinations passing: {res['n_pass']}/{res['n_sheets']}\n"
                            f"Summary rows: {len(sdf)}")
