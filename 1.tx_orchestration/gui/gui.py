#!/usr/bin/env python3
"""
gui.py - minimal Tkinter launcher for interactive console scripts.

Runs any Python script that uses an input() prompt (for example
ssh_manager.py or iperf_manager.py) as a child process. The script's console
output appears in the log pane, and anything typed in the command bar is sent
to the script's stdin, as if typed in its own terminal.

Usage:
    python main.py        (or: python gui.py)

Toolbar:
    [script dropdown] [Browse] [Start/Restart] [Stop]

    The dropdown lists every *.py in the managers/ folder. Scripts run from the
    repository root with it on PYTHONPATH. Browse adds any other script.
    Up/Down in the command bar recalls recent commands (kept per script).
    Stop sends "exit" to the script, then terminates it if it does not quit.

Output (relative to the repository root):
    Service/Service_configs/gui/gui_state.json    selected script, command history
    Service/Service_logs/gui/gui_<timestamp>.log  log pane contents, per session

Requirements:
    Python 3.8+ with tkinter. Standard library only.
"""

import codecs
import glob
import json
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.core as core  # noqa: E402

ROOT = str(Path(__file__).resolve().parents[1])
MANAGERS = os.path.join(ROOT, "managers")
APP = "gui"
STATE_FILE = ""          # set by setup_output()
LOG_FILE = ""            # set by setup_output()
MAX_HISTORY = 100
MAX_LOG_LINES = 5000


APP_Version = core.get_version()


def setup_output():
    global STATE_FILE, LOG_FILE
    cfg_dir = os.path.join(ROOT, "Service", "Service_configs", APP)
    log_dir = os.path.join(ROOT, "Service", "Service_logs", APP)
    os.makedirs(cfg_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    STATE_FILE = os.path.join(cfg_dir, "%s_state.json" % APP)
    LOG_FILE = os.path.join(
        log_dir, "%s_%s.log" % (APP, datetime.now().strftime("%Y%m%d_%H%M%S")))



def append_log_file(text):
    if not LOG_FILE or not text:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def find_scripts():
    out = []
    for p in sorted(glob.glob(os.path.join(MANAGERS, "*.py"))):
        name = os.path.basename(p)
        if not name.startswith(("core", "__")):
            out.append(p)
    return out


class App:
    def __init__(self, root):
        self.root = root
        root.title(" GUI Version - %s" % APP_Version)
        root.geometry("820x560")

        self.proc = None
        self.gen = 0                      # bumps on every start; stale readers ignored
        self.state = self.load_state()

        # toolbar
        bar = tk.Frame(root)
        bar.pack(side="top", fill="x", padx=4, pady=4)
        self.script = tk.StringVar(value=self.state.get("script", ""))
        self.combo = ttk.Combobox(bar, textvariable=self.script,
                                  values=find_scripts())
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.on_select())
        tk.Button(bar, text="Browse", command=self.browse).pack(side="left", padx=2)
        self.btn_start = tk.Button(bar, text="Start", width=8, command=self.restart)
        self.btn_start.pack(side="left", padx=2)
        tk.Button(bar, text="Stop", width=6, command=self.stop).pack(side="left")

        # command bar
        self.entry = tk.Entry(root, font=("Consolas", 11))
        self.entry.pack(side="bottom", fill="x", padx=4, pady=4)

        # log
        self.log = tk.Text(root, state="disabled", wrap="none", bg="#111",
                           fg="#ddd", font=("Consolas", 10), borderwidth=0)
        sb = tk.Scrollbar(root, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_config("cmd", foreground="#6cf")
        self.log.tag_config("sys", foreground="#fc6")

        self.history = []
        self.idx = 0
        self.load_history()

        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<Up>", lambda e: self.recall(-1))
        self.entry.bind("<Down>", lambda e: self.recall(1))
        self.entry.focus_set()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        if self.script.get() and os.path.isfile(self.script.get()):
            self.restart()
        else:
            self.write("Select a script and press Start.\n", "sys")

    # ---------- state / history
    def load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self):
        self.state["script"] = self.script.get()
        self.state.setdefault("history", {})[self.script.get()] = \
            self.history[-MAX_HISTORY:]
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
        except OSError:
            pass

    def load_history(self):
        h = self.state.get("history", {}).get(self.script.get(), [])
        self.history = [x for x in h if isinstance(x, str)][-MAX_HISTORY:]
        self.idx = len(self.history)

    def recall(self, d):
        if self.history:
            self.idx = max(0, min(len(self.history), self.idx + d))
            self.entry.delete(0, "end")
            if self.idx < len(self.history):
                self.entry.insert(0, self.history[self.idx])
        return "break"

    # ---------- log
    def write(self, text, tag=None):
        append_log_file(text)
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        extra = int(self.log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log.delete("1.0", "%d.0" % (extra + 1))
        self.log.configure(state="disabled")
        self.log.see("end")

    # ---------- script selection
    def browse(self):
        p = filedialog.askopenfilename(initialdir=MANAGERS,
                                       filetypes=[("Python", "*.py")])
        if p:
            vals = list(self.combo["values"])
            if p not in vals:
                vals.append(p)
                self.combo["values"] = vals
            self.script.set(p)
            self.on_select()

    def on_select(self):
        self.save_state()
        self.load_history()
        self.restart()

    # ---------- process control
    def restart(self):
        path = self.script.get().strip()
        if not os.path.isfile(path):
            self.write("File not found: %s\n" % path, "sys")
            return
        self.stop(quiet=True)
        self.gen += 1
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        cwd = ROOT
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", path], cwd=cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
        except OSError as e:
            self.write("Could not start: %s\n" % e, "sys")
            return
        self.write("\n=== started %s ===\n" % os.path.basename(path), "sys")
        self.btn_start.config(text="Restart")
        self.save_state()
        threading.Thread(target=self.reader, args=(self.proc, self.gen),
                         daemon=True).start()

    def stop(self, quiet=False):
        p, self.proc = self.proc, None
        self.gen += 1                     # silence the old reader
        if p and p.poll() is None:
            try:
                p.stdin.write(b"exit\n")
                p.stdin.flush()
            except OSError:
                pass
            try:
                p.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
            if not quiet:
                self.write("\n=== stopped ===\n", "sys")

    def reader(self, p, gen):
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        fd = p.stdout
        while True:
            chunk = fd.read1(4096) if hasattr(fd, "read1") else fd.read(1)
            if not chunk:
                break
            text = dec.decode(chunk)
            text = re.sub(r"\r +\r", "", text).replace("\r", "")  # core.py prompt redraw
            self.root.after(0, self.on_output, text, gen)
        rc = p.wait()
        self.root.after(0, self.on_exit, rc, gen)

    def on_output(self, text, gen):
        if gen == self.gen:
            self.write(text)

    def on_exit(self, rc, gen):
        if gen == self.gen:
            self.write("\n=== process exited (rc=%s) ===\n" % rc, "sys")
            self.proc = None

    # ---------- command bar
    def on_enter(self, _):
        line = self.entry.get().strip()
        self.entry.delete(0, "end")
        if not line:
            return
        if not self.history or self.history[-1] != line:
            self.history.append(line)
            self.save_state()
        self.idx = len(self.history)
        self.write(line + "\n", "cmd")
        if not self.proc or self.proc.poll() is not None:
            self.write("(not running - press Start)\n", "sys")
            return
        try:
            self.proc.stdin.write((line + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as e:
            self.write("send failed: %s\n" % e, "sys")

    def on_close(self):
        self.save_state()
        self.stop(quiet=True)
        self.root.destroy()


def main():
    setup_output()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()