import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.eval_core import evaluate, find_pairs, list_models, percent


class App(tk.Tk):
    def __init__(self, cfg, app_version=""):
        super().__init__()
        self.cfg = cfg
        gui, eng = cfg["gui"], cfg["engine"]
        self.title(f'{gui["title"]} v{app_version}' if app_version else gui["title"])
        self.geometry(gui["window_geometry"])
        self.minsize(int(gui["min_width"]), int(gui["min_height"]))
        self._poll_ms = int(gui["poll_ms"])
        self._bgq, self._progq = queue.Queue(), queue.Queue()
        self._stop, self._results = None, {}

        # ── input ─────────────────────────────────────────────────────────
        inp = ttk.LabelFrame(self, text="Input")
        inp.pack(fill="x", padx=8, pady=6)
        inp.columnconfigure(1, weight=1)
        self.folder_var = tk.StringVar()
        self.tess_var = tk.StringVar(value=os.path.normpath(eng["tessdata_dir"]))
        self.lang_var = tk.StringVar(value=str(eng["languages"]))
        ttk.Label(inp, text="Eval folder").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        entry = ttk.Entry(inp, textvariable=self.folder_var)
        entry.grid(row=0, column=1, sticky="ew", padx=4)
        entry.bind("<FocusOut>", lambda e: self._check_inputs())
        entry.bind("<Return>", lambda e: self._check_inputs())
        ttk.Button(inp, text="Browse...", command=self._browse_folder).grid(row=0, column=2, padx=4)
        self.pairs_lbl = ttk.Label(inp, text="")
        self.pairs_lbl.grid(row=1, column=1, sticky="w", padx=4)

        ttk.Label(inp, text="Models folder").grid(row=2, column=0, sticky="w", padx=6, pady=3)
        entry = ttk.Entry(inp, textvariable=self.tess_var)
        entry.grid(row=2, column=1, sticky="ew", padx=4)
        entry.bind("<FocusOut>", lambda e: self._check_inputs())
        entry.bind("<Return>", lambda e: self._check_inputs())
        ttk.Button(inp, text="Browse...", command=self._browse_tess).grid(row=2, column=2, padx=4)
        self.models_lbl = ttk.Label(inp, text="")
        self.models_lbl.grid(row=3, column=1, sticky="w", padx=4)
        ttk.Label(inp, text="Models to test").grid(row=4, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(inp, textvariable=self.lang_var).grid(row=4, column=1, sticky="ew", padx=4)
        ttk.Label(inp, foreground="gray", text="comma separated, e.g. rf,custom").grid(
            row=4, column=2, padx=4)

        # ── settings ──────────────────────────────────────────────────────
        sett = ttk.LabelFrame(self, text="Settings")
        sett.pack(fill="x", padx=8, pady=6)
        self.psm_var = tk.StringVar(value=str(eng["psm"]))
        self.wl_var = tk.StringVar(value=str(eng["whitelist"]))
        ttk.Label(sett, text="PSM").pack(side="left", padx=(8, 2), pady=6)
        ttk.Entry(sett, textvariable=self.psm_var, width=5).pack(side="left")
        ttk.Label(sett, text="Whitelist (empty = none)").pack(side="left", padx=(16, 2))
        ttk.Entry(sett, textvariable=self.wl_var, width=24).pack(side="left")

        # ── run ───────────────────────────────────────────────────────────
        run = ttk.Frame(self)
        run.pack(fill="x", padx=8, pady=4)
        self.b_run = ttk.Button(run, text="Run", command=self.run)
        self.b_run.pack(side="left")
        self.b_stop = ttk.Button(run, text="Stop", command=self.stop, state="disabled")
        self.b_stop.pack(side="left", padx=6)
        self.bar = ttk.Progressbar(run, length=300)
        self.bar.pack(side="left", padx=10)
        self.status = ttk.Label(run, text="")
        self.status.pack(side="left")

        # ── results ───────────────────────────────────────────────────────
        res = ttk.LabelFrame(self, text="Results")
        res.pack(fill="x", padx=8, pady=6)
        self.rtree = ttk.Treeview(res, columns=("images", "exact", "value", "cer"), height=4,
                                  selectmode="browse")
        for col, text, width in (("#0", "Model", 160), ("images", "Images", 80),
                                 ("exact", "Exact match %", 120), ("value", "Value match %", 120),
                                 ("cer", "CER %", 90)):
            self.rtree.heading(col, text=text)
            self.rtree.column(col, width=width, anchor="w" if col == "#0" else "e")
        self.rtree.pack(fill="x", padx=6, pady=6)
        self.rtree.bind("<<TreeviewSelect>>", lambda e: self._show_errors())
        self.conf_lbl = ttk.Label(res, text="", justify="left", wraplength=900)
        self.conf_lbl.pack(anchor="w", padx=8, pady=(0, 6))

        errs = ttk.LabelFrame(self, text="Wrong readings of the selected model")
        errs.pack(fill="both", expand=True, padx=8, pady=6)
        frame = ttk.Frame(errs)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.etree = ttk.Treeview(frame, columns=("truth", "ocr"), selectmode="browse")
        for col, text, width in (("#0", "File", 380), ("truth", "Ground truth", 220),
                                 ("ocr", "Model reading", 220)):
            self.etree.heading(col, text=text)
            self.etree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.etree.yview)
        self.etree.configure(yscrollcommand=scroll.set)
        self.etree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._check_inputs()
        self.after(self._poll_ms, self._tick)

    # ── helpers ───────────────────────────────────────────────────────────
    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder_var.get() or None,
                                         title="Eval folder (png + gt.txt pairs)")
        if folder:
            self.folder_var.set(os.path.normpath(folder))
            self._check_inputs()

    def _browse_tess(self):
        folder = filedialog.askdirectory(initialdir=self.tess_var.get() or None,
                                         title="Folder with .traineddata models")
        if folder:
            self.tess_var.set(os.path.normpath(folder))
            self._check_inputs()

    def _check_inputs(self):
        folder = self.folder_var.get().strip()
        if os.path.isdir(folder):
            self.pairs_lbl.config(text=f"{len(find_pairs(folder)):,} image + ground-truth pair(s) found")
        else:
            self.pairs_lbl.config(text="Choose the eval folder")
        models = list_models(self.tess_var.get().strip())
        self.models_lbl.config(text="Available: " + (", ".join(models) if models else "none found"))

    def _on_close(self):
        if self._stop:
            self._stop.set()
        self.destroy()

    def _tick(self):
        while True:
            try:
                cb, res, exc = self._bgq.get_nowait()
            except queue.Empty:
                break
            cb(res, exc)
        last = None
        while True:
            try:
                last = self._progq.get_nowait()
            except queue.Empty:
                break
        if last:
            self.bar.config(maximum=last[1], value=last[0])
            self.status.config(text=f"{last[2]}: {last[0]:,} / {last[1]:,}")
        self.after(self._poll_ms, self._tick)

    # ── run ───────────────────────────────────────────────────────────────
    def run(self):
        folder, tess = self.folder_var.get().strip(), self.tess_var.get().strip()
        pairs = find_pairs(folder) if os.path.isdir(folder) else []
        if not pairs:
            messagebox.showerror("Run", "No name.png + name.gt.txt pairs found in the eval folder.")
            return
        langs = [l.strip() for l in self.lang_var.get().split(",") if l.strip()]
        if not langs:
            messagebox.showerror("Run", "Enter at least one model name.")
            return
        missing = [l for l in langs if not os.path.isfile(os.path.join(tess, l + ".traineddata"))]
        if missing:
            messagebox.showerror("Run", f"No .traineddata in the models folder for: {', '.join(missing)}")
            return
        try:
            psm = int(self.psm_var.get().strip())
        except ValueError:
            messagebox.showerror("Run", "PSM must be a whole number.")
            return
        whitelist, cfg = self.wl_var.get().strip(), self.cfg
        self._stop = stop = threading.Event()
        self._results = {}
        self.rtree.delete(*self.rtree.get_children())
        self.etree.delete(*self.etree.get_children())
        self.conf_lbl.config(text="")
        self.b_run.config(state="disabled")
        self.b_stop.config(state="normal")
        total = len(pairs) * len(langs)

        def work():
            out = []
            for k, lang in enumerate(langs):
                base = k * len(pairs)
                out.append(evaluate(
                    pairs, lang, tess, psm, whitelist, cfg, stop=stop,
                    progress=lambda d, base=base, lang=lang: self._progq.put((base + d, total, lang))))
                if stop.is_set():
                    break
            return out

        def finish(res, exc):
            self.b_run.config(state="normal")
            self.b_stop.config(state="disabled")
            self._stop = None
            if exc:
                self.status.config(text="Failed")
                messagebox.showerror("Run failed", f"{type(exc).__name__}: {exc}")
                return
            self.status.config(text="Stopped" if any(r["stopped"] for r in res) else "Done")
            self._fill_results(res)

        def runner():
            try:
                outcome = (work(), None)
            except Exception as exc:
                outcome = (None, exc)
            self._bgq.put((finish,) + outcome)
        threading.Thread(target=runner, daemon=True).start()

    def stop(self):
        if self._stop:
            self._stop.set()
            self.status.config(text="Stopping...")

    # ── results ───────────────────────────────────────────────────────────
    def _fill_results(self, results):
        for r in results:
            self._results[r["lang"]] = r
            self.rtree.insert("", "end", iid=r["lang"], text=r["lang"], values=(
                f"{r['n']:,}", f"{percent(r['exact'], r['n']):.2f}",
                f"{percent(r['value_ok'], r['value_n']):.2f}",
                f"{percent(r['edits'], r['ref_chars']):.2f}"))
        if results:
            self.rtree.selection_set(results[0]["lang"])

    def _show_errors(self):
        self.etree.delete(*self.etree.get_children())
        selection = self.rtree.selection()
        r = self._results.get(selection[0]) if selection else None
        if not r:
            return
        top = ", ".join(f"{m} x{n}" for m, n in r["confusions"].most_common(int(self.cfg["gui"]["top_confusions"])))
        note = f"   ({r['unreadable']} unreadable image(s) skipped)" if r["unreadable"] else ""
        self.conf_lbl.config(text=(f"Most frequent mistakes (truth->reading): {top}" if top
                                   else "No mistakes.") + note)
        limit = int(self.cfg["gui"]["max_errors_shown"])
        for name, truth, reading in r["errors"][:limit]:
            self.etree.insert("", "end", text=name, values=(truth, reading or "(empty)"))
        if len(r["errors"]) > limit:
            self.etree.insert("", "end", text=f"... {len(r['errors']) - limit:,} more not shown")
