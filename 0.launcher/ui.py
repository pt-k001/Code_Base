import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from core import core

BG = ("#f1f3f7", "#12151c")
SIDEBAR = ("#e4e7ee", "#0d1016")
CARD = ("#ffffff", "#1c212c")
CARD_SOFT = ("#eef1f6", "#262c3a")
ACCENT = ("#2f6bff", "#4c82ff")
ACCENT_HOVER = ("#1f56e0", "#3a6ee8")
TEXT = ("#161a22", "#eef1f7")
MUTED = ("#6a7384", "#98a1b3")
OK = ("#16a34a", "#4ade80")
BAD = ("#dc2626", "#f87171")
CARD_WRAP = 380
NOTE_WRAP = 640
NAV = (("Apps", "▦"), ("Versions", "↑"), ("Commit Log", "☰"))


def _label(parent, text="", size=13, bold=False, color=TEXT, **kw):
    return ctk.CTkLabel(parent, text=text, text_color=color,
                        font=ctk.CTkFont(size=size, weight="bold" if bold else "normal"), **kw)


def _pill(parent, text, color=ACCENT):
    return ctk.CTkLabel(parent, text=text, text_color="#ffffff", fg_color=color, corner_radius=9,
                        font=ctk.CTkFont(size=12, weight="bold"), padx=10, pady=2)


def _button(parent, text, command, soft=False, **kw):
    colors = dict(fg_color=CARD_SOFT, hover_color=("#dde2ec", "#313849"), text_color=TEXT) if soft \
        else dict(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff")
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=10,
                         font=ctk.CTkFont(size=13, weight="bold"), **colors, **kw)


class App(ctk.CTk):
    def __init__(self, cfg, version=""):
        ctk.set_appearance_mode(cfg["settings"]["appearance"])
        super().__init__(fg_color=BG)
        self.cfg = cfg
        self.apps: list[core.AppInfo] = []
        self.title(f"{cfg['gui']['title']} v{version}" if version else cfg["gui"]["title"])
        self.geometry(cfg["gui"]["window_geometry"])
        self.minsize(cfg["gui"]["min_width"], cfg["gui"]["min_height"])

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar(version)
        self.pages = {}
        for name, builder in (("Apps", self._build_apps_page), ("Versions", self._build_versions_page),
                              ("Commit Log", self._build_commits_page)):
            page = ctk.CTkFrame(self, fg_color="transparent")
            page.grid(row=0, column=1, sticky="nsew", padx=24, pady=20)
            builder(page)
            self.pages[name] = page
        self._reload()
        self._show("Apps")

    # ── shell ─────────────────────────────────────────────────────────────────
    def _build_sidebar(self, version):
        bar = ctk.CTkFrame(self, width=210, corner_radius=0, fg_color=SIDEBAR)
        bar.grid(row=0, column=0, sticky="nsw")
        bar.grid_propagate(False)
        bar.grid_rowconfigure(10, weight=1)
        _label(bar, "Launcher", 22, True).grid(row=0, column=0, padx=24, pady=(28, 2), sticky="w")
        _label(bar, "apps · versions · history", 11, color=MUTED).grid(
            row=1, column=0, padx=24, pady=(0, 24), sticky="w")
        self.nav_buttons = {}
        for i, (name, icon) in enumerate(NAV):
            btn = ctk.CTkButton(bar, text=f"  {icon}   {name}", anchor="w", height=42, corner_radius=10,
                                font=ctk.CTkFont(size=14, weight="bold"), fg_color="transparent",
                                hover_color=CARD_SOFT, text_color=TEXT, command=lambda n=name: self._show(n))
            btn.grid(row=2 + i, column=0, padx=14, pady=3, sticky="ew")
            self.nav_buttons[name] = btn
        self.dark_var = ctk.BooleanVar(value=self.cfg["settings"]["appearance"] == "dark")
        ctk.CTkSwitch(bar, text="Dark mode", variable=self.dark_var, command=self._toggle_theme,
                      progress_color=ACCENT, text_color=TEXT).grid(row=11, column=0, padx=24, pady=(0, 10), sticky="w")
        _label(bar, f"v{version}" if version else "", 11, color=MUTED).grid(
            row=12, column=0, padx=24, pady=(0, 20), sticky="w")
        bar.grid_columnconfigure(0, weight=1)

    def _show(self, name):
        self.pages[name].tkraise()
        for key, btn in self.nav_buttons.items():
            btn.configure(fg_color=ACCENT if key == name else "transparent",
                          text_color="#ffffff" if key == name else TEXT,
                          hover_color=ACCENT_HOVER if key == name else CARD_SOFT)

    def _toggle_theme(self):
        mode = "dark" if self.dark_var.get() else "light"
        ctk.set_appearance_mode(mode)
        self.cfg["settings"]["appearance"] = mode
        core.save_config(self.cfg)

    def _reload(self):
        self.apps = core.scan_apps(self.cfg)
        self._render_apps()
        self._refresh_version_menu()
        self._render_commits()

    def _description(self, app):
        return self.cfg["descriptions"].get(app.folder, "").strip() or app.doc_line

    # ── apps page ─────────────────────────────────────────────────────────────
    def _build_apps_page(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        _label(head, "Apps", 28, True).grid(row=0, column=0, sticky="w")
        self.apps_count = _label(head, "", 12, color=MUTED)
        self.apps_count.grid(row=1, column=0, sticky="w")
        self.search_apps = ctk.CTkEntry(head, placeholder_text="Search apps…", width=260, height=36,
                                        corner_radius=10)
        self.search_apps.grid(row=0, column=1, rowspan=2, padx=(0, 14))
        self.search_apps.bind("<KeyRelease>", lambda _e: self._render_apps())
        self.quit_var = ctk.BooleanVar(value=bool(self.cfg["settings"]["quit_after_open"]))
        ctk.CTkSwitch(head, text="Close after opening", variable=self.quit_var, command=self._toggle_quit,
                      progress_color=ACCENT, text_color=TEXT).grid(row=0, column=2, rowspan=2)
        self.apps_status = _label(page, "", 12, color=MUTED, anchor="w")
        self.apps_status.grid(row=1, column=0, sticky="ew", pady=(10, 6))
        self.cards = ctk.CTkScrollableFrame(page, fg_color="transparent")
        self.cards.grid(row=2, column=0, sticky="nsew")
        self.cards.grid_columnconfigure((0, 1), weight=1, uniform="cards")

    def _toggle_quit(self):
        self.cfg["settings"]["quit_after_open"] = self.quit_var.get()
        core.save_config(self.cfg)

    def _render_apps(self):
        for child in self.cards.winfo_children():
            child.destroy()
        needle = self.search_apps.get().strip().lower()
        shown = [a for a in self.apps
                 if needle in f"{a.folder} {self._description(a)}".lower()]
        self.apps_count.configure(text=f"{len(shown)} of {len(self.apps)} apps in {core.apps_dir(self.cfg)}")
        if not shown:
            _label(self.cards, "No apps found.", 14, color=MUTED).grid(row=0, column=0, pady=40, sticky="w")
        for i, app in enumerate(shown):
            self._card(app).grid(row=i // 2, column=i % 2, padx=8, pady=8, sticky="nsew")

    def _card(self, app):
        card = ctk.CTkFrame(self.cards, corner_radius=16, fg_color=CARD)
        card.grid_columnconfigure(0, weight=1)
        _label(card, app.folder, 17, True, anchor="w").grid(row=0, column=0, padx=(20, 8), pady=(18, 4), sticky="w")
        _pill(card, f"v{app.version}" if app.version else "no version",
              ACCENT if app.version else MUTED).grid(row=0, column=1, padx=(0, 18), pady=(18, 4))
        text = self._description(app)
        _label(card, text or "No description yet. Use Edit to add one.", 13,
               color=TEXT if text else MUTED, wraplength=CARD_WRAP, justify="left", anchor="w").grid(
            row=1, column=0, columnspan=2, padx=20, pady=(0, 14), sticky="ew")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.grid(row=2, column=0, columnspan=2, padx=16, pady=(0, 16), sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        _button(row, "Edit", lambda a=app: self._edit_description(a), soft=True, width=70, height=34).grid(
            row=0, column=1, padx=(0, 8))
        if core.is_self(app):
            _label(row, "this launcher", 12, color=MUTED).grid(row=0, column=0, sticky="w", padx=4)
        else:
            _button(row, "Open", lambda a=app: self._open(a), width=110, height=34).grid(row=0, column=2)
        return card

    def _edit_description(self, app):
        top = ctk.CTkToplevel(self, fg_color=BG)
        top.title(f"Description - {app.folder}")
        top.geometry("520x300")
        top.transient(self)
        top.after(100, top.grab_set)
        _label(top, app.folder, 16, True).pack(anchor="w", padx=20, pady=(18, 8))
        box = ctk.CTkTextbox(top, corner_radius=10, fg_color=CARD, text_color=TEXT, wrap="word")
        box.pack(fill="both", expand=True, padx=20)
        box.insert("1.0", self.cfg["descriptions"].get(app.folder, ""))

        def save():
            text = box.get("1.0", "end").strip()
            if text:
                self.cfg["descriptions"][app.folder] = text
            else:
                self.cfg["descriptions"].pop(app.folder, None)
            core.save_config(self.cfg)
            top.destroy()
            self._render_apps()

        _button(top, "Save", save, height=36).pack(anchor="e", padx=20, pady=14)

    def _open(self, app):
        try:
            proc, err = core.launch_app(app)
        except OSError as exc:
            messagebox.showerror("Cannot open", str(exc), parent=self)
            return
        self.apps_status.configure(text=f"Opening {app.folder}…", text_color=MUTED)
        self.after(int(self.cfg["settings"]["launch_check_ms"]), lambda: self._after_open(app, proc, err))

    def _after_open(self, app, proc, err):
        problem = core.launch_error(proc, err)
        if problem:
            self.apps_status.configure(text=f"{app.folder} failed to start.", text_color=BAD)
            messagebox.showerror(f"{app.folder} failed to start", problem, parent=self)
        elif self.cfg["settings"]["quit_after_open"]:
            self.destroy()
        else:
            self.apps_status.configure(text=f"{app.folder} is running.", text_color=OK)

    # ── versions page ─────────────────────────────────────────────────────────
    def _build_versions_page(self, page):
        page.grid_columnconfigure(0, weight=1)
        _label(page, "Versions", 28, True).grid(row=0, column=0, sticky="w")
        _label(page, "Each bump adds 0.1 and is written to version.toml, changelog.toml and CHANGELOG.md.",
               12, color=MUTED).grid(row=1, column=0, sticky="w", pady=(0, 16))
        card = ctk.CTkFrame(page, corner_radius=16, fg_color=CARD)
        card.grid(row=2, column=0, sticky="new")
        card.grid_columnconfigure(0, weight=1)

        _label(card, "App", 12, color=MUTED).grid(row=0, column=0, padx=24, pady=(20, 4), sticky="w")
        self.version_app = ctk.StringVar()
        self.version_menu = ctk.CTkOptionMenu(card, variable=self.version_app, values=[""], height=38,
                                              corner_radius=10, fg_color=CARD_SOFT, button_color=ACCENT,
                                              button_hover_color=ACCENT_HOVER, text_color=TEXT,
                                              command=lambda _v: self._update_version_view())
        self.version_menu.grid(row=1, column=0, padx=24, sticky="w")

        flow = ctk.CTkFrame(card, fg_color="transparent")
        flow.grid(row=2, column=0, padx=24, pady=18, sticky="w")
        self.cur_pill = ctk.CTkLabel(flow, text="", font=ctk.CTkFont(size=26, weight="bold"), text_color=TEXT,
                                     fg_color=CARD_SOFT, corner_radius=12, padx=18, pady=8)
        self.cur_pill.grid(row=0, column=0)
        _label(flow, "→", 24, True, color=MUTED).grid(row=0, column=1, padx=14)
        self.next_pill = ctk.CTkLabel(flow, text="", font=ctk.CTkFont(size=26, weight="bold"),
                                      text_color="#ffffff", fg_color=ACCENT, corner_radius=12, padx=18, pady=8)
        self.next_pill.grid(row=0, column=2)

        _label(card, "Change log note (required)", 12, color=MUTED).grid(row=3, column=0, padx=24, pady=(0, 4), sticky="w")
        self.note_box = ctk.CTkTextbox(card, height=150, corner_radius=10, fg_color=CARD_SOFT, text_color=TEXT,
                                       wrap="word")
        self.note_box.grid(row=4, column=0, padx=24, sticky="ew")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.grid(row=5, column=0, padx=24, pady=18, sticky="ew")
        row.grid_columnconfigure(1, weight=1)
        self.bump_btn = _button(row, "Bump +0.1", self._bump, height=40, width=150)
        self.bump_btn.grid(row=0, column=0)
        self.bump_status = _label(row, "", 12, color=MUTED, anchor="w")
        self.bump_status.grid(row=0, column=1, padx=16, sticky="w")

    def _selected_app(self):
        return next((a for a in self.apps if a.folder == self.version_app.get()), None)

    def _refresh_version_menu(self):
        names = [a.folder for a in self.apps] or [""]
        self.version_menu.configure(values=names)
        if self.version_app.get() not in names:
            self.version_app.set(names[0])
        self._update_version_view()

    def _update_version_view(self):
        app = self._selected_app()
        if not app:
            self.cur_pill.configure(text="-")
            self.next_pill.configure(text="-")
            return
        self.cur_pill.configure(text=app.version or "none")
        try:
            self.next_pill.configure(text=core.next_version(app.version))
            self.bump_btn.configure(state="normal")
        except ValueError:
            self.next_pill.configure(text="?")
            self.bump_btn.configure(state="disabled")

    def _bump(self):
        app = self._selected_app()
        note = self.note_box.get("1.0", "end").strip()
        if not app:
            return
        if not note:
            self.bump_status.configure(text="Write a change log note first.", text_color=BAD)
            return
        new = core.next_version(app.version)
        if not messagebox.askyesno("Confirm bump", f"Bump {app.folder} from {app.version or 'none'} to {new}?",
                                   parent=self):
            return
        try:
            core.apply_bump(app, note)
        except (OSError, ValueError) as exc:
            self.bump_status.configure(text=str(exc), text_color=BAD)
            return
        self.note_box.delete("1.0", "end")
        self.bump_status.configure(text=f"{app.folder} is now {new}.", text_color=OK)
        self._reload()

    # ── commit log page ───────────────────────────────────────────────────────
    def _build_commits_page(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        _label(head, "Commit Log", 28, True).grid(row=0, column=0, sticky="w")
        self.commit_count = _label(head, "", 12, color=MUTED)
        self.commit_count.grid(row=1, column=0, sticky="w")
        self.commit_filter = ctk.StringVar(value="All apps")
        self.filter_menu = ctk.CTkOptionMenu(head, variable=self.commit_filter, values=["All apps"], height=36,
                                             corner_radius=10, fg_color=CARD, button_color=ACCENT,
                                             button_hover_color=ACCENT_HOVER, text_color=TEXT,
                                             command=lambda _v: self._render_commits())
        self.filter_menu.grid(row=0, column=1, rowspan=2, padx=(0, 12))
        self.search_commits = ctk.CTkEntry(head, placeholder_text="Search notes…", width=240, height=36,
                                           corner_radius=10)
        self.search_commits.grid(row=0, column=2, rowspan=2, padx=(0, 12))
        self.search_commits.bind("<KeyRelease>", lambda _e: self._render_commits())
        _button(head, "Refresh", self._reload, soft=True, width=90, height=36).grid(row=0, column=3, rowspan=2)
        self.commit_warn = _label(page, "", 12, color=BAD, anchor="w")
        self.commit_warn.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.commit_list = ctk.CTkScrollableFrame(page, fg_color="transparent")
        self.commit_list.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.commit_list.grid_columnconfigure(0, weight=1)

    def _render_commits(self):
        names = ["All apps"] + [a.folder for a in self.apps]
        self.filter_menu.configure(values=names)
        if self.commit_filter.get() not in names:
            self.commit_filter.set("All apps")
        commits, problems = core.all_commits(self.apps)
        self.commit_warn.configure(text="  |  ".join(problems))

        chosen = self.commit_filter.get()
        needle = self.search_commits.get().strip().lower()
        commits = [c for c in commits if (chosen == "All apps" or c["app"] == chosen)
                   and needle in f"{c['note']} {c['app']} {c['user']}".lower()]
        limit = int(self.cfg["settings"]["max_commits_shown"])
        self.commit_count.configure(
            text=f"{len(commits)} entries" + (f" (showing newest {limit})" if len(commits) > limit else ""))

        for child in self.commit_list.winfo_children():
            child.destroy()
        if not commits:
            _label(self.commit_list, "No commits yet. Bump a version to record one.", 14, color=MUTED).grid(
                row=0, column=0, pady=40, sticky="w")
        for i, c in enumerate(commits[:limit]):
            row = ctk.CTkFrame(self.commit_list, corner_radius=14, fg_color=CARD)
            row.grid(row=i, column=0, sticky="ew", pady=6, padx=2)
            row.grid_columnconfigure(1, weight=1)
            badge = f"{c['from']} → {c['to']}" if c["from"] else c["to"]
            _pill(row, badge).grid(row=0, column=0, padx=(18, 12), pady=(16, 4), sticky="w")
            _label(row, c["app"], 14, True, anchor="w").grid(row=0, column=1, pady=(16, 4), sticky="w")
            _label(row, f"{c['time'].replace('T', ' ')[:16]}  ·  {c['user']}", 12, color=MUTED).grid(
                row=0, column=2, padx=18, pady=(16, 4))
            _label(row, c["note"], 13, wraplength=NOTE_WRAP, justify="left", anchor="w").grid(
                row=1, column=0, columnspan=3, padx=18, pady=(0, 16), sticky="ew")
