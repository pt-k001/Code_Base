"""Tkinter dataset maker: 1 Project (Create / Open), 2 Label, 3 Export.

Labels PNG crops from a capture database for Tesseract training (tesstrain style:
``name.png`` + ``name.gt.txt``).  Everything you do is saved at once to a project
database inside the dataset folder; the png / gt.txt files are only written when
you press Export.

Usage:
    python main.py
    Settings: Service/Service_configs/trainer/trainer_config.toml
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from contextlib import closing
from tkinter import filedialog, messagebox, ttk

from core import core, dataset_core, ocr_core


class App(tk.Tk):

    def __init__(self, cfg, app_version=""):
        super().__init__()
        self.cfg = cfg
        title = self._d("title")
        self.title(f"{title} v{app_version}" if app_version else title)
        self.geometry(self._d("window_geometry"))
        self.minsize(int(self._d("min_width")), int(self._d("min_height")))
        self._poll_ms = int(self._d("poll_ms"))
        self._img_size = (int(self._d("image_max_width")), int(self._d("image_max_height")))
        self._font = ("TkDefaultFont", int(self._d("text_font_size")))
        self._font_bold = self._font + ("bold",)

        self.project = self.prefetcher = None
        self.cur_id, self._item = None, None
        self._waiting = None                    # item id the screen is waiting for
        self._can_label = self._can_skip = False
        self._img = None
        self._bgq = queue.Queue()

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        tabs = []
        for title in ("1. Project", "2. Label", "3. Export"):
            frame = ttk.Frame(self.nb)
            self.nb.add(frame, text=title)
            tabs.append(frame)
        self.nb.tab(1, state="disabled")
        self.nb.tab(2, state="disabled")
        self._build_project(tabs[0])
        self._build_label(tabs[1])
        self._build_export(tabs[2])

        self.bind("<Alt-Left>", lambda e: self._shortcut(self.go_prev))
        self.bind("<Alt-Right>", lambda e: self._shortcut(self.skip))
        self.bind("<Control-Return>", lambda e: self._shortcut(self.accept_next))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(self._poll_ms, self._tick)

    # ── helpers ───────────────────────────────────────────────────────────
    def _d(self, key):
        return core.require(self.cfg, "dataset", key)

    @staticmethod
    def _project_dir():
        return os.path.normpath(core.resolve_path("."))

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
        if self.prefetcher:
            while True:
                try:
                    item_id, err = self.prefetcher.done.get_nowait()
                except queue.Empty:
                    break
                if item_id == self._waiting:
                    self._engine_error(err) if err else self._show_item(item_id)
        self.after(self._poll_ms, self._tick)

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
        self._close_project()
        self.destroy()

    def _close_project(self):
        if self.prefetcher:
            self.prefetcher.stop(float(self._d("shutdown_wait_s")))
            self.prefetcher = None
        if self.project:
            self.project.close()
            self.project = None
        self.cur_id, self._item, self._waiting = None, None, None

    # ══ TAB 1: PROJECT ═══════════════════════════════════════════════════
    def _build_project(self, tab):
        center = ttk.Frame(tab)
        center.pack(expand=True)
        ttk.Label(center, text="Dataset project", font=("TkDefaultFont", 14, "bold")).pack(pady=(0, 14))
        row = ttk.Frame(center)
        row.pack()
        ttk.Button(row, text="Create", width=18, command=lambda: CreateDialog(self)).pack(
            side="left", padx=10, ipady=10)
        ttk.Button(row, text="Open", width=18, command=lambda: OpenDialog(self)).pack(
            side="left", padx=10, ipady=10)
        self.proj_info = ttk.Label(center, text="No project open", justify="left")
        self.proj_info.pack(pady=(24, 0))

    def _opened(self, project, exc):
        if exc:
            messagebox.showerror("Project", str(exc))
            return
        self._close_project()
        self.project = project
        self.prefetcher = dataset_core.Prefetcher(project)
        p = project.params
        self.proj_info.config(text=(
            f"Project: {project.name}\n"
            f"Folder: {project.folder}\n"
            f"Source database: {project.source_db}\n"
            f"OCR: lang {p['lang']}, psm {p['psm']}, stage {p['stage']}, "
            f"whitelist {'on' if project.use_whitelist else 'off (raw text)'}"))
        self.exp_folder.set(os.path.join(project.folder, self._d("export_subdir")))
        self.nb.tab(1, state="normal")
        self.nb.tab(2, state="normal")
        self._refresh_export()
        self.nb.select(1)
        start = project.first_pending()
        if start is None:
            messagebox.showinfo("Project", "Every image of this project is already labelled or "
                                           "skipped. Use Previous to review, or the Export tab.")
        self._show_item(start or 1)

    # ══ TAB 2: LABEL ═════════════════════════════════════════════════════
    def _build_label(self, tab):
        self.head_lbl = ttk.Label(tab, text="", font=("TkDefaultFont", 10, "bold"))
        self.head_lbl.pack(anchor="w", padx=10, pady=(8, 2))
        iw, ih = self._img_size
        box = ttk.Frame(tab, width=iw + 20, height=ih + 20, relief="sunken", borderwidth=1)
        box.pack(padx=10, pady=4)
        box.pack_propagate(False)
        self.img_lbl = ttk.Label(box, text="No image", anchor="center", justify="center",
                                 wraplength=iw)
        self.img_lbl.pack(expand=True)

        ocr = ttk.LabelFrame(tab, text="OCR value (raw)")
        ocr.pack(fill="x", padx=10, pady=6)
        self.ocr_lbl = ttk.Label(ocr, text="-", font=self._font_bold, anchor="w")
        self.ocr_lbl.pack(fill="x", padx=8, pady=(4, 0))
        self.ocr_info = ttk.Label(ocr, text="", justify="left")
        self.ocr_info.pack(anchor="w", padx=8, pady=(0, 4))

        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", padx=10, pady=4)
        self.b_prev = ttk.Button(buttons, text="Previous", command=self.go_prev)
        self.b_accept = ttk.Button(buttons, text="Accept and Next", command=self.accept_next)
        self.b_skip = ttk.Button(buttons, text="Skip", command=self.skip)
        for button in (self.b_prev, self.b_accept, self.b_skip):
            button.pack(side="left", expand=True, fill="x", padx=3)

        entry_row = ttk.LabelFrame(tab, text="Label (ground truth)")
        entry_row.pack(fill="x", padx=10, pady=6)
        self.text_var = tk.StringVar()
        self.entry = ttk.Entry(entry_row, textvariable=self.text_var, font=self._font)
        self.entry.pack(side="left", fill="x", expand=True, padx=(8, 4), pady=8)
        self.entry.bind("<Return>", lambda e: self.save_next())
        self.b_save = ttk.Button(entry_row, text="Save and next", command=self.save_next)
        self.b_save.pack(side="left", padx=(4, 8), pady=8)

        self.count_lbl = ttk.Label(tab, text="")
        self.count_lbl.pack(anchor="w", padx=10)
        ttk.Label(tab, foreground="gray", text=(
            "Accept and Next saves the OCR value as it is.  Save and next (Enter) saves the "
            "text in the label box.  Skip leaves the image out of the dataset.\n"
            "Shortcuts: Enter = Save and next, Ctrl+Enter = Accept and Next, "
            "Alt+Left = Previous, Alt+Right = Skip."), justify="left").pack(
                anchor="w", padx=10, pady=(2, 8))

    def _shortcut(self, action):
        if self.project and self.nb.index("current") == 1:
            action()

    def _set_buttons(self):
        self.b_prev.config(state="normal" if self.cur_id and self.cur_id > 1 else "disabled")
        self.b_accept.config(state="normal" if self._can_label else "disabled")
        self.b_save.config(state="normal" if self._can_label else "disabled")
        self.b_skip.config(state="normal" if self._can_skip else "disabled")
        self.entry.config(state="normal" if self._can_label else "disabled")

    def _refresh_counts(self):
        c = self.project.counts()
        self.count_lbl.config(text=(f"Accepted {c['accepted']:,}    Skipped {c['skipped']:,}    "
                                    f"Pending {c['pending']:,}    of {c['total']:,}"))
        return c

    def _show_item(self, item_id):
        project = self.project
        item = project.get_item(item_id) if project else None
        if item is None:
            return
        self.cur_id, self._item = item_id, item
        counts = self._refresh_counts()
        column = ocr_core.make_column_name(item["session"] or "", item["region"] or "",
                                           project.params["strip_prefix"])
        self.head_lbl.config(text=f"Image {item_id:,} / {counts['total']:,}   |   "
                                  f"{column}  #{item['seq']}   |   {item['status'].upper()}")
        self._refresh_export()
        for nid in project.unprocessed_after(item_id, int(self._d("prefetch"))):
            self.prefetcher.request(nid)

        if item["image"] is None and not item["error"]:
            self._waiting, self._can_label, self._can_skip = item_id, False, False
            self.img_lbl.config(image="", text="Processing...")
            self.ocr_lbl.config(text="-")
            self.ocr_info.config(text="")
            self.text_var.set("")
            self.prefetcher.request(item_id, urgent=True)
            self._set_buttons()
            return

        self._waiting = None
        if item["error"]:
            self._can_label, self._can_skip = False, True
            self.img_lbl.config(image="", text=f"Cannot use this image: {item['error']}\n"
                                               "Press Skip.")
            self.ocr_lbl.config(text="-")
            self.ocr_info.config(text="")
            self.text_var.set("")
            self._set_buttons()
            return

        self._can_label = self._can_skip = True
        try:
            b64 = dataset_core.png_to_display_b64(item["image"], *self._img_size)
            self._img = tk.PhotoImage(data=b64)
            self.img_lbl.config(image=self._img, text="")
        except Exception as exc:
            self.img_lbl.config(image="", text=f"Cannot display the image: {exc}")
        self.ocr_lbl.config(text=item["ocr_text"] or "(empty)")
        self.ocr_info.config(text=f"confidence {item['confidence'] or 0:.1f}"
                                  + ("   SUSPICIOUS" if item["suspicious"] else ""))
        prefill = item["label"] if item["status"] == dataset_core.ACCEPTED and item["label"] \
            else item["ocr_text"]
        self.text_var.set(prefill or "")
        self._set_buttons()
        self.entry.focus_set()
        self.entry.icursor("end")

    def _engine_error(self, err):
        self._waiting, self._can_label, self._can_skip = None, False, False
        self.img_lbl.config(image="", text=f"OCR could not process this image:\n{err}\n\n"
                                           "Fix the problem (tesserocr / tessdata / source "
                                           "database path), then reopen the project.")
        self._set_buttons()

    def _advance(self):
        nxt = self.project.next_pending(self.cur_id)
        if nxt is None:
            self._refresh_counts()
            self._refresh_export()
            messagebox.showinfo("Done", "No pending images left. Use the Export tab to write "
                                        "the dataset (or Previous to review).")
            self._show_item(self.cur_id)
            return
        self._show_item(nxt)

    def _commit(self, label, status):
        try:
            self.project.set_label(self.cur_id, label, status)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self._advance()

    def go_prev(self):
        if self.project and self.cur_id and self.cur_id > 1:
            self._show_item(self.cur_id - 1)

    def accept_next(self):
        if not self._can_label:
            return
        label = dataset_core.clean_label(self._item["ocr_text"])
        if not label:
            messagebox.showwarning("Empty label", "The OCR value is empty. Type the label and "
                                                  "press Save and next, or press Skip.")
            return
        self._commit(label, dataset_core.ACCEPTED)

    def save_next(self):
        if not self._can_label:
            return
        label = dataset_core.clean_label(self.text_var.get())
        if not label:
            messagebox.showwarning("Empty label", "The label box is empty. Type the label or "
                                                  "press Skip.")
            return
        self._commit(label, dataset_core.ACCEPTED)

    def skip(self):
        if self._can_skip:
            self._commit(None, dataset_core.SKIPPED)

    # ══ TAB 3: EXPORT ════════════════════════════════════════════════════
    def _build_export(self, tab):
        source = ttk.LabelFrame(tab, text="Labels")
        source.pack(fill="x", padx=8, pady=6)
        self.exp_info = ttk.Label(source, text="No project open.", justify="left")
        self.exp_info.pack(anchor="w", padx=8, pady=8)

        out = ttk.LabelFrame(tab, text="Output (tesstrain style: name.png + name.gt.txt)")
        out.pack(fill="x", padx=8, pady=6)
        self.exp_folder = tk.StringVar()
        self.exp_frac = tk.StringVar(value=str(self._d("eval_fraction")))
        self.exp_seed = tk.StringVar(value=str(self._d("seed")))
        ttk.Label(out, text="Folder").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(out, textvariable=self.exp_folder, width=70).grid(row=0, column=1, padx=4)
        ttk.Button(out, text="Browse...", command=lambda: self.exp_folder.set(
            filedialog.askdirectory(initialdir=self.exp_folder.get() or self._project_dir())
            or self.exp_folder.get())).grid(row=0, column=2)
        ttk.Label(out, text="Eval fraction").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(out, textvariable=self.exp_frac, width=10).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(out, text="Random seed").grid(row=2, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(out, textvariable=self.exp_seed, width=10).grid(row=2, column=1, sticky="w", padx=4)
        self.exp_split = ttk.Label(out, text="")
        self.exp_split.grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(3, 6))
        self.exp_frac.trace_add("write", lambda *a: self._refresh_export())

        row = ttk.Frame(tab)
        row.pack(fill="x", padx=8, pady=8)
        self.b_export = ttk.Button(row, text="Export", command=self.do_export, state="disabled")
        self.b_export.pack(side="left")
        ttk.Button(row, text="Open folder", command=self._open_folder).pack(side="left", padx=6)
        self.exp_status = ttk.Label(row, text="")
        self.exp_status.pack(side="left", padx=10)

    def _refresh_export(self):
        if not self.project:
            return
        c = self.project.counts()
        self.exp_info.config(text=(f"{c['accepted']:,} accepted (exported)    "
                                   f"{c['skipped']:,} skipped (left out)    "
                                   f"{c['pending']:,} pending (left out)"))
        try:
            n_train, n_eval = dataset_core.split_counts(c["accepted"], float(self.exp_frac.get()))
            self.exp_split.config(text=f"Split: {n_train:,} train / {n_eval:,} eval")
        except ValueError:
            self.exp_split.config(text="Split: -")
        self.b_export.config(state="normal" if c["accepted"] else "disabled")

    def do_export(self):
        if not self.project:
            return
        try:
            fraction, seed = float(self.exp_frac.get()), int(self.exp_seed.get().strip())
        except ValueError:
            messagebox.showerror("Export", "Eval fraction must be a number and the seed a whole number.")
            return
        if not 0 <= fraction < 1:
            messagebox.showerror("Export", "Eval fraction must be at least 0 and below 1.")
            return
        folder = self.exp_folder.get().strip()
        if not folder:
            messagebox.showerror("Export", "Choose the output folder.")
            return
        old = dataset_core.existing_export_files(folder, self.cfg)
        if old and not messagebox.askyesno(
                "Overwrite", f"{len(old):,} png / gt.txt file(s) already exist in the train and "
                             "eval folders there. They will be deleted and replaced by this "
                             "export. Continue?"):
            return
        project = self.project
        self.b_export.config(state="disabled")
        self.exp_status.config(text="Exporting...")
        self.bg(lambda: dataset_core.export_dataset(project, folder, fraction, seed),
                self._export_done)

    def _export_done(self, res, exc):
        self._refresh_export()
        if exc:
            self.exp_status.config(text="Export failed")
            messagebox.showerror("Export failed", str(exc))
            return
        self.exp_status.config(text=f"Wrote {res['train']:,} train + {res['eval']:,} eval")
        messagebox.showinfo("Export finished",
                            f"{res['train']:,} train and {res['eval']:,} eval pairs\n{res['folder']}")

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


# ══ dialogs ══════════════════════════════════════════════════════════════════
INVALID_NAME_CHARS = '<>:"/\\|?*'
DIALOG_SECTIONS = {"OCR": ("naming", "ocr"), "Image processing": ("image_prepare", "image_threshold")}


class CreateDialog(tk.Toplevel):

    def __init__(self, app):
        super().__init__(app)
        self.app, self.cfg = app, app.cfg
        self.title("Create dataset project")
        self.geometry(app._d("dialog_geometry"))
        self.transient(app)
        self._preview_row, self._prev_job, self._prev_img = None, None, None
        self._busy = False

        left = ttk.Frame(self)
        left.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        right = ttk.Frame(self)
        right.pack(side="right", fill="y", padx=8, pady=8)

        box = ttk.LabelFrame(left, text="Project")
        box.pack(fill="x")
        self.parent_var = tk.StringVar(value=app._project_dir())
        self.name_var = tk.StringVar()
        ttk.Label(box, text="Parent folder").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(box, textvariable=self.parent_var, width=56).grid(row=0, column=1, padx=4)
        ttk.Button(box, text="Browse...", command=self._browse_parent).grid(row=0, column=2, padx=4)
        ttk.Label(box, text="Project name").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(box, textvariable=self.name_var, width=30).grid(row=1, column=1, sticky="w", padx=4)
        self.path_lbl = ttk.Label(box, text="", foreground="gray")
        self.path_lbl.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 4))
        self.parent_var.trace_add("write", lambda *a: self._update_path())
        self.name_var.trace_add("write", lambda *a: self._update_path())

        src = ttk.LabelFrame(left, text="Source database")
        src.pack(fill="both", expand=True, pady=8)
        bar = ttk.Frame(src)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Label(bar, text="Folder").pack(side="left")
        self.root_var = tk.StringVar(value=app._project_dir())
        ttk.Entry(bar, textvariable=self.root_var, width=44).pack(side="left", padx=4)
        ttk.Button(bar, text="Browse...", command=self._browse_root).pack(side="left")
        ttk.Button(bar, text="Refresh", command=self.refresh_files).pack(side="left", padx=6)
        self.files_lbl = ttk.Label(bar, text="")
        self.files_lbl.pack(side="left", padx=6)
        frame, self.ftree = app._tree(src, ("size", "modified"), ("Database", "Size", "Modified"),
                                      (360, 90, 140), selectmode="browse", height=8)
        frame.pack(fill="both", expand=True, padx=6)
        self.ftree.bind("<<TreeviewSelect>>", lambda e: self._pick_preview_row())
        size_row = ttk.Frame(src)
        size_row.pack(fill="x", padx=6, pady=6)
        ttk.Label(size_row, text="Random sample size").pack(side="left")
        self.sample_var = tk.StringVar(value=str(app._d("sample_size")))
        ttk.Entry(size_row, textvariable=self.sample_var, width=10).pack(side="left", padx=6)

        self._build_settings(right)

        bottom = ttk.Frame(left)
        bottom.pack(fill="x")
        self.status = ttk.Label(bottom, text="")
        self.status.pack(side="left")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        self.b_create = ttk.Button(bottom, text="Create", command=self._create)
        self.b_create.pack(side="right", padx=6)

        self._update_path()
        self.refresh_files()
        self.wait_visibility()
        self.grab_set()

    # ── settings ──────────────────────────────────────────────────────────
    def _build_settings(self, parent):
        defaults = ocr_core.default_params(self.cfg)
        frame = ttk.LabelFrame(parent, text="Processing settings (fixed for this project)")
        frame.pack(fill="x")
        nb = ttk.Notebook(frame)
        nb.pack(fill="x", padx=6, pady=6)
        self.pvars = {}
        for page_title, sections in DIALOG_SECTIONS.items():
            page = ttk.Frame(nb)
            nb.add(page, text=page_title)
            page.columnconfigure(0, weight=1)
            page.columnconfigure(1, weight=1)
            n = 0
            for section, title, items in ocr_core.PARAM_SPECS:
                if section not in sections:
                    continue
                group = ttk.LabelFrame(page, text=title.replace("Image: ", ""))
                group.grid(row=n // 2, column=n % 2, sticky="nsew", padx=4, pady=4)
                n += 1
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
                        widget = ttk.Entry(group, textvariable=var, width=18 if key == "regex" else 12)
                    widget.grid(row=row, column=1, sticky="w", padx=6, pady=2)
                    self.pvars[key] = var
                    var.trace_add("write", lambda *a: self._preview_soon())
        self.wl_var = tk.BooleanVar(value=bool(self.app._d("use_whitelist")))
        ttk.Checkbutton(frame, variable=self.wl_var, text="Use the whitelist from [ocr_engine] "
                        "(off = raw text, units kept)").pack(anchor="w", padx=8, pady=(0, 6))

        pw, ph = int(self.app._d("preview_max_width")), int(self.app._d("preview_max_height"))
        prev = ttk.LabelFrame(parent, text="Preview - image fed to OCR")
        prev.pack(fill="x", pady=8)
        self._prev_size = (pw, ph)
        box = ttk.Frame(prev, width=pw + 20, height=ph + 20, relief="sunken", borderwidth=1)
        box.pack(padx=8, pady=8)
        box.pack_propagate(False)
        self.prev_lbl = ttk.Label(box, text="Select a source database", anchor="center",
                                  wraplength=pw)
        self.prev_lbl.pack(expand=True)
        row = ttk.Frame(prev)
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(row, text="Random", command=self._pick_preview_row).pack(side="left")
        self.prev_info = ttk.Label(row, text="")
        self.prev_info.pack(side="left", padx=8)

    def _get_params(self):
        return ocr_core.coerce_params({k: v.get() for k, v in self.pvars.items()}, self.cfg)

    # ── folders and source database ───────────────────────────────────────
    def _browse_parent(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.parent_var.get(),
                                         title="Parent folder of the project")
        if folder:
            self.parent_var.set(os.path.normpath(folder))

    def _browse_root(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.root_var.get(),
                                         title="Folder with databases")
        if folder:
            self.root_var.set(folder)
            self.refresh_files()

    def _update_path(self):
        name = self.name_var.get().strip()
        self.path_lbl.config(text=("Project folder: " + os.path.join(self.parent_var.get().strip(),
                                                                     name)) if name else "")

    def refresh_files(self):
        root = self.root_var.get().strip()
        if not os.path.isdir(root):
            self.files_lbl.config(text=f"Not a folder: {root}")
            return
        self.files_lbl.config(text="Scanning...")
        cfg = self.cfg
        self.app.bg(lambda: ocr_core.find_databases(root, cfg), self._fill_files)

    def _fill_files(self, dbs, exc):
        if not self.winfo_exists():
            return
        tree = self.ftree
        tree.delete(*tree.get_children())
        if exc:
            self.files_lbl.config(text=f"Scan failed: {exc}")
            return
        project_db = self.app._d("db_name").lower()
        dbs = [db for db in dbs if os.path.basename(db["path"]).lower() != project_db]
        for db in dbs:
            tree.insert("", "end", iid=db["path"], text=db["rel"],
                        values=(core.fmt_size(db["size"]), core.fmt_time(db["mtime"])))
        self.files_lbl.config(text=f"{len(dbs)} database file(s)")

    # ── preview ───────────────────────────────────────────────────────────
    def _pick_preview_row(self):
        selection = self.ftree.selection()
        if not selection:
            return
        db, timeout = selection[0], self.cfg["database"]["busy_timeout_s"]

        def pick():
            with closing(ocr_core.open_db_readonly(db, timeout)) as conn:
                row = conn.execute("SELECT id FROM captures ORDER BY RANDOM() LIMIT 1").fetchone()
            return row[0] if row else None

        def got(row_id, exc):
            if not self.winfo_exists():
                return
            if exc or row_id is None:
                self.prev_lbl.config(image="", text=f"No preview: {exc or 'no images'}")
                return
            self._preview_row = (db, row_id)
            self._show_preview()
        self.app.bg(pick, got)

    def _preview_soon(self):
        if self._prev_job:
            self.after_cancel(self._prev_job)
        self._prev_job = self.after(int(self.app._d("preview_debounce_ms")), self._show_preview)

    def _show_preview(self):
        self._prev_job = None
        if not self._preview_row:
            return
        try:
            params = self._get_params()
        except ValueError as exc:
            self.prev_info.config(text=f"Invalid setting: {exc}")
            return
        db, row_id = self._preview_row
        try:
            b64, meta = ocr_core.make_preview(db, row_id, params, self.cfg, params["stage"],
                                              *self._prev_size)
        except Exception as exc:
            self.prev_lbl.config(image="", text=f"Preview failed: {exc}")
            return
        self._prev_img = tk.PhotoImage(data=b64)
        self.prev_lbl.config(image=self._prev_img, text="")
        self.prev_info.config(text=f"{meta['region']} #{meta['seq']}   {meta['w']}x{meta['h']} px")

    # ── create ────────────────────────────────────────────────────────────
    def _create(self):
        if self._busy:
            return
        name, parent = self.name_var.get().strip(), self.parent_var.get().strip()
        if not name or not parent:
            messagebox.showerror("Create", "Parent folder and project name are required.", parent=self)
            return
        if any(ch in name for ch in INVALID_NAME_CHARS):
            messagebox.showerror("Create", f"The name cannot contain any of: {INVALID_NAME_CHARS}",
                                 parent=self)
            return
        selection = self.ftree.selection()
        if not selection:
            messagebox.showerror("Create", "Select a source database in the list.", parent=self)
            return
        try:
            size = int(self.sample_var.get().strip())
            if size < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Create", "Sample size must be a whole number of at least 1.",
                                 parent=self)
            return
        try:
            params = self._get_params()
        except ValueError as exc:
            messagebox.showerror("Create", f"Invalid setting: {exc}", parent=self)
            return
        folder, source, cfg, whitelist = os.path.join(parent, name), selection[0], self.cfg, self.wl_var.get()
        if dataset_core.peek_project(folder, cfg) is not None:
            messagebox.showerror("Create", "That folder already holds a project. Use Open, or "
                                           "choose another name.", parent=self)
            return
        self._busy = True
        self.b_create.config(state="disabled")
        self.status.config(text="Creating project (drawing the random sample)...")
        self.app.bg(lambda: dataset_core.DatasetProject.create(
            folder, source, size, cfg, params=params, use_whitelist=whitelist, name=name),
            self._created)

    def _created(self, project, exc):
        self._busy = False
        if not self.winfo_exists():
            if project:
                project.close()
            return
        if exc:
            self.b_create.config(state="normal")
            self.status.config(text="")
            messagebox.showerror("Create", str(exc), parent=self)
            return
        self.destroy()
        self.app._opened(project, None)


class OpenDialog(tk.Toplevel):

    def __init__(self, app):
        super().__init__(app)
        self.app, self.cfg = app, app.cfg
        self.title("Open dataset project")
        self.transient(app)
        self.resizable(False, False)
        self._busy = False
        row = ttk.Frame(self)
        row.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(row, text="Project folder").pack(side="left")
        self.folder_var = tk.StringVar(value=app._project_dir())
        entry = ttk.Entry(row, textvariable=self.folder_var, width=60)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: self._check())
        entry.bind("<FocusOut>", lambda e: self._check())
        ttk.Button(row, text="Browse...", command=self._browse).pack(side="left")
        self.status = ttk.Label(self, text="", justify="left", wraplength=620)
        self.status.pack(anchor="w", padx=10, pady=6)
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        self.b_open = ttk.Button(bottom, text="Open", command=self._open, state="disabled")
        self.b_open.pack(side="right", padx=6)
        self._check()
        self.wait_visibility()
        self.grab_set()

    def _browse(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.folder_var.get(),
                                         title="Dataset project folder")
        if folder:
            self.folder_var.set(os.path.normpath(folder))
            self._check()

    def _check(self):
        info = dataset_core.peek_project(self.folder_var.get().strip(), self.cfg)
        ok = False
        if info is None:
            text = "No dataset project in this folder."
        elif "error" in info:
            text = f"The project file cannot be read: {info['error']}"
        else:
            c, ok = info["counts"], True
            text = (f"Project '{info['name']}': {c['total']:,} images - {c['accepted']:,} accepted, "
                    f"{c['skipped']:,} skipped, {c['pending']:,} pending.\n"
                    f"Source database: {info['source_db']}")
        self.status.config(text=text)
        self.b_open.config(state="normal" if ok else "disabled")

    def _open(self):
        if self._busy:
            return
        folder, cfg = self.folder_var.get().strip(), self.cfg
        self._busy = True
        self.b_open.config(state="disabled")
        self.status.config(text="Opening project...")
        self.app.bg(lambda: dataset_core.DatasetProject.open(folder, cfg), self._opened)

    def _opened(self, project, exc):
        self._busy = False
        if not self.winfo_exists():
            if project:
                project.close()
            return
        if exc:
            self._check()
            messagebox.showerror("Open", str(exc), parent=self)
            return
        self.destroy()
        self.app._opened(project, None)
