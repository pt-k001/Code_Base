import datetime
import os
import queue
import re
import threading
import time
import tkinter as tk
import traceback
from tkinter import ttk, filedialog, messagebox, simpledialog

from core import core
from core.ssh_core import (
    DEFAULT_PORT,
    CommandRunner,
    HostKeyMismatchError,
    KnownHostsStore,
    Logger,
    ProfileStore,
    SSHManager,
    helper_format_record,
    helper_now_timestamp,
    helper_open_folder,
    helper_parse_interval,
    helper_run_command,
    helper_tail_read_lines,
    helper_validate_command,
    helper_validate_host,
    helper_validate_port,
    helper_validate_username,
)


# ── Command Row ───────────────────────────────────────────────────────────────
class CommandRow(tk.Frame):
    _PLACEHOLDER = "tag"

    def __init__(self, parent, limits, on_remove, on_run_once, **kwargs):
        super().__init__(parent, relief="groove", bd=1, **kwargs)
        self._limits = limits
        self._on_remove = on_remove
        self._on_run_once = on_run_once
        self._enabled = True
        self._tag_placeholder = True
        self._build()

    def _build(self):
        self.columnconfigure(1, weight=1)

        self._en_var = tk.BooleanVar(value=True)
        enable_check = tk.Checkbutton(
            self, variable=self._en_var, command=self._toggle)
        enable_check.grid(row=0, column=0, padx=(4, 2), pady=4)

        self._cmd_var = tk.StringVar()
        self._cmd_entry = tk.Entry(
            self, textvariable=self._cmd_var, font=("Courier", 10))
        self._cmd_entry.grid(row=0, column=1, sticky="ew", padx=2, pady=4)

        self._tag_var = tk.StringVar(value=self._PLACEHOLDER)
        self._tag_entry = tk.Entry(self, textvariable=self._tag_var, width=8)
        self._tag_entry.grid(row=0, column=2, padx=(2, 4), pady=4)
        self._tag_entry.bind("<FocusIn>", self._tag_in)
        self._tag_entry.bind("<FocusOut>", self._tag_out)

        bottom = tk.Frame(self)
        bottom.grid(row=1, column=0, columnspan=3, sticky="ew",
                    padx=4, pady=(0, 4))

        every_label = tk.Label(bottom, text="Every")
        every_label.pack(side="left")

        self._interval_var = tk.StringVar(value=str(self._limits["default_interval_s"]))
        interval_spin = tk.Spinbox(
            bottom, from_=self._limits["min_interval_s"], to=self._limits["max_interval_s"],
            textvariable=self._interval_var, width=6)
        interval_spin.pack(side="left", padx=2)

        seconds_label = tk.Label(bottom, text="s")
        seconds_label.pack(side="left", padx=(0, 8))

        run_once_button = tk.Button(
            bottom, text="Run Once", command=self._run_once_clicked)
        run_once_button.pack(side="left", padx=(0, 4))

        remove_button = tk.Button(bottom, text="Remove", command=self._remove)
        remove_button.pack(side="right")

    def _run_once_clicked(self):
        self._on_run_once(self)

    def _tag_in(self, _event):
        if self._tag_placeholder:
            self._tag_var.set("")
            self._tag_placeholder = False

    def _tag_out(self, _event):
        if not self._tag_var.get().strip():
            self._tag_var.set(self._PLACEHOLDER)
            self._tag_placeholder = True

    def _toggle(self):
        self._enabled = self._en_var.get()
        if self._enabled:
            self._cmd_entry.config(state="normal")
        else:
            self._cmd_entry.config(state="disabled")

    def _remove(self):
        self._on_remove(self)
        self.destroy()

    def to_dict(self):
        return {
            "cmd": self._cmd_var.get().strip(),
            "interval": self.interval,
            "tag": self.tag,
            "enabled": self._enabled,
        }

    def from_dict(self, data):
        self._cmd_var.set(str(data.get("cmd", "")))

        interval = helper_parse_interval(data.get("interval"), self._limits)
        self._interval_var.set(str(interval))

        tag = str(data.get("tag", "")).strip()
        if tag:
            self._tag_var.set(tag)
            self._tag_placeholder = False
        else:
            self._tag_var.set(self._PLACEHOLDER)
            self._tag_placeholder = True

        self._en_var.set(bool(data.get("enabled", True)))
        self._toggle()

    @property
    def command(self):
        return self._cmd_var.get().strip()

    @property
    def interval(self):
        return helper_parse_interval(self._interval_var.get(), self._limits)

    @property
    def tag(self):
        if self._tag_placeholder:
            return ""
        return self._tag_var.get().strip()

    @property
    def enabled(self):
        return self._enabled


# ── Stats bar ─────────────────────────────────────────────────────────────────
class StatsBar(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, relief="sunken", bd=1, **kwargs)
        self._start_time = None
        self._records = 0
        self._errors = 0
        self._reconnects = 0
        self._after_id = None

        self._uptime_lbl = self._make_stat("Uptime", 0)
        self._rec_lbl = self._make_stat("Records", 1)
        self._err_lbl = self._make_stat("Errors", 2)
        self._cmd_lbl = self._make_stat("Commands", 3)
        self._recon_lbl = self._make_stat("Reconnects", 4)

    def _make_stat(self, label, column):
        left_padding = 8 if column == 0 else 4
        caption = tk.Label(self, text=label + ":")
        caption.grid(row=0, column=column * 2, padx=(left_padding, 2), pady=3)

        value = tk.Label(self, text="--", width=10, anchor="w")
        value.grid(row=0, column=column * 2 + 1, padx=(0, 6))
        return value

    def start(self, command_count):
        self._cancel_tick()
        self._start_time = time.monotonic()
        self._records = 0
        self._errors = 0
        self._reconnects = 0
        self._cmd_lbl.config(text=str(command_count))
        self._rec_lbl.config(text="0")
        self._err_lbl.config(text="0")
        self._recon_lbl.config(text="0")
        self._tick()

    def stop(self):
        self._start_time = None
        self._cancel_tick()
        self._uptime_lbl.config(text="--")

    def _cancel_tick(self):
        if self._after_id is None:
            return
        try:
            self.after_cancel(self._after_id)
        except (tk.TclError, ValueError):
            pass
        self._after_id = None

    def add_record(self):
        self._records += 1
        self._rec_lbl.config(text=str(self._records))

    def add_error(self):
        self._errors += 1
        self._err_lbl.config(text=str(self._errors))

    def add_reconnect(self):
        self._reconnects += 1
        self._recon_lbl.config(text=str(self._reconnects))

    def _tick(self):
        if self._start_time is None:
            return
        elapsed = int(time.monotonic() - self._start_time)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        self._uptime_lbl.config(text=f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        self._after_id = self.after(1000, self._tick)


# ═══════════════════════════════════════════════════════════════════════════════
# ConnectionPane
# ═══════════════════════════════════════════════════════════════════════════════
class ConnectionPane(tk.Frame):
    def __init__(self, parent, cfg, tab_number, profiles, known_hosts, tab_label_var, **kwargs):
        super().__init__(parent, **kwargs)
        self._cfg = cfg
        self._limits = cfg["limits"]
        self._viewer = cfg["viewer"]
        self._recon_cfg = cfg["reconnect"]
        self._tab_number = tab_number
        self._profiles = profiles
        self._tab_label = tab_label_var
        self._scale = core.helper_dpi_scale(self)

        self._ssh = SSHManager(known_hosts, cfg)
        self._logger = None
        self._runners = []
        self._rows = []
        self._event_q = queue.Queue()

        self._autoscroll = True
        self._filter_text = ""
        self._all_blocks = []

        self._log_filepath = None
        self._logging_active = False   # True from Start -> Stop

        # connection / reconnect state
        self._closed = False
        self._connecting = False
        self._session_token = 0        # bumped on disconnect/teardown
        self._reconnecting = False
        self._reconnect_attempts = 0
        self._reconnect_after_id = None
        self._watchdog_after_id = None
        self._poll_after_id = None

        self._build_ui()
        self._refresh_profile_list()
        self._poll_event_queue()

    def _px(self, value):
        """Scale a hard-coded pixel size to the current display DPI."""
        return core.helper_scale_px(value, self._scale)

    # ── Thread -> UI hand-off ─────────────────────────────────────────────────
    def _post(self, function, *arguments):
        self._event_q.put(("call", (function, arguments)))

    def _poll_event_queue(self):
        if self._closed:
            return

        processed = 0
        while processed < int(self._viewer["max_events_per_tick"]):
            try:
                kind, payload = self._event_q.get_nowait()
            except queue.Empty:
                break
            processed += 1
            self._handle_event(kind, payload)

        self._poll_after_id = self.after(int(self._viewer["poll_ms"]),
                                         self._poll_event_queue)

    def _handle_event(self, kind, payload):
        if kind == "call":
            function, arguments = payload
            try:
                function(*arguments)
            except Exception:
                traceback.print_exc()
        elif kind == "error":
            self._stats.add_error()
        elif kind == "record":
            self._handle_record(payload)

    def _handle_record(self, block):
        self._remember_block(block)
        if self._passes_filter(block):
            self._append_to_viewer(block)
        self._stats.add_record()

    def _remember_block(self, block):
        self._all_blocks.append(block)
        if len(self._all_blocks) > int(self._viewer["max_memory_blocks"]):
            self._all_blocks = self._all_blocks[-int(self._viewer["keep_memory_blocks"]):]

    # ── UI build ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # Status bar
        top = tk.Frame(self, relief="sunken", bd=1)
        top.grid(row=0, column=0, sticky="ew", padx=4, pady=2)
        top.columnconfigure(1, weight=1)

        self._status_lbl = tk.Label(top, text="Disconnected", width=30, anchor="w")
        self._status_lbl.grid(row=0, column=0, padx=6, pady=3, sticky="w")

        self._log_file_lbl = tk.Label(top, text="", anchor="w")
        self._log_file_lbl.grid(row=0, column=1, padx=6, sticky="w")

        # Stats
        self._stats = StatsBar(self)
        self._stats.grid(row=1, column=0, sticky="ew", padx=4, pady=2)

        # Paned layout
        paned = tk.PanedWindow(self, orient="horizontal", sashwidth=self._px(5),
                               sashrelief="raised")
        paned.grid(row=2, column=0, sticky="nsew", padx=4, pady=2)

        left = self._build_left(paned)
        right = self._build_right(paned)
        paned.add(left, minsize=self._px(340))
        paned.add(right, minsize=self._px(460))
        paned.paneconfigure(left, width=self._px(460))

    def _section_label(self, parent, text):
        frame = tk.Frame(parent)
        frame.pack(fill="x", padx=6, pady=(8, 2))

        label = tk.Label(frame, text=text, font=("TkDefaultFont", 9, "bold"),
                         anchor="w")
        label.pack(side="left")

        rule = tk.Frame(frame, relief="groove", bd=1, height=self._px(2))
        rule.pack(side="left", fill="x", expand=True, padx=(4, 0), pady=5)

    # ── Left panel ────────────────────────────────────────────────────────────
    def _build_left(self, parent):
        outer = tk.Frame(parent, relief="groove", bd=1)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def resize(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(window_id, width=canvas.winfo_width())

        inner.bind("<Configure>", resize)
        canvas.bind("<Configure>", resize)

        self._build_profiles_section(inner)
        self._build_connection_section(inner)
        self._build_reconnect_section(inner)
        self._build_commands_section(inner)
        self._build_logging_section(inner)
        return outer

    # ── Profiles ──────────────────────────────────────────────────────────────
    def _build_profiles_section(self, parent):
        self._section_label(parent, "Profiles")
        frame = tk.Frame(parent)
        frame.pack(fill="x", padx=6, pady=(0, 4))
        frame.columnconfigure(0, weight=1)

        self._profile_var = tk.StringVar()
        self._profile_cb = ttk.Combobox(frame, textvariable=self._profile_var,
                                        state="readonly")
        self._profile_cb.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        button_frame = tk.Frame(frame)
        button_frame.grid(row=0, column=1)

        buttons = [
            ("Load", self._profile_load),
            ("Save", self._profile_save),
            ("Delete", self._profile_delete),
        ]
        for label, handler in buttons:
            button = tk.Button(button_frame, text=label, command=handler, width=6)
            button.pack(side="left", padx=(0, 2))

    def _refresh_profile_list(self):
        names = self._profiles.names()
        self._profile_cb["values"] = names
        if names and not self._profile_var.get():
            self._profile_var.set(names[0])

    def _profile_save(self):
        initial_name = (self._profile_var.get()
                        or self._host_var.get().strip()
                        or "new-profile")
        name = simpledialog.askstring(
            "Save Profile", "Profile name:",
            initialvalue=initial_name,
            parent=self.winfo_toplevel())
        if not name:
            return

        password = self._pass_var.get()
        if password:
            store_password = messagebox.askyesno(
                "Save Password?",
                "Store the password in profiles.json as plain text?\n\n"
                "Choose 'No' to save the profile without the password.",
                parent=self.winfo_toplevel())
            if not store_password:
                password = ""

        profile = {
            "host": self._host_var.get().strip(),
            "port": self._port_var.get().strip(),
            "user": self._user_var.get().strip(),
            "password": password,
            "key_path": self._key_var.get().strip(),
            "commands": [row.to_dict() for row in self._rows],
            "max_recon": self._max_recon_var.get(),
            "recon_delay": self._recon_delay_var.get(),
        }

        try:
            saved_name = self._profiles.save(name, profile)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Save Failed", str(exc),
                                 parent=self.winfo_toplevel())
            return

        self._profile_var.set(saved_name)
        self._refresh_profile_list()
        self._log_status.config(text=f"Profile '{saved_name}' saved.")

    def _profile_load(self):
        name = self._profile_var.get()
        if not name:
            return

        profile = self._profiles.load(name)
        if not profile:
            messagebox.showwarning("Not Found", f"Profile '{name}' not found.",
                                   parent=self.winfo_toplevel())
            return

        self._host_var.set(str(profile.get("host", "")))
        self._port_var.set(str(profile.get("port", str(DEFAULT_PORT))))
        self._user_var.set(str(profile.get("user", "")))
        self._pass_var.set(str(profile.get("password", "")))
        self._key_var.set(str(profile.get("key_path", "")))
        self._max_recon_var.set(
            str(profile.get("max_recon", self._recon_cfg["max_attempts"])))
        self._recon_delay_var.set(
            str(profile.get("recon_delay", self._recon_cfg["delay_s"])))

        for row in list(self._rows):
            self._remove_row(row)
            row.destroy()

        commands = profile.get("commands", [])
        if not isinstance(commands, list):
            commands = []

        for command_data in commands[:int(self._limits["max_command_rows"])]:
            if isinstance(command_data, dict):
                self._add_row(command_data)

        if not self._rows:
            self._add_row()

    def _profile_delete(self):
        name = self._profile_var.get()
        if not name:
            return

        confirmed = messagebox.askyesno(
            "Delete Profile", f"Delete '{name}'?",
            parent=self.winfo_toplevel())
        if not confirmed:
            return

        try:
            self._profiles.delete(name)
        except OSError as exc:
            messagebox.showerror("Delete Failed", str(exc),
                                 parent=self.winfo_toplevel())
            return

        self._profile_var.set("")
        self._refresh_profile_list()

    # ── Connection section ────────────────────────────────────────────────────
    def _build_connection_section(self, parent):
        self._section_label(parent, "Connection")
        grid = tk.Frame(parent)
        grid.pack(fill="x", padx=6)
        grid.columnconfigure(1, weight=1)

        self._host_var = self._helper_make_field(grid, 0, "Host/IP")
        self._port_var = self._helper_make_field(grid, 1, "Port", default=str(DEFAULT_PORT))
        self._user_var = self._helper_make_field(grid, 2, "User", default="root")
        self._pass_var = self._helper_make_field(grid, 3, "Password", show="*")

        key_label = tk.Label(grid, text="SSH Key", anchor="e", width=10)
        key_label.grid(row=4, column=0, sticky="e", pady=2, padx=(0, 4))

        self._key_var = tk.StringVar()
        key_entry = tk.Entry(grid, textvariable=self._key_var)
        key_entry.grid(row=4, column=1, sticky="ew", pady=2)

        browse_button = tk.Button(grid, text="Browse...", command=self._browse_key)
        browse_button.grid(row=4, column=2, padx=(4, 0), pady=2)

        key_hint = tk.Label(
            grid,
            text="Key overrides password. For encrypted keys, put the "
                 "passphrase in Password.",
            font=("TkDefaultFont", 8), anchor="w")
        key_hint.grid(row=5, column=1, columnspan=2, sticky="w")

        buttons = tk.Frame(parent)
        buttons.pack(fill="x", padx=6, pady=(4, 2))

        connect_button = tk.Button(buttons, text="Connect",
                                   command=self._on_connect, width=12)
        connect_button.pack(side="left", padx=(0, 6))

        disconnect_button = tk.Button(buttons, text="Disconnect",
                                      command=self._on_disconnect, width=12)
        disconnect_button.pack(side="left")

    @staticmethod
    def _helper_make_field(grid, row, label, show="", default=""):
        caption = tk.Label(grid, text=label, anchor="e", width=10)
        caption.grid(row=row, column=0, sticky="e", pady=2, padx=(0, 4))

        variable = tk.StringVar(value=default)
        entry = tk.Entry(grid, textvariable=variable, show=show)
        entry.grid(row=row, column=1, sticky="ew", pady=2, columnspan=2)
        return variable

    def _remember(self, key, value):
        self._cfg["paths"][key] = value
        core.helper_remember_path(key, value)

    def _browse_key(self):
        path = filedialog.askopenfilename(
            title="Select SSH Private Key",
            filetypes=[("Key files", "id_rsa id_ed25519 *.pem *.key *"),
                       ("All files", "*.*")],
            parent=self.winfo_toplevel(),
            **core.helper_dialog_start(self._cfg["paths"]["key_file"]))
        if path:
            self._key_var.set(path)
            self._remember("key_file", path)

    # ── Auto-reconnect settings ───────────────────────────────────────────────
    def _build_reconnect_section(self, parent):
        self._section_label(parent, "Auto-Reconnect")
        grid = tk.Frame(parent)
        grid.pack(fill="x", padx=6, pady=(0, 4))
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        self._autorecon_var = tk.BooleanVar(value=True)
        enable_check = tk.Checkbutton(
            grid, text="Enable auto-reconnect on connection loss",
            variable=self._autorecon_var)
        enable_check.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

        attempts_label = tk.Label(grid, text="Max attempts:", anchor="e")
        attempts_label.grid(row=1, column=0, sticky="e", padx=(0, 4), pady=2)

        self._max_recon_var = tk.StringVar(value=str(self._recon_cfg["max_attempts"]))
        attempts_spin = tk.Spinbox(grid, from_=1, to=999,
                                   textvariable=self._max_recon_var, width=5)
        attempts_spin.grid(row=1, column=1, sticky="w", padx=(0, 12))

        delay_label = tk.Label(grid, text="Retry delay (s):", anchor="e")
        delay_label.grid(row=1, column=2, sticky="e", padx=(0, 4))

        self._recon_delay_var = tk.StringVar(value=str(self._recon_cfg["delay_s"]))
        delay_spin = tk.Spinbox(grid, from_=1, to=300,
                                textvariable=self._recon_delay_var, width=5)
        delay_spin.grid(row=1, column=3, sticky="w")

        self._recon_status_lbl = tk.Label(parent, text="", anchor="w",
                                          font=("TkDefaultFont", 9))
        self._recon_status_lbl.pack(fill="x", padx=6, pady=(0, 4))

    # ── Commands section ──────────────────────────────────────────────────────
    def _build_commands_section(self, parent):
        self._section_label(parent, "Scheduled Commands")
        container = tk.Frame(parent)
        container.pack(fill="x", padx=6)

        self._rows_canvas = tk.Canvas(container, height=self._px(240), bd=1, relief="sunken")
        scrollbar = tk.Scrollbar(container, orient="vertical",
                                 command=self._rows_canvas.yview)
        self._rows_frame = tk.Frame(self._rows_canvas)
        self._rows_frame.bind("<Configure>", self._update_rows_scrollregion)

        self._rows_canvas.create_window((0, 0), window=self._rows_frame,
                                        anchor="nw")
        self._rows_canvas.configure(yscrollcommand=scrollbar.set)
        self._rows_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._rows_canvas.bind("<MouseWheel>", self._on_rows_mousewheel)
        self._rows_canvas.bind("<Button-4>", self._on_rows_mousewheel)
        self._rows_canvas.bind("<Button-5>", self._on_rows_mousewheel)

        add_button = tk.Button(parent, text="Add Command", command=self._add_row)
        add_button.pack(fill="x", padx=6, pady=4)
        self._add_row()

    def _update_rows_scrollregion(self, _event=None):
        self._rows_canvas.configure(scrollregion=self._rows_canvas.bbox("all"))

    def _on_rows_mousewheel(self, event):
        if getattr(event, "num", None) == 4:
            direction = -1
        elif getattr(event, "num", None) == 5:
            direction = 1
        elif event.delta > 0:
            direction = -1
        elif event.delta < 0:
            direction = 1
        else:
            return
        self._rows_canvas.yview_scroll(direction, "units")

    def _add_row(self, data=None):
        if len(self._rows) >= int(self._limits["max_command_rows"]):
            messagebox.showwarning(
                "Too Many Commands",
                f"A maximum of {self._limits['max_command_rows']} commands is allowed.",
                parent=self.winfo_toplevel())
            return

        row = CommandRow(self._rows_frame, self._limits,
                         on_remove=self._remove_row,
                         on_run_once=self._run_once)
        if data:
            row.from_dict(data)
        row.pack(fill="x", pady=(0, 2))
        self._rows.append(row)
        self._rows_canvas.update_idletasks()
        self._update_rows_scrollregion()

    def _remove_row(self, row):
        if row in self._rows:
            self._rows.remove(row)

    def _collect_enabled_rows(self):
        selected = []
        for row in self._rows:
            if not row.enabled or not row.command:
                continue
            try:
                helper_validate_command(row.command, self._limits["max_command"])
            except ValueError as exc:
                messagebox.showwarning("Invalid Command", str(exc),
                                       parent=self.winfo_toplevel())
                return None
            selected.append(row)

        if not selected:
            messagebox.showwarning("No Commands",
                                   "Add at least one enabled command.",
                                   parent=self.winfo_toplevel())
            return None
        return selected

    # ── Logging controls ──────────────────────────────────────────────────────
    def _build_logging_section(self, parent):
        self._section_label(parent, "Logging")
        buttons = tk.Frame(parent)
        buttons.pack(fill="x", padx=6, pady=(0, 4))

        self._start_btn = tk.Button(buttons, text="Start Logging",
                                    command=self._on_start_logging, width=14)
        self._start_btn.pack(side="left", padx=(0, 6))

        self._continue_btn = tk.Button(buttons, text="Continue Logging...",
                                       command=self._on_continue_logging,
                                       width=16)
        self._continue_btn.pack(side="left", padx=(0, 6))

        stop_button = tk.Button(buttons, text="Stop Logging",
                                command=self._on_stop_logging, width=14)
        stop_button.pack(side="left")

        self._log_status = tk.Label(parent, text="", anchor="w",
                                    font=("TkDefaultFont", 9))
        self._log_status.pack(fill="x", padx=6, pady=(0, 8))

    # ── Right panel ───────────────────────────────────────────────────────────
    def _build_right(self, parent):
        outer = tk.Frame(parent, relief="groove", bd=1)
        outer.rowconfigure(3, weight=1)
        outer.columnconfigure(0, weight=1)

        toolbar = tk.Frame(outer)
        toolbar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))

        title = tk.Label(toolbar, text="Log Viewer",
                         font=("TkDefaultFont", 9, "bold"))
        title.pack(side="left")

        self._scroll_var = tk.BooleanVar(value=True)
        autoscroll_check = tk.Checkbutton(
            toolbar, text="Auto-scroll", variable=self._scroll_var,
            command=self._on_autoscroll_toggle)
        autoscroll_check.pack(side="right")

        folder_button = tk.Button(toolbar, text="Open Folder...",
                                  command=self._open_log_folder)
        folder_button.pack(side="right", padx=(0, 4))

        clear_button = tk.Button(toolbar, text="Clear", command=self._clear_log)
        clear_button.pack(side="right", padx=(0, 4))

        divider = tk.Frame(outer, relief="groove", bd=1, height=self._px(2))
        divider.grid(row=1, column=0, sticky="ew", padx=6)

        filter_bar = tk.Frame(outer)
        filter_bar.grid(row=2, column=0, sticky="ew", padx=6, pady=4)
        filter_bar.columnconfigure(1, weight=1)

        filter_label = tk.Label(filter_bar, text="Filter:")
        filter_label.grid(row=0, column=0, padx=(0, 4))

        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", self._on_filter_change)
        filter_entry = tk.Entry(filter_bar, textvariable=self._filter_var)
        filter_entry.grid(row=0, column=1, sticky="ew")

        self._match_lbl = tk.Label(filter_bar, text="", width=14, anchor="w")
        self._match_lbl.grid(row=0, column=2, padx=(4, 0))

        log_frame = tk.Frame(outer)
        log_frame.grid(row=3, column=0, sticky="nsew", padx=6, pady=(0, 6))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        x_scroll = tk.Scrollbar(log_frame, orient="horizontal")
        y_scroll = tk.Scrollbar(log_frame, orient="vertical")
        self._log_text = tk.Text(
            log_frame, font=("Courier", 10), relief="sunken", bd=1,
            state="disabled", wrap="none",
            xscrollcommand=x_scroll.set, yscrollcommand=y_scroll.set)
        x_scroll.config(command=self._log_text.xview)
        y_scroll.config(command=self._log_text.yview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        self._log_text.grid(row=0, column=0, sticky="nsew")

        self._log_text.tag_config("record_tag", font=("Courier", 10, "bold"))
        self._log_text.tag_config("meta_tag", font=("Courier", 10, "italic"))
        self._log_text.tag_config("sys_tag", font=("Courier", 10, "bold"))
        self._log_text.tag_config("hi_tag", background="yellow",
                                  foreground="black")
        return outer

    def _on_autoscroll_toggle(self):
        self._autoscroll = self._scroll_var.get()

    # ── Connect / Disconnect ──────────────────────────────────────────────────
    def _on_connect(self):
        if self._connecting:
            return

        try:
            host = helper_validate_host(self._host_var.get())
            port = helper_validate_port(self._port_var.get())
            user = helper_validate_username(self._user_var.get(), self._limits["max_username"])
        except ValueError as exc:
            messagebox.showwarning("Invalid Input", str(exc),
                                   parent=self.winfo_toplevel())
            return

        password = self._pass_var.get()
        key_path = self._key_var.get().strip() or None

        self._cancel_reconnect()
        self._connecting = True
        self._status_lbl.config(text="Connecting…")

        def attempt(replace_key):
            self._ssh.connect(host, port, user, password, key_path=key_path,
                              replace_changed_key=replace_key)

        self._run_connect(attempt)

    def _run_connect(self, attempt, replace_key=False):
        """Run attempt(replace_key) off the UI thread and route the outcome."""
        token = self._session_token

        def worker():
            try:
                attempt(replace_key)
            except HostKeyMismatchError as exc:
                self._post(self._handle_host_key_changed, token, attempt, exc)
                return
            except Exception as exc:
                self._post(self._handle_connect_failure, token, str(exc))
                return
            self._post(self._handle_connect_success, token)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_host_key_changed(self, token, attempt, exc):
        """Same IP, different device (or reinstall): let the user trust the new key."""
        self._connecting = False
        if token != self._session_token or self._closed:
            return
        self._status_lbl.config(text="Disconnected (host key changed)")
        saved = "\n".join(exc.saved) or "(none)"
        trust = messagebox.askyesno(
            "Host Key Changed",
            f"{exc.host}:{exc.port} presented a different SSH host key than the one saved.\n\n"
            "This is expected if another device (different MAC) now uses this IP address, "
            "or the device was reinstalled. It can also mean someone is intercepting the "
            "connection.\n\n"
            f"Saved:\n{saved}\n\nPresented:\n{exc.fingerprint}\n\n"
            "Trust the new key and replace the saved one?",
            icon="warning", default="no", parent=self.winfo_toplevel())
        if not trust:
            self._push_system(f"[SYSTEM] Host key for {exc.host}:{exc.port} changed; "
                              "connection refused.\n\n")
            return
        self._connecting = True
        self._status_lbl.config(text="Connecting…")
        self._run_connect(attempt, replace_key=True)

    def _handle_connect_success(self, token):
        self._connecting = False
        if token != self._session_token or self._closed:
            self._ssh.disconnect()
            return
        self._finish_connection(was_reconnect=False)

    def _handle_connect_failure(self, token, message):
        self._connecting = False
        if token != self._session_token or self._closed:
            return
        self._status_lbl.config(text="Disconnected")
        messagebox.showerror("Connection Failed", message,
                             parent=self.winfo_toplevel())

    def _on_disconnect(self):
        self._session_token += 1
        self._connecting = False
        self._cancel_reconnect()
        self._stop_runners()
        self._stop_watchdog()
        self._ssh.disconnect()
        self._status_lbl.config(text="Disconnected")
        self._tab_label.set(f"Connection {self._tab_number}")
        self._recon_status_lbl.config(text="")

    def _finish_connection(self, was_reconnect):
        host = self._ssh.host
        self._status_lbl.config(text=f"Connected: {host}")
        self._tab_label.set(host)
        self._start_watchdog()

        attempts_used = self._reconnect_attempts
        self._reconnecting = False
        self._reconnect_attempts = 0

        if was_reconnect:
            self._recon_status_lbl.config(
                text=f"Reconnected after {attempts_used} attempt(s).")
        else:
            self._recon_status_lbl.config(text="")

        if self._ssh.host_key_was_new:
            self._push_system(
                f"[SYSTEM] New host key trusted and saved for {host}: "
                f"{self._ssh.last_fingerprint}\n\n")
        elif self._ssh.host_key_was_replaced:
            self._push_system(
                f"[SYSTEM] Host key for {host} replaced with: "
                f"{self._ssh.last_fingerprint}\n\n")

        if self._logging_active and self._logger:
            self._restart_runners()
            self._stats.add_reconnect()
            self._push_system(f"[SYSTEM] Reconnected to {host}.\n\n")

    # ── Reconnect cycle ───────────────────────────────────────────────────────
    def _max_reconnect(self):
        try:
            return max(1, int(self._max_recon_var.get()))
        except (TypeError, ValueError):
            return int(self._recon_cfg["max_attempts"])

    def _recon_delay(self):
        try:
            return max(1, int(self._recon_delay_var.get()))
        except (TypeError, ValueError):
            return int(self._recon_cfg["delay_s"])

    def _begin_reconnect_cycle(self):
        if not self._autorecon_var.get() or not self._ssh.has_credentials:
            self._status_lbl.config(text="Disconnected (connection lost)")
            self._push_system("[SYSTEM] Connection lost.\n\n")
            self._stop_watchdog()
            return

        self._reconnecting = True
        self._reconnect_attempts = 0
        self._push_system("[SYSTEM] Connection lost — starting auto-reconnect…\n\n")
        self._schedule_next_attempt()

    def _schedule_next_attempt(self):
        self._reconnect_attempts += 1
        max_attempts = self._max_reconnect()

        if self._reconnect_attempts > max_attempts:
            self._reconnecting = False
            message = f"Auto-reconnect failed after {max_attempts} attempt(s)."
            self._recon_status_lbl.config(text=message)
            self._push_system(f"[SYSTEM] {message}\n\n")
            self._status_lbl.config(text="Disconnected")
            self._tab_label.set(f"Connection {self._tab_number}")
            self._stop_watchdog()   # do not start endless new cycles
            return

        delay = self._recon_delay()
        self._recon_status_lbl.config(
            text=f"Reconnecting… attempt {self._reconnect_attempts}/"
                 f"{max_attempts} (in {delay}s)")
        self._reconnect_after_id = self.after(delay * 1000, self._try_reconnect)

    def _try_reconnect(self):
        self._reconnect_after_id = None
        if not self._reconnecting or self._closed:
            return

        self._status_lbl.config(text="Reconnecting…")
        max_attempts = self._max_reconnect()
        self._recon_status_lbl.config(
            text=f"Reconnecting… attempt {self._reconnect_attempts}/{max_attempts}")
        token = self._session_token

        def worker():
            try:
                self._ssh.reconnect()
            except HostKeyMismatchError as exc:
                self._post(self._handle_reconnect_key_changed, token, exc)
                return
            except Exception as exc:
                self._post(self._handle_reconnect_failure, token, str(exc))
                return
            self._post(self._handle_reconnect_success, token)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_reconnect_success(self, token):
        if token != self._session_token or self._closed:
            self._ssh.disconnect()
            return
        if not self._reconnecting:
            return   # superseded by a manual connect
        self._finish_connection(was_reconnect=True)

    def _handle_reconnect_key_changed(self, token, exc):
        if token != self._session_token or self._closed or not self._reconnecting:
            return
        # Retrying cannot fix a changed key, and it must never be trusted silently.
        self._cancel_reconnect()
        self._stop_watchdog()
        self._recon_status_lbl.config(text="Auto-reconnect stopped: host key changed.")
        self._push_system(f"[SYSTEM] Reconnect aborted: {exc.host}:{exc.port} "
                          "presented a different host key.\n\n")
        self._handle_host_key_changed(
            token, lambda replace_key: self._ssh.reconnect(replace_changed_key=replace_key), exc)

    def _handle_reconnect_failure(self, token, message):
        if token != self._session_token or self._closed:
            return
        if not self._reconnecting:
            return
        self._push_system(
            f"[SYSTEM] Reconnect attempt {self._reconnect_attempts} "
            f"failed: {message}\n\n")
        self._schedule_next_attempt()

    def _cancel_reconnect(self):
        self._reconnecting = False
        if self._reconnect_after_id is not None:
            try:
                self.after_cancel(self._reconnect_after_id)
            except (tk.TclError, ValueError):
                pass
            self._reconnect_after_id = None

    # ── Watchdog ──────────────────────────────────────────────────────────────
    def _start_watchdog(self):
        self._stop_watchdog()
        self._watchdog_after_id = self.after(int(self._recon_cfg["watchdog_ms"]),
                                             self._watchdog_tick)

    def _stop_watchdog(self):
        if self._watchdog_after_id is None:
            return
        try:
            self.after_cancel(self._watchdog_after_id)
        except (tk.TclError, ValueError):
            pass
        self._watchdog_after_id = None

    def _watchdog_tick(self):
        self._watchdog_after_id = None
        if self._closed:
            return

        if not self._ssh.connected and not self._reconnecting:
            self._stop_runners()          # pause I/O; logging stays active
            self._begin_reconnect_cycle()

        # Reschedule only if nothing above stopped the watchdog on purpose.
        # (_begin_reconnect_cycle may have called _stop_watchdog.)
        if self._watchdog_after_id is None and not self._watchdog_stopped_by_cycle():
            self._watchdog_after_id = self.after(int(self._recon_cfg["watchdog_ms"]),
                                                 self._watchdog_tick)

    def _watchdog_stopped_by_cycle(self):
        if self._ssh.connected or self._reconnecting:
            return False
        if not self._autorecon_var.get():
            return True
        if not self._ssh.has_credentials:
            return True
        return self._reconnect_attempts > self._max_reconnect()

    # ── Runner management ─────────────────────────────────────────────────────
    def _stop_runners(self):
        for runner in self._runners:
            runner.stop()
        self._runners.clear()

    def _start_runners(self, rows):
        for row in rows:
            runner = CommandRunner(
                row.command, row.interval, row.tag,
                self._ssh, self._logger, self._event_q)
            runner.start()
            self._runners.append(runner)

    def _restart_runners(self):
        self._stop_runners()
        if not self._logger:
            return
        rows = []
        for row in self._rows:
            if row.command and row.enabled:
                rows.append(row)
        self._start_runners(rows)

    @property
    def has_active_session(self):
        return self._logging_active or bool(self._runners)

    # ── Logging start / continue / stop ──────────────────────────────────────
    def _start_logging_common(self, filepath, rows, preload=False):
        host = self._host_var.get().strip() or "device"

        try:
            logger = Logger(filepath, host)
        except OSError as exc:
            messagebox.showerror("Log File Error",
                                 f"Cannot open the log file:\n{exc}",
                                 parent=self.winfo_toplevel())
            return

        self._log_filepath = filepath
        self._logger = logger
        self._logging_active = True
        self._start_btn.config(state="disabled")
        self._continue_btn.config(state="disabled")

        def finish_start():
            if self._logger is not logger:
                return   # Stop was pressed while the tail was loading
            self._start_runners(rows)
            short_name = os.path.basename(filepath)
            self._log_file_lbl.config(text=f"→ {short_name}")
            verb = "Continuing" if preload else "Logging"
            self._log_status.config(text=f"{verb} to {short_name}")
            self._stats.start(len(rows))

        if not preload:
            finish_start()
            return

        # Files here can be 5M-100M lines, so only the last preload_lines
        # lines are read, via a backward chunked seek, off the UI thread.
        self._log_status.config(text="Reading tail of previous log…")

        def tail_worker():
            try:
                lines, file_size = helper_tail_read_lines(
                    filepath, int(self._viewer["preload_lines"]),
                    int(self._viewer["tail_chunk_bytes"]))
            except Exception as exc:
                self._post(self._push_system,
                           f"[SYSTEM] Could not preload prior log: {exc}\n\n")
                self._post(finish_start)
                return
            self._post(apply_tail, lines, file_size)

        def apply_tail(lines, file_size):
            tail_text = "\n".join(lines)
            if lines:
                tail_text += "\n"
            if tail_text.strip():
                self._remember_block(tail_text)
                if self._passes_filter(tail_text):
                    self._append_to_viewer(tail_text)
            short_name = os.path.basename(filepath)
            self._push_system(
                f"[SYSTEM] Continuing from previous log ({short_name}) — "
                f"showing last {len(lines)} lines of {file_size} bytes on disk.\n\n")
            finish_start()

        threading.Thread(target=tail_worker, daemon=True).start()

    def _on_start_logging(self):
        if not self._ssh.connected:
            messagebox.showwarning("Not Connected",
                                   "Connect to a device before logging.",
                                   parent=self.winfo_toplevel())
            return

        rows = self._collect_enabled_rows()
        if rows is None:
            return

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        host_label = re.sub(r"[^A-Za-z0-9_-]+", "-", self._host_var.get().strip()).strip("-") or "device"
        filepath = filedialog.asksaveasfilename(
            title="Choose Log File",
            initialfile=f"diag_{host_label}_{timestamp}.log",
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("All files", "*.*")],
            parent=self.winfo_toplevel(),
            initialdir=core.helper_dialog_start(self._cfg["paths"]["log_file"]).get("initialdir"))
        if not filepath:
            return

        self._remember("log_file", filepath)
        self._start_logging_common(filepath, rows, preload=False)

    def _on_continue_logging(self):
        if not self._ssh.connected:
            messagebox.showwarning("Not Connected",
                                   "Connect to a device before logging.",
                                   parent=self.winfo_toplevel())
            return

        rows = self._collect_enabled_rows()
        if rows is None:
            return

        filepath = filedialog.askopenfilename(
            title="Select Existing Log File to Continue",
            filetypes=[("Log files", "*.log"), ("All files", "*.*")],
            parent=self.winfo_toplevel(),
            **core.helper_dialog_start(self._cfg["paths"]["log_file"]))
        if not filepath:
            return
        self._remember("log_file", filepath)
        if not os.path.isfile(filepath):
            messagebox.showerror("Not Found", "Selected log file does not exist.",
                                 parent=self.winfo_toplevel())
            return

        self._start_logging_common(filepath, rows, preload=True)

    def _on_stop_logging(self):
        self._stop_runners()
        self._logging_active = False
        if self._logger:
            self._logger.close()
            self._logger = None
        self._log_status.config(text="Stopped.")
        self._log_file_lbl.config(text="")
        self._start_btn.config(state="normal")
        self._continue_btn.config(state="normal")
        self._stats.stop()

    # ── Run once ──────────────────────────────────────────────────────────────
    def _run_once(self, row):
        if not self._ssh.connected:
            messagebox.showwarning("Not Connected", "Connect to a device first.",
                                   parent=self.winfo_toplevel())
            return

        command = row.command
        if not command:
            return

        try:
            helper_validate_command(command, self._limits["max_command"])
        except ValueError as exc:
            messagebox.showwarning("Invalid Command", str(exc),
                                   parent=self.winfo_toplevel())
            return

        host = self._ssh.host

        def worker():
            try:
                combined = helper_run_command(self._ssh, command)
            except Exception as exc:
                combined = f"[ERROR] {exc}"

            block = helper_format_record(helper_now_timestamp(), host, command, combined,
                                  tag="run-once")
            self._event_q.put(("record", block))

        threading.Thread(target=worker, daemon=True).start()

    # ── Filter ────────────────────────────────────────────────────────────────
    def _passes_filter(self, block):
        needle = self._filter_text.strip().lower()
        if not needle:
            return True
        return needle in block.lower()

    def _on_filter_change(self, *_args):
        self._filter_text = self._filter_var.get()
        self._rerender_log()

    def _rerender_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

        matches = 0
        for block in self._all_blocks:
            if self._passes_filter(block):
                self._append_to_viewer(block, scroll=False)
                matches += 1

        if self._filter_text.strip():
            suffix = "" if matches == 1 else "es"
            self._match_lbl.config(text=f"{matches} match{suffix}")
        else:
            self._match_lbl.config(text="")

        if self._autoscroll:
            self._log_text.see("end")

    def _append_to_viewer(self, block, scroll=True):
        max_chars = int(self._viewer["max_block_chars"])
        if len(block) > max_chars:
            block = (block[:max_chars]
                     + "\n[... truncated in viewer; see log file ...]\n")

        needle = self._filter_text.strip().lower()
        self._log_text.config(state="normal")

        for line in block.splitlines(keepends=True):
            stripped = line.rstrip("\n")
            if stripped in ("[record]", "[/record]"):
                self._log_text.insert("end", line, "record_tag")
            elif stripped.startswith(("time=", "host=", "cmd=", "tag=")):
                self._log_text.insert("end", line, "meta_tag")
            elif stripped.startswith(("[SYSTEM]", "[WATCHDOG]")):
                self._log_text.insert("end", line, "sys_tag")
            elif needle and needle in stripped.lower():
                line_start = self._log_text.index("end-1c")
                self._log_text.insert("end", line)
                self._highlight_matches(line_start, needle)
            else:
                self._log_text.insert("end", line)

        if scroll and self._autoscroll:
            self._log_text.see("end")
        self._log_text.config(state="disabled")
        self._trim_viewer()

    def _highlight_matches(self, start_index, needle):
        search_from = start_index
        while True:
            found = self._log_text.search(needle, search_from, nocase=True,
                                          stopindex="end-1c")
            if not found:
                break
            match_end = f"{found}+{len(needle)}c"
            self._log_text.tag_add("hi_tag", found, match_end)
            search_from = match_end

    def _trim_viewer(self):
        line_count = int(self._log_text.index("end-1c").split(".")[0])
        if line_count <= int(self._viewer["max_view_lines"]):
            return
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", f"{int(self._viewer['trim_lines']) + 1}.0")
        self._log_text.config(state="disabled")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _push_system(self, message):
        self._remember_block(message)
        if self._passes_filter(message):
            self._append_to_viewer(message)

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")
        self._all_blocks.clear()

    def _open_log_folder(self):
        if not self._log_filepath:
            return
        folder = os.path.dirname(self._log_filepath)
        try:
            helper_open_folder(folder)
        except OSError as exc:
            messagebox.showerror("Open Folder Failed", str(exc),
                                 parent=self.winfo_toplevel())

    # ── Teardown ──────────────────────────────────────────────────────────────
    def teardown(self):
        self._closed = True
        self._session_token += 1
        self._cancel_reconnect()
        self._stop_watchdog()

        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except (tk.TclError, ValueError):
                pass
            self._poll_after_id = None

        self._on_stop_logging()
        self._ssh.disconnect()


# ═══════════════════════════════════════════════════════════════════════════════
# App
# ═══════════════════════════════════════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self, cfg, app_version=""):
        super().__init__()
        gui = cfg["gui"]
        self.title(f'{gui["title"]} v{app_version}' if app_version else gui["title"])
        scale = core.helper_dpi_scale(self)
        self.geometry(core.helper_scale_geometry(gui["window_geometry"], scale))
        self.minsize(core.helper_scale_px(gui["min_width"], scale),
                     core.helper_scale_px(gui["min_height"], scale))

        self._cfg = cfg
        self._profiles = ProfileStore(core.helper_service_file("Service_profiles", "profiles.json"),
                                      cfg["limits"]["max_profile_name"])
        self._known_hosts = KnownHostsStore(core.helper_service_file("Service_profiles", "known_hosts"))
        self._panes = []
        self._next_tab_number = 1

        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True)

        self._build_menubar()
        self._add_connection()
        self.protocol("WM_DELETE_WINDOW", self._on_quit)

    def _build_menubar(self):
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        connections_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Connections", menu=connections_menu)
        connections_menu.add_command(label="Add Connection", command=self._add_connection,
                                     accelerator="Ctrl+T")
        connections_menu.add_command(label="Remove Current", command=self._remove_current,
                                     accelerator="Ctrl+W")
        connections_menu.add_separator()
        connections_menu.add_command(label="Quit", command=self._on_quit, accelerator="Ctrl+Q")

        self.bind_all("<Control-t>", lambda _event: self._add_connection())
        self.bind_all("<Control-w>", lambda _event: self._remove_current())
        self.bind_all("<Control-q>", lambda _event: self._on_quit())

    def _add_connection(self):
        number = self._next_tab_number
        self._next_tab_number += 1

        label = tk.StringVar(value=f"Connection {number}")
        pane = ConnectionPane(self._notebook, self._cfg, number, self._profiles,
                              self._known_hosts, label)
        self._notebook.add(pane, text="")
        tab_id = self._notebook.tabs()[-1]

        def sync_tab_text(*_args):
            try:
                self._notebook.tab(tab_id, text=f"  {label.get()}  ")
            except tk.TclError:
                pass

        label.trace_add("write", sync_tab_text)
        sync_tab_text()

        self._panes.append(pane)
        self._notebook.select(tab_id)

    def _remove_current(self):
        if len(self._panes) == 1:
            messagebox.showinfo("Cannot Remove", "At least one connection tab must remain.",
                                parent=self)
            return

        index = self._notebook.index(self._notebook.select())
        pane = self._panes[index]

        if pane.has_active_session:
            if not messagebox.askyesno("Active Session",
                                       "This tab is actively logging. Stop and close?",
                                       parent=self):
                return

        pane.teardown()
        self._notebook.forget(index)
        pane.destroy()
        self._panes.pop(index)

    def _on_quit(self):
        try:
            for pane in self._panes:
                try:
                    pane.teardown()
                except Exception:
                    traceback.print_exc()
        finally:
            self.destroy()
