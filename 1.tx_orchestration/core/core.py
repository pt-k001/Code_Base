#!/usr/bin/env python3
"""
core.py - shared infrastructure for every manager app.

Provides folder setup, TOML config/profile storage, run logging, the JSON
TCP command protocol, and small timing helpers. Imported by ssh_manager,
iperf_manager, capture_manager, mediator and jump_tunnel as
"import core.core as core".

Directory layout (created by setup_env, relative to the repository root,
which is the parent of the core/ folder):
    Service/Service_configs/<app>/<app>_config.toml
    Service/Service_logs/<app>/<app>_<timestamp>.log
    Service/Service_profiles/<app>/<app>_profiles.toml

    One sub-folder per app.

Main functions:
    setup_env(app)                    create folders, register the app
    get_version()                     repository version (version.toml)
    load_config / save_config         per-app TOML config (merged with defaults)
    load_profiles / save_profiles     per-app TOML profiles
    start_run_log / logmsg            one log file per process start
    run_tcp_server                    line-based TCP server, one command per line
    send_response                     build the JSON reply dict
    tcp_command                       client side: send a line, get the reply
    wait_until / sleep_ticks          interruptible waiting helpers
    set_active_prompt /
    clear_active_prompt               keep the console input() prompt intact
                                      while background threads print

TCP reply format:
    JSON object with the keys built by send_response(ok, status, message, data).

TOML writer:
    Python 3.11+ ships tomllib (read-only), which is used for reading.
    Writing uses a small built-in serialiser, so no third-party packages are
    needed. It handles str, int, float, bool, lists of scalars and dicts
    (a dict becomes a [section]; deeper nesting is written as inline tables).
    Keys are not quoted, so keys must be plain identifiers.

Requirements:
    Python 3.11+ (tomllib). Standard library only.
"""

import json
import os
import socket
import sys
import threading
import time
import tomllib
from datetime import datetime

# ─────────────────────────────────────────────────────────────────── state ───
_ENV      = {}          # registered by setup_env(); keyed by app_name
_log_lock = threading.Lock()

# Tracks the text of the local input() prompt currently on screen, if any,
# so logmsg() (called from background threads) can cleanly clear the
# prompt line before printing and redraw it afterward, instead of the
# background print and the user's in-progress typing getting interleaved
# on the same terminal line.
_active_prompt = ""


def set_active_prompt(text: str):
    global _active_prompt
    with _log_lock:
        _active_prompt = text


def clear_active_prompt():
    global _active_prompt
    with _log_lock:
        _active_prompt = ""
_log_paths: dict[str, str] = {}


# ──────────────────────────────────────────────────────── repo-root helper ───
def _repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))   # …/core
    return os.path.dirname(here)


# ══════════════════════════════════════════════════════════════════════════════
#  INLINE TOML WRITER
#  Supports: str, int, float, bool, list-of-scalars, dict (one level deep).
#  Dicts nested inside a list are written as inline tables { k = v }.
# ══════════════════════════════════════════════════════════════════════════════
def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return '"%s"' % escaped
    if isinstance(v, (list, tuple)):
        parts = []
        for item in v:
            if isinstance(item, dict):
                inner = ", ".join("%s = %s" % (k, _toml_value(u))
                                  for k, u in item.items())
                parts.append("{%s}" % inner)
            else:
                parts.append(_toml_value(item))
        return "[%s]" % ", ".join(parts)
    if isinstance(v, dict):
        inner = ", ".join("%s = %s" % (k, _toml_value(u)) for k, u in v.items())
        return "{%s}" % inner
    return '"%s"' % str(v)


def _dict_to_toml(data: dict) -> str:
    lines    = []
    deferred = []   # [section] blocks go after the scalars

    for k, v in data.items():
        if isinstance(v, dict):
            deferred.append((k, v))
        else:
            lines.append("%s = %s" % (k, _toml_value(v)))

    for k, sub in deferred:
        lines.append("")
        lines.append("[%s]" % k)
        for sk, sv in sub.items():
            lines.append("%s = %s" % (sk, _toml_value(sv)))

    return "\n".join(lines) + "\n"


# ══════════════════════════════════════════════════════════════════════════════
#  FOLDER SETUP
# ══════════════════════════════════════════════════════════════════════════════
def setup_env(app_name: str) -> dict:
    root     = _repo_root()
    cfg_dir  = os.path.join(root,"Service", "Service_configs",  app_name)
    log_dir  = os.path.join(root, "Service","Service_logs",     app_name)
    prof_dir = os.path.join(root, "Service","Service_profiles", app_name)

    for d in (cfg_dir, log_dir, prof_dir):
        os.makedirs(d, exist_ok=True)

    cfg_file  = os.path.join(cfg_dir,  "%s_config.toml"   % app_name)
    prof_file = os.path.join(prof_dir, "%s_profiles.toml" % app_name)

    env = {
        "dir":          root,
        "config_file":  cfg_file,
        "profile_file": prof_file,
        "log_dir":      log_dir,
    }
    _ENV[app_name] = env
    return env


def _require_env(app_name: str) -> dict:
    if app_name not in _ENV:
        setup_env(app_name)
    return _ENV[app_name]


def get_version() -> str:
    data = load_toml_file(os.path.join(_repo_root(), "version.toml"), None)
    version = data.get("version") if isinstance(data, dict) else None
    return str(version).strip() if version else "0.0.0"


# ══════════════════════════════════════════════════════════════════════════════
#  LOG  –  one flat file per process start, named by timestamp
# ══════════════════════════════════════════════════════════════════════════════
def start_run_log(app_name: str) -> str:
    env  = _require_env(app_name)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(env["log_dir"], "%s_%s.log" % (app_name, ts))
    with _log_lock:
        _log_paths[app_name] = path
    return path


# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════
def _logfmt_value(val) -> str:
    if isinstance(val, bool):
        s = "true" if val else "false"
    elif val is None:
        s = "-"
    else:
        s = str(val)
    if s == "" or any(c in s for c in ' \t"='):
        s = '"%s"' % s.replace('\\', '\\\\').replace('"', '\\"')
    return s


def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        if not obj:
            yield (prefix, {})
            return
        for k, v in obj.items():
            yield from _flatten(v, "%s.%s" % (prefix, k) if prefix else str(k))
    elif isinstance(obj, list):
        if not obj:
            yield (prefix, [])
            return
        for i, item in enumerate(obj):
            yield from _flatten(item, "%s.%d" % (prefix, i))
    else:
        yield (prefix, obj)


def humanize_table(obj, prefix: str = "") -> str:
    rows = [(k, _logfmt_value(v).strip('"')) for k, v in _flatten(obj, prefix)]
    if not rows:
        return ""
    width = max(len(k) for k, _ in rows)
    return "\n".join("%-*s : %s" % (width, k, v) for k, v in rows)


def console_print(text: str):
    with _log_lock:
        if _active_prompt:
            sys.stdout.write("\r" + " " * (len(_active_prompt) + 40) + "\r")
        print(text, flush=True)
        if _active_prompt:
            sys.stdout.write(_active_prompt)
            sys.stdout.flush()


def logmsg(app_name: str, text: str, console: str = None):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines         = str(text).splitlines() or [""]
    console_lines = str(console).splitlines() if console is not None else lines

    with _log_lock:
        env      = _require_env(app_name)
        log_path = _log_paths.get(app_name)
        if not log_path:
            # No log started yet — create one now.
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(env["log_dir"],
                                    "%s_%s.log" % (app_name, ts))
            _log_paths[app_name] = log_path

        with open(log_path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(stamp + "  " + line + "\n")

        pad = " " * len(stamp)
        if _active_prompt:
            # Erase whatever's on the current line (the prompt + anything
            # typed so far) before printing our message, then redraw the
            # bare prompt afterward. This can't restore partially-typed
            # characters (no raw terminal control here), but it stops our
            # background print from being spliced into the middle of the
            # user's keystrokes and leaves a fresh prompt visible.
            sys.stdout.write("\r" + " " * (len(_active_prompt) + 40) + "\r")
        for i, line in enumerate(console_lines):
            head = stamp if i == 0 else pad
            print(head + "  " + line, flush=True)
        if _active_prompt:
            sys.stdout.write(_active_prompt)
            sys.stdout.flush()


# ══════════════════════════════════════════════════════════════════════════════
#  TOML FILE HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def load_toml_file(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return default


def save_toml_file(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dict_to_toml(data))


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG  (config/<app_name>/<app_name>_config.toml)
# ══════════════════════════════════════════════════════════════════════════════
def load_config(app_name: str, defaults: dict) -> dict:
    env  = _require_env(app_name)
    path = env["config_file"]
    cfg  = dict(defaults)

    if not os.path.exists(path):
        save_config(app_name, cfg)
        logmsg(app_name, "config.toml not found – created with defaults")
        return cfg

    data = load_toml_file(path, None)
    if not isinstance(data, dict):
        logmsg(app_name, "config.toml unreadable – using defaults")
        return cfg

    for key in cfg:
        if key in data:
            cfg[key] = data[key]
    logmsg(app_name, "config loaded from config.toml")
    return cfg


def save_config(app_name: str, cfg: dict):
    env = _require_env(app_name)
    save_toml_file(env["config_file"], cfg)


# ══════════════════════════════════════════════════════════════════════════════
#  PROFILES  (profile/<app_name>/<app_name>_profiles.toml)
# ══════════════════════════════════════════════════════════════════════════════
def profiles_path(app_name: str) -> str:
    return _require_env(app_name)["profile_file"]


def load_profiles(app_name: str, default: dict) -> dict:
    data = load_toml_file(profiles_path(app_name), None)
    return data if isinstance(data, dict) else default


def save_profiles(app_name: str, data: dict):
    save_toml_file(profiles_path(app_name), data)


# ══════════════════════════════════════════════════════════════════════════════
#  RESPONSES
# ══════════════════════════════════════════════════════════════════════════════
def send_response(ok, status, message, data=None) -> dict:
    return {
        "ok":      bool(ok),
        "status":  status,
        "message": message,
        "data":    data if data is not None else {},
    }


# ══════════════════════════════════════════════════════════════════════════════
#  TCP SERVER
# ══════════════════════════════════════════════════════════════════════════════
def _tcp_client(app_name, conn, addr, dispatch):
    peer = "%s:%s" % (addr[0], addr[1])
    try:
        f = conn.makefile("rb")
        for raw in f:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            logmsg(app_name, "TCP %s > %s" % (peer, line))
            try:
                result = dispatch(line)
            except Exception as e:
                result = send_response(False, "ERROR", "internal error: %s" % e)
            text = json.dumps(result)
            logmsg(app_name, "TCP %s < %s" % (peer, text))
            conn.sendall((text + "\n").encode())
    except OSError:
        pass
    finally:
        conn.close()


def run_tcp_server(app_name, host, port, dispatch, running_flag=None):
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, int(port)))
        srv.listen(5)
    except OSError as e:
        logmsg(app_name, "TCP server failed to start: %s" % e)
        return

    logmsg(app_name, "TCP server listening on %s:%s" % (host, port))
    while running_flag is None or running_flag():
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        threading.Thread(target=_tcp_client,
                         args=(app_name, conn, addr, dispatch),
                         daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
#  TCP CLIENTS
# ══════════════════════════════════════════════════════════════════════════════
def tcp_command(host, port, line, timeout=5) -> dict:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((line.strip() + "\n").encode())
            f   = s.makefile("rb")
            raw = f.readline()
            if not raw:
                return send_response(False, "ERROR",
                                     "empty reply from %s:%s" % (host, port))
            text = raw.decode("utf-8", "replace").strip()
            try:
                data = json.loads(text)
            except ValueError:
                return send_response(False, "ERROR",
                                     "unparseable reply: %s" % text)
            if not isinstance(data, dict):
                return send_response(False, "ERROR",
                                     "unexpected reply: %s" % text)
            return data
    except OSError as e:
        return send_response(False, "ERROR",
                             "connection to %s:%s failed: %s" % (host, port, e))


# ══════════════════════════════════════════════════════════════════════════════
#  POLL HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def wait_until(check_fn, timeout, interval=0.5, stop_flag=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_flag and stop_flag():
            return None
        result = check_fn()
        if result is not None:
            return result
        time.sleep(interval)
    return None


def sleep_ticks(seconds, stop_flag, pause_flag=None, tick_cb=None,
                tick_interval=1.0) -> bool:
    remaining = seconds
    while remaining > 0:
        if stop_flag():
            return False
        if pause_flag and pause_flag():
            time.sleep(0.2)
            continue
        if tick_cb:
            tick_cb(remaining)
        step = min(tick_interval, remaining)
        time.sleep(step)
        remaining -= step
    return True