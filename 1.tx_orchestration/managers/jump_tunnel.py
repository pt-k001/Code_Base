#!/usr/bin/env python3
"""
jump_tunnel.py - GUI SSH port forward through a jump server.

Forwards 127.0.0.1:<local_port> to <target_host>:<target_port> via an SSH
jump host, so tools such as ssh_manager.py can reach a device that is only
reachable from the jump server. The tunnel reconnects to the jump host
automatically after a drop. Works on Linux and Windows.

Usage:
    python jump_tunnel.py
    Fill in the Tunnel tab and press Start. Then point the client at the
    tunnel, e.g. in ssh_manager's config:
        dut_host = "127.0.0.1"
        dut_port = <local_port>
    Keep dut_user / dut_pass as the target device's own credentials.

Configuration (jump_tunnel_config.toml; loaded at start, saved on Start):
    jump_host, jump_port, jump_user   jump server login
    jump_secret                       password, or key passphrase if key_file
                                      is set (saved only if save_secret = true)
    key_file                          private key path (optional)
    save_secret                       store jump_secret in plain text
    target_host, target_port          device the tunnel points at
    local_port                        local listening port (127.0.0.1 only)
    connect_timeout                   jump connect timeout, seconds
    keepalive_interval                SSH keepalive, seconds
    retry_interval                    delay between reconnect attempts, seconds

Files (created by core.setup_env):
    Service/Service_configs/jump_tunnel/jump_tunnel_config.toml
    Service/Service_logs/jump_tunnel/jump_tunnel_<timestamp>.log

Security:
    The tunnel binds to 127.0.0.1 only. If "Remember password" is enabled the
    secret is stored in plain text in the config file, so do not commit that
    file to version control.

Requirements:
    Python 3.8+ with tkinter, and paramiko (pip install paramiko).
    Needs core/core.py.
"""

import os
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

import paramiko

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.core as core  # noqa: E402

APP = "jump_tunnel"
APP_version = core.get_version()

DEFAULTS = {
    "jump_host": "",
    "jump_port": 22,
    "jump_user": "",
    "jump_secret": "",
    "key_file": "",
    "save_secret": False,
    "target_host": os.environ.get("DUT_HOST_REAL", "192.168.99.1"),
    "target_port": 22,
    "local_port": 2222,
    "connect_timeout": 8,
    "keepalive_interval": 5,
    "retry_interval": 3,
}

cfg = {}


def log(text: str, console: str = None):
    core.logmsg(APP, text, console=console)


class Tunnel:

    def __init__(self, p, emit, status):
        self.p, self.emit, self.status = p, emit, status
        self.stop_ev = threading.Event()
        self.srv = None
        self.client = None

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", int(self.p["local_port"])))
        srv.listen(20)
        srv.settimeout(0.5)
        self.srv = srv
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._maintain, daemon=True).start()
        self.emit("listening on 127.0.0.1:%s -> %s:%s via %s" % (
            self.p["local_port"], self.p["target_host"],
            self.p["target_port"], self.p["jump_host"]))

    def stop(self):
        self.stop_ev.set()
        for obj in (self.srv, self.client):
            try:
                obj.close()
            except Exception:
                pass
        self.status("stopped")
        self.emit("stopped")

    # -- ssh link to the jump host, retried until stopped --------------------
    def _maintain(self):
        p = self.p
        while not self.stop_ev.is_set():
            t = self.client.get_transport() if self.client else None
            if t and t.is_active():
                self.stop_ev.wait(1)
                continue
            self.status("connecting")
            try:
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                key = str(p["key_file"]).strip() or None
                secret = p["jump_secret"] or None
                c.connect(p["jump_host"], int(p["jump_port"]), p["jump_user"],
                          password=None if key else secret,
                          key_filename=key,
                          passphrase=secret if key else None,
                          timeout=float(p["connect_timeout"]),
                          look_for_keys=False,
                          allow_agent=not (key or secret))
                c.get_transport().set_keepalive(int(p["keepalive_interval"]))
                self.client = c
                self.status("up")
                self.emit("SSH ALIVE (jump host %s)" % p["jump_host"])
            except Exception as e:
                self.status("retrying")
                self.emit("SSH DEAD: jump connect failed: %s" % e)
                self.stop_ev.wait(float(p["retry_interval"]))

    # -- local side ----------------------------------------------------------
    def _accept(self):
        while not self.stop_ev.is_set():
            try:
                conn, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._pipe, args=(conn, addr),
                             daemon=True).start()

    def _pipe(self, conn, addr):
        t = self.client.get_transport() if self.client else None
        if not t or not t.is_active():
            conn.close()
            return
        try:
            ch = t.open_channel(
                "direct-tcpip",
                (self.p["target_host"], int(self.p["target_port"])), addr)
        except Exception as e:
            self.emit("open channel failed: %s" % e)
            conn.close()
            return

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(32768)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            for s in (src, dst):
                try:
                    s.close()
                except Exception:
                    pass

        threading.Thread(target=pump, args=(conn, ch), daemon=True).start()
        pump(ch, conn)


HELP_TEXT = """HOW TO USE

1. Open the Tunnel tab and fill in:
   - Jump host / port / user: the jump server you log in to first.
   - Password / key passphrase: the jump server password. If you use a key file, enter the key passphrase here instead (leave empty if the key has none). If both are empty, your ssh-agent is tried.
   - Target host / port: the device behind the jump server (the DUT, e.g. 192.168.99.1 and 22).
   - Local port: a free port on this PC, e.g. 2222.

2. Click Start. The dot shows the state:
   gray = stopped, orange = connecting or retrying, green = tunnel up.

3. Point ssh_manager at the tunnel, once, in its config file:
       dut_host = "127.0.0.1"
       dut_port = 2222
   Use the same number as Local port. Keep dut_user and dut_pass as the DUT's own credentials, not the jump server's.

4. Start ssh_manager as usual. Leave this app running while you work. Click Stop (or close the window) to end the tunnel.

NOTES
- The tunnel reconnects to the jump server automatically after a drop, and ssh_manager's heartbeat reconnects through it.
- The tunnel only listens on 127.0.0.1, so other machines cannot use it.
- Settings are saved to jump_tunnel_config.toml when you press Start. The password is saved only if "Remember password" is ticked, and then as plain text.
- The run log is in Service/Service_logs/jump_tunnel/.

TROUBLESHOOTING
- "cannot listen on port": the local port is already in use. Pick another one and update dut_port to match.
- Stays orange with "jump connect failed": check the jump host, port, user and password, and that this PC can reach the jump server.
- Green, but ssh_manager says SSH DEAD: check that Target host and port are reachable from the jump server.
- "open channel failed": the jump server may forbid port forwarding (AllowTcpForwarding no in its sshd_config).
"""


class App(tk.Tk):
    COLORS = {"stopped": "gray", "connecting": "orange", "retrying": "orange",
              "up": "green"}
    FIELDS = [("Jump host", "jump_host"), ("Jump port", "jump_port"),
              ("Jump user", "jump_user"),
              ("Password / key passphrase", "jump_secret"),
              ("Key file (optional)", "key_file"),
              ("Target host", "target_host"), ("Target port", "target_port"),
              ("Local port", "local_port")]
    INTS = ("jump_port", "target_port", "local_port")

    def __init__(self):
        super().__init__()
        self.title("SSH Jump Tunnel v%s" % APP_version)
        self.resizable(False, False)
        self.tunnel = None
        self.q = queue.Queue()

        self.vars = {}
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Tunnel")
        self.build_help(nb)
        for i, (label, key) in enumerate(self.FIELDS):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=str(cfg.get(key, "")))
            self.vars[key] = v
            ttk.Entry(f, textvariable=v, width=32,
                      show="*" if key == "jump_secret" else "").grid(
                row=i, column=1, pady=2, padx=4)
            if key == "key_file":
                ttk.Button(f, text="...", width=3,
                           command=self.pick_key).grid(row=i, column=2)

        n = len(self.FIELDS)
        self.save_secret = tk.BooleanVar(value=bool(cfg["save_secret"]))
        ttk.Checkbutton(f, text="Remember password (stored as plain text)",
                        variable=self.save_secret).grid(
            row=n, column=0, columnspan=3, sticky="w")

        bar = ttk.Frame(f)
        bar.grid(row=n + 1, column=0, columnspan=3, pady=6, sticky="we")
        self.btn = ttk.Button(bar, text="Start", command=self.toggle)
        self.btn.pack(side="left")
        self.dot = tk.Label(bar, text="\u25cf", fg="gray",
                            font=("TkDefaultFont", 14))
        self.dot.pack(side="left", padx=(10, 2))
        self.state = ttk.Label(bar, text="stopped")
        self.state.pack(side="left")

        self.logbox = tk.Text(f, height=8, width=52, state="disabled",
                              font=("TkFixedFont", 9))
        self.logbox.grid(row=n + 2, column=0, columnspan=3)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.pump_queue)

    def build_help(self, nb):
        h = ttk.Frame(nb, padding=10)
        nb.add(h, text="How to use")
        box = tk.Text(h, height=22, width=60, wrap="word",
                      font=("TkDefaultFont", 9))
        sb = ttk.Scrollbar(h, command=box.yview)
        box.config(yscrollcommand=sb.set)
        box.insert("1.0", HELP_TEXT.strip() + "\n")
        box.config(state="disabled")
        box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def pick_key(self):
        path = filedialog.askopenfilename(title="Select private key")
        if path:
            self.vars["key_file"].set(path)

    def emit(self, text):
        log(text)
        self.q.put(("log", "%s  %s\n" % (time.strftime("%H:%M:%S"), text)))

    def set_status(self, s):
        self.q.put(("status", s))

    def pump_queue(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.logbox.config(state="normal")
                    self.logbox.insert("end", val)
                    self.logbox.see("end")
                    self.logbox.config(state="disabled")
                else:
                    self.state.config(text=val)
                    self.dot.config(fg=self.COLORS.get(val, "gray"))
        except queue.Empty:
            pass
        self.after(150, self.pump_queue)

    def collect(self):
        data = dict(cfg)
        for k, v in self.vars.items():
            val = v.get() if k == "jump_secret" else v.get().strip()
            data[k] = int(val) if k in self.INTS else val
        data["save_secret"] = bool(self.save_secret.get())
        return data

    def toggle(self):
        global cfg
        if self.tunnel:
            self.tunnel.stop()
            self.tunnel = None
            self.btn.config(text="Start")
            return
        try:
            p = self.collect()
        except ValueError:
            self.emit("ports must be numbers")
            return
        if not p["jump_host"] or not p["jump_user"]:
            self.emit("jump host and user are required")
            return

        cfg = p
        to_save = dict(p)
        if not to_save["save_secret"]:
            to_save["jump_secret"] = ""
        try:
            core.save_config(APP, to_save)
        except OSError as e:
            self.emit("could not save config: %s" % e)

        t = Tunnel(p, self.emit, self.set_status)
        try:
            t.start()
        except OSError as e:
            self.emit("cannot listen on port %s: %s" % (p["local_port"], e))
            return
        self.tunnel = t
        self.btn.config(text="Stop")

    def on_close(self):
        if self.tunnel:
            self.tunnel.stop()
        log("=== session end ===")
        self.destroy()


def main():
    global cfg

    core.setup_env(APP)
    cfg = core.load_config(APP, DEFAULTS)
    core.start_run_log(APP)

    log("=== Jump tunnel start Version=%s ===" % APP_version)
    App().mainloop()


if __name__ == "__main__":
    main()