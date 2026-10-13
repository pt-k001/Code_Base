#!/usr/bin/env python3
"""
iperf_manager.py - runs iperf3 tests under orchestrator control.

Single-file manager: state, helpers and the iperf3 runner all live here. The
only other file needed is core/core.py (folders, config, log, TCP). Works on
Windows and Linux; standard library only.

Usage:
    python iperf_manager.py
    Set the iperf3 arguments once with PERF SET CMD, then PERF START.

Commands (TCP or local prompt):
    PERF START                        run the stored cmd
    PERF STOP                         stop the running test
    PERF STATUS                       state + live Mbps + last result
    PERF SET CMD <full iperf3 args>   store a new cmd
    PERF SET CMD CLEAR                clear the cmd
    help | exit                       local prompt only

Configuration (iperf_manager_config.toml):
    iperf3_path   path to the iperf3 binary (auto-derived; override if needed)
    cmd           raw iperf3 argument string; must be set before PERF START
    tcp_host      address this manager listens on (default 0.0.0.0)
    tcp_port      port this manager listens on (default 9001)

States:
    manager_state: IDLE | RUNNING | STOPPING

    last_result.state (set when a test finishes):
        SUCCESS         traffic flowed
        ZERO_TRAFFIC    rc == 0 but no traffic reported
        SERVER_STOPPED  unable to connect / refused / timeout
        SERVER_BUSY     server busy / too many connections
        ERROR           binary missing, unparseable output, mid-run error
        STOPPED         stopped by the orchestrator

    All rates are in Mbps.

Files:
    bin/iperf3[.exe]    iperf3 binary (plus the Cygwin DLLs on Windows)
    Service/Service_configs/iperf_manager/iperf_manager_config.toml
    Service/Service_logs/iperf_manager/iperf_manager_<timestamp>.log

Example:
    echo "PERF STATUS" | ncat 127.0.0.1 9001

Requirements:
    Python 3.11+. Needs core/core.py and an iperf3 binary.
"""

import copy
import json
import os
import shlex
import subprocess
import sys
import threading

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import core  # noqa: E402

APP = "iperf_manager"
APP_Version = core.get_version()

_BINARY_NAME = "iperf3.exe" if sys.platform == "win32" else "iperf3"

MAX_CMD_LEN     = 4096
_WAIT_TIMEOUT_S = 5      # grace after terminate() before kill()
_KILL_TIMEOUT_S = 5      # grace after kill() before giving up

DEFAULTS = {
    "iperf3_path": "",      # derived at startup; override here if needed
    "cmd":         "",
    "tcp_host":    "0.0.0.0",
    "tcp_port":    9001,
}

# ──────────────────────────────────────────────────────────────── state ───
# Always access under state_lock.
state_lock     = threading.Lock()
manager_state  = "IDLE"     # IDLE | RUNNING | STOPPING
current_mbps   = 0.0        # latest interval speed while RUNNING
current_cmd    = ""         # exact argv string of the running / last test
last_result    = {"state": "NONE", "mbps": 0.0, "message": ""}
proc           = None       # active subprocess.Popen, or None
stop_requested = False      # set by PERF STOP

cfg      = {}
cfg_lock = threading.Lock()
running  = True

# ──────────────────────────────────────────────────────── failure text ───
_BUSY_TEXT    = ["server is busy", "too many connections"]
_STOPPED_TEXT = ["unable to connect", "connection refused", "timeout",
                 "timed out", "actively refused", "no route to host"]


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def log(text: str, console: str = None):
    try:
        core.logmsg(APP, text, console=console)
    except Exception:
        pass            # logging must never take the manager down


def _derive_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "bin", _BINARY_NAME)


def _mbps(bps) -> float:
    try:
        return float(bps) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def classify_failure(text: str) -> str:
    low = text.lower()
    if any(w in low for w in _BUSY_TEXT):
        return "SERVER_BUSY"
    if any(w in low for w in _STOPPED_TEXT):
        return "SERVER_STOPPED"
    return "ERROR"


def build_argv(raw: str, iperf3_path: str) -> list:
    tokens = shlex.split(raw)
    if tokens:
        head = tokens[0].lower()
        if head in ("iperf3", "iperf3.exe") or tokens[0] == iperf3_path:
            tokens = tokens[1:]
    if not tokens:
        raise ValueError("no arguments")
    return [iperf3_path] + tokens


def _validate_cmd(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty command")
    if len(raw) > MAX_CMD_LEN:
        raise ValueError("command too long (max %d chars)" % MAX_CMD_LEN)
    if any(c in raw for c in ("\x00", "\r", "\n")):
        raise ValueError("control characters not allowed")
    return raw


def _humanize(result: dict) -> str:
    try:
        return "<\n" + core.humanize_table(result)
    except Exception:
        return "< " + json.dumps(result, default=str)


def finish(state: str, mbps: float, message: str):
    global manager_state, last_result, proc, stop_requested, current_mbps
    with state_lock:
        last_result    = {"state": state, "mbps": round(mbps, 3),
                          "message": message}
        manager_state  = "IDLE"
        proc           = None
        stop_requested = False
        current_mbps   = 0.0
    log("RESULT %s mbps=%.2f %s" % (state, mbps, message))


# ══════════════════════════════════════════════════════════════════════════════
#  RUNNER  (iperf3 3.21 --json-stream)
#    events: start | interval | end | error
# ══════════════════════════════════════════════════════════════════════════════
def _terminate(p):
    try:
        if p.poll() is None:
            p.terminate()
    except OSError:
        pass


def _wait_bounded(p):
    try:
        p.wait(timeout=_WAIT_TIMEOUT_S)
        return
    except subprocess.TimeoutExpired:
        pass
    log("iperf3 did not exit within %ss — killing" % _WAIT_TIMEOUT_S)
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.wait(timeout=_KILL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log("iperf3 still alive after kill(); giving up (possible orphan)")


def _log_start(data):
    conn = data.get("test_start", {})
    conns = data.get("connected") or [{}]
    info = conns[0] if isinstance(conns[0], dict) else {}
    log("START  %s -> %s:%s  proto=%s streams=%s dur=%ss" % (
        info.get("local_host", "?"), info.get("remote_host", "?"),
        info.get("remote_port", "?"), conn.get("protocol", "TCP"),
        conn.get("num_streams", 1), conn.get("duration", 0)))


def _log_end(data, udp, reverse) -> float:
    sent = data.get("sum_sent", {}) or {}
    recv = data.get("sum_received", {}) or {}
    cpu  = data.get("cpu_utilization_percent", {}) or {}

    sent_m = _mbps(sent.get("bits_per_second", 0.0))
    recv_m = _mbps(recv.get("bits_per_second", 0.0))
    lines = [
        "─" * 54,
        "  RESULT",
        "  Sent     %8.2f Mbps   (%.1f MB)" % (
            sent_m, sent.get("bytes", 0) / 1_048_576.0),
        "  Received %8.2f Mbps   (%.1f MB)" % (
            recv_m, recv.get("bytes", 0) / 1_048_576.0),
    ]
    if cpu:
        lines.append("  CPU      host=%.1f%%  remote=%.1f%%" % (
            cpu.get("host_total", 0), cpu.get("remote_total", 0)))
    lines.append("─" * 54)
    log("\n".join(lines))

    if udp:
        return _mbps((data.get("sum", {}) or {}).get("bits_per_second", 0.0))
    return recv_m if reverse else sent_m


def test_runner(argv: list):
    global proc, current_mbps, current_cmd

    udp     = ("-u" in argv) or ("--udp" in argv)
    reverse = ("-R" in argv) or ("--reverse" in argv)

    cmd = [t for t in argv if t not in ("-J", "--json", "--json-stream")]
    cmd.append("--json-stream")

    with state_lock:
        current_cmd  = " ".join(cmd)
        current_mbps = 0.0
    log("START cmd=%s" % current_cmd)

    try:
        # stdin=DEVNULL: the Cygwin iperf3.exe otherwise inherits the manager's
        # console stdin and stalls until a key is pressed in that window.
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL,
                             text=True, bufsize=1)
    except FileNotFoundError:
        finish("ERROR", 0.0, "iperf3 binary not found: %s" % cmd[0])
        return
    except OSError as e:
        finish("ERROR", 0.0, "could not start iperf3: %s" % e)
        return

    with state_lock:
        proc = p
        stop_already = stop_requested
    if stop_already:                     # STOP arrived before proc was set
        _terminate(p)

    st = {"final": 0.0, "end_seen": False, "n": 0, "diag": []}

    def process_line(line):
        global current_mbps
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            log("unparseable line: %s" % line[:200])
            if len(st["diag"]) < 50:
                st["diag"].append(line)
            return None
        if not isinstance(obj, dict):
            return None

        event = obj.get("event", "")
        data  = obj.get("data", obj)
        if not isinstance(data, dict) and event != "error":
            return None

        if event == "start":
            _log_start(data)
        elif event == "interval":
            st["n"] += 1
            s = data.get("sum", {}) or {}
            m = _mbps(s.get("bits_per_second", 0.0))
            log("  [%2d]  %4.2f-%4.2f s   %8.2f Mbps" % (
                st["n"], s.get("start", 0.0), s.get("end", 0.0), m))
            with state_lock:
                current_mbps = m
        elif event == "end":
            st["final"]    = _log_end(data, udp, reverse)
            st["end_seen"] = True
        elif event == "error":
            return str(data.get("error", data)) if isinstance(data, dict) \
                   else str(data)
        return None

    def bail(err_msg):
        _wait_bounded(p)
        finish(classify_failure(err_msg), 0.0, err_msg[:300])

    stopped_by_us = False
    while True:
        with state_lock:
            if stop_requested:
                stopped_by_us = True
                break
        raw = p.stdout.readline()
        if raw == "":
            break
        try:
            err = process_line(raw)
        except Exception as e:
            log("error processing line: %r (%s)" % (e, raw.strip()[:200]))
            _terminate(p)
            _wait_bounded(p)
            finish("ERROR", 0.0, "runner error: %s" % e)
            return
        if err is not None:
            bail(err)
            return

    if stopped_by_us:
        _terminate(p)
    _wait_bounded(p)

    try:
        leftover = p.stdout.read()
    except (OSError, ValueError):
        leftover = ""
    if leftover and not st["end_seen"]:
        for line in leftover.splitlines():
            try:
                err = process_line(line)
            except Exception as e:
                log("error processing leftover: %r" % e)
                finish("ERROR", 0.0, "runner error: %s" % e)
                return
            if err is not None:
                bail(err)
                return

    with state_lock:
        was_stopping = stop_requested

    if was_stopping:
        finish("STOPPED", 0.0, "test stopped by orchestrator")
    elif not st["end_seen"]:
        msg = (" ".join(st["diag"]) or "no end event received from iperf3")[:300]
        finish(classify_failure(msg), 0.0, msg)
    elif st["final"] <= 0:
        finish("ZERO_TRAFFIC", 0.0, "no traffic reported")
    else:
        finish("SUCCESS", st["final"], "")


def _run_test(argv):
    try:
        test_runner(argv)
    except Exception as e:
        log("runner crashed: %r" % e)
        with state_lock:
            p = proc
        if p is not None:
            _terminate(p)
        finish("ERROR", 0.0, "runner crashed: %s" % e)
    else:
        with state_lock:
            stuck = manager_state != "IDLE"
        if stuck:
            finish("ERROR", 0.0, "runner exited without result")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
def _load_cfg():
    loaded  = core.load_config(APP, DEFAULTS)
    changed = False

    if not isinstance(loaded["cmd"], str):
        loaded["cmd"] = ""
        changed = True

    try:
        port = int(loaded["tcp_port"])
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        log("invalid tcp_port %r, using default" % loaded["tcp_port"])
        port = DEFAULTS["tcp_port"]
    if loaded["tcp_port"] != port:
        loaded["tcp_port"] = port
        changed = True

    p = loaded.get("iperf3_path") or ""
    if not p or not os.path.isfile(p):
        derived = _derive_path()
        if p != derived:
            loaded["iperf3_path"] = derived
            changed = True

    if changed:
        core.save_config(APP, loaded)
    return loaded


# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLER
# ══════════════════════════════════════════════════════════════════════════════
def _handle(line: str) -> dict:
    global manager_state, current_mbps, stop_requested

    words = line.split()
    if not words:
        return core.send_response(False, "ERROR", "empty command")
    if words[0].upper() != "PERF" or len(words) < 2:
        return core.send_response(False, "ERROR", "unknown command", {
            "commands": ["PERF START", "PERF STOP", "PERF STATUS",
                         "PERF SET CMD <args>", "PERF SET CMD CLEAR"]})

    sub = words[1].upper()

    # ── STATUS ────────────────────────────────────────────────────────────────
    if sub == "STATUS":
        with cfg_lock:
            cmd_val, path_val = cfg.get("cmd", ""), cfg.get("iperf3_path", "")
        with state_lock:
            snap = {
                "manager":      manager_state,
                "current_mbps": round(float(current_mbps or 0.0), 3),
                "current_cmd":  current_cmd,
                "cmd":          cmd_val,
                "last_result":  copy.deepcopy(last_result),
                "iperf3_path":  path_val,
            }
        return core.send_response(True, snap["manager"], "status ok", snap)

    # ── START ─────────────────────────────────────────────────────────────────
    if sub == "START":
        with cfg_lock:
            cmd_val, path_val = cfg.get("cmd", ""), cfg.get("iperf3_path", "")
        if not cmd_val:
            return core.send_response(False, "ERROR",
                "no cmd set; use PERF SET CMD <full iperf3 args> first")
        if not os.path.isfile(path_val):
            return core.send_response(False, "ERROR",
                "iperf3 binary not found: %s" % path_val)
        try:
            argv = build_argv(cmd_val, path_val)
        except ValueError as e:
            return core.send_response(False, "ERROR",
                "stored cmd could not be parsed: %s" % e)

        with state_lock:                      # check-and-set atomically
            if manager_state != "IDLE":
                return core.send_response(False, manager_state,
                                          "a test is already running")
            manager_state  = "RUNNING"
            current_mbps   = 0.0
            stop_requested = False
        try:
            threading.Thread(target=_run_test, args=(argv,),
                             daemon=True).start()
        except Exception as e:
            with state_lock:
                manager_state = "IDLE"
            return core.send_response(False, "ERROR",
                                      "could not start test: %s" % e)
        return core.send_response(True, "STARTED", "test started",
                                  {"cmd": cmd_val})

    # ── STOP ──────────────────────────────────────────────────────────────────
    if sub == "STOP":
        with state_lock:
            if manager_state == "IDLE":
                return core.send_response(False, "IDLE", "no test running")
            stop_requested = True
            manager_state  = "STOPPING"
            active = proc
        if active is not None:
            _terminate(active)               # runner escalates to kill
        log("STOP requested")
        return core.send_response(True, "STOPPING", "stop requested")

    # ── SET ───────────────────────────────────────────────────────────────────
    if sub == "SET":
        if len(words) < 3:
            return core.send_response(False, "ERROR",
                "usage: PERF SET CMD <args> | PERF SET CMD CLEAR")
        if words[2].upper() != "CMD":
            return core.send_response(False, "ERROR",
                "unknown SET param %r; use CMD" % words[2])

        parts = line.strip().split(None, 3)   # keep original spacing/quotes
        rest  = parts[3].strip() if len(parts) > 3 else ""

        with state_lock:
            busy = manager_state != "IDLE"
        if busy:
            return core.send_response(False, manager_state,
                "cannot change cmd while a test is running")

        if not rest or rest.upper() == "CLEAR":
            with cfg_lock:
                cfg["cmd"] = ""
                core.save_config(APP, cfg)
            return core.send_response(True, "OK", "cmd cleared", {"cmd": ""})

        try:
            rest = _validate_cmd(rest)
            with cfg_lock:
                path_val = cfg["iperf3_path"]
            build_argv(rest, path_val)        # parse check
        except ValueError as e:
            return core.send_response(False, "ERROR",
                "could not parse cmd (check quoting): %s" % e)
        with cfg_lock:
            cfg["cmd"] = rest
            core.save_config(APP, cfg)
        return core.send_response(True, "OK", "cmd set", {"cmd": rest})

    return core.send_response(False, "ERROR",
                              "unknown PERF command %r" % words[1])


def handle_command(line: str) -> dict:
    try:
        if not isinstance(line, str):
            return core.send_response(False, "ERROR", "invalid input")
        return _handle(line[:MAX_CMD_LEN + 64])
    except Exception as e:
        log("handler error: %r" % e)
        return core.send_response(False, "ERROR", "internal error: %s" % e)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    global cfg, running, stop_requested

    core.setup_env(APP)
    core.start_run_log(APP)
    cfg = _load_cfg()

    log("=== Iperf start Version=%s  path=%s  ===" % ( APP_Version, cfg["iperf3_path"]))
    if not os.path.isfile(cfg["iperf3_path"]):
        msg = "WARNING: iperf3 binary not found at %s" % cfg["iperf3_path"]
        log(msg, console=msg)

    threading.Thread(
        target=core.run_tcp_server,
        args=(APP, cfg["tcp_host"], cfg["tcp_port"],
              handle_command, lambda: running),
        daemon=True,
    ).start()

    try:
        while True:
            core.set_active_prompt("perf> ")
            try:
                line = input("perf> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            finally:
                core.clear_active_prompt()
            if not line:
                continue
            if line.lower() in ("exit", "quit"):
                break
            if line.lower() == "help":
                print("PERF START\n"
                      "PERF STOP\n"
                      "PERF STATUS\n"
                      "PERF SET CMD <full iperf3 args>\n"
                      "PERF SET CMD CLEAR\n"
                      "exit")
                continue

            log("> " + line)
            result = handle_command(line)
            log("< " + json.dumps(result, default=str),
                console=_humanize(result))
    finally:
        running = False
        with state_lock:
            stop_requested = True
            active = proc
        if active is not None:
            _terminate(active)
        log("=== session end ===")


if __name__ == "__main__":
    main()