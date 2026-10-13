"""
mediator.py - headless orchestrator for channel x txpower sweeps.

Sweeps channel and txpower combinations on the DUT and, for each combination,
runs an iperf3 test while capturing frames. It drives three managers over the
core.py JSON TCP protocol:

    ssh_manager      APPLY <radio> <chan> <txpower_dBm> / STATUS / SSHALIVE
    iperf_manager    PERF START / STOP / STATUS / SET
    capture_manager  SESSION <name> / START / STOP / NUM / STATUS / DBNAME

All infrastructure (folders, config, log, TCP) lives in core/core.py.

Usage:
    python mediator.py
    Commands can be typed at the console or sent over TCP (default port 9003).

Commands (console and TCP):
    sweep start [profile] [from <n>] db <name>
                                        run a sweep; "db <name>" is required
                                        and selects the capture database
                                        (letters, digits, _ and -, max 64);
                                        "from <n>" starts at combination n
                                        (1-based)
    sweep stop
    sweep pause | resume
    sweep confirm y|n
    sweep status
    sweep check                         check that the managers are reachable
    sweep show combos [profile]         table of index / channel / txpower with
                                        total ETA
    sweep profile list | show [name] | use <name> | new <name> | copy <src> <dst>
    sweep profile set <param> <value>
    sweep set <param> <value>
    sweep help
    manual <ssh cmd> | manual perf <cmd>

Behaviour:
    * iperf_manager is controlled only through its custom cmd. The profile
      carries a full iperf3 command; the mediator pushes it with PERF SET CMD
      before the first slot and clears it when the sweep ends.
    * iperf_manager runs as a separate process, so the mediator polls
      PERF STATUS (current_mbps) itself and logs the live speed:
          [slot 3]  12.4 Mbps
    * After each combination the remaining ETA is printed.
    * After every sweep a summary is written next to the run log as
      lastrun_<timestamp>.toml. There is no test-name prompt; a run is
      identified by its timestamp.

Files (created by core.setup_env / core.load_profiles):
    Service/Service_configs/mediator/mediator_config.toml
    Service/Service_logs/mediator/mediator_<timestamp>.log
    Service/Service_logs/mediator/lastrun_<timestamp>.toml
    Service/Service_profiles/mediator/mediator_profiles.toml

Example:
    echo sweep status | ncat 127.0.0.1 9003

Requirements:
    Python 3.11+. Needs core/core.py and running ssh_manager, iperf_manager
    and capture_manager instances (default ports 9000, 9001, 9002).
"""

import copy
import json
import os
import re
import shlex
import threading
import time
from datetime import datetime

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.core as core  # noqa: E402

APP = "mediator"
APP_Version = core.get_version()

# ─────────────────────────────────── defaults ────────────────────────────────
DEFAULTS = {
    "ssh_host":            "127.0.0.1",
    "ssh_port":            9000,
    "perf_host":           "127.0.0.1",
    "perf_port":           9001,
    "capture_host":        "127.0.0.1",
    "capture_port":        9002,
    "commit_timeout":      90,
    "slots_needed":        2,
    "perf_retries":        5,
    "perf_retry_wait":     5,
    "recommit_attempts":   2,
    "session_each_start":  False,
    "tcp_host":            "127.0.0.1",
    "tcp_port":            9003,
    # peer command timeouts (seconds)
    "ssh_timeout":         10,
    "perf_timeout":        5,
    "cap_timeout":         5,
    # ssh_manager APPLY blocks while the channel switches, settles and reads
    # back; must exceed its chan_switch_deadline + commit_settle + delays
    "apply_timeout":       60,
    # node-check timeouts (seconds)
    "check_ssh_timeout":     5,
    "check_status_timeout": 15,
    "check_cap_timeout":     3,
    # manual command timeout (seconds)
    "manual_timeout":      15,
    # perf idle-wait / cleanup polling
    "perf_cleanup_timeout":  15,
    "perf_cleanup_poll":     0.5,
    "idle_poll_interval":    0.5,
    "mbps_poll_interval":    1.0,
    # extra seconds of slack added on top of slot_duration when waiting
    # for PERF START's ack / for iperf_manager to go back IDLE
    "perf_start_buffer":   10,
    "perf_idle_buffer":    20,
    # txpower bounds enforced on profile channels/powers
    "txpower_min":         0,
    "txpower_max":         40,
}

SETTABLE = {
    "ssh_host": "str", "ssh_port": "int",
    "perf_host": "str", "perf_port": "int",
    "capture_host": "str", "capture_port": "int",
    "commit_timeout": "int",
    "slots_needed": "int", "perf_retries": "int",
    "perf_retry_wait": "int", "recommit_attempts": "int",
    "session_each_start": "bool",
    "ssh_timeout": "int", "perf_timeout": "int", "cap_timeout": "int",
    "apply_timeout": "int",
    "check_ssh_timeout": "int", "check_status_timeout": "int",
    "check_cap_timeout": "int", "manual_timeout": "int",
    "perf_cleanup_timeout": "int", "perf_cleanup_poll": "float",
    "idle_poll_interval": "float", "mbps_poll_interval": "float",
    "perf_start_buffer": "int", "perf_idle_buffer": "int",
    "txpower_min": "int", "txpower_max": "int",
}

DEFAULT_PROFILE = {
    "radio":                  "radio0",
    "channels":               [1, 6, 11],
    "powers":                 [10, 20],
    "custom_cmd":             "",        # full iperf3 args, REQUIRED before start
    "session_prefix":         "sweep",
    "confirm_channel_change": True,
    "step_delay":             2,
    "slot_duration":          20,        # seconds – used for ETA and poll timeout
}

READONLY_SSH = ("SSHALIVE", "STATUS")

# ─────────────────────────────────── globals ─────────────────────────────────
cfg      = {}
profiles = {}
running  = True
log_path = ""   # path of the current run log, set by start_run_log() in main()

state_lock      = threading.RLock()
sweep_state     = "IDLE"   # IDLE / RUNNING / PAUSED / WAIT_CONFIRM / STOPPING
paused          = False
stop_req        = False
confirm_pending = False
confirm_answer  = None
confirm_text    = ""
progress        = {}
results         = []

# Shared between sweep_thread and the Mbps poller
_current_slot_label = ""
_mbps_poll_active   = False


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def log(text: str, console: str = None):
    core.logmsg(APP, text, console=console)


def stopped() -> bool:
    return stop_req


def is_paused() -> bool:
    return paused


def set_state(s: str):
    global sweep_state
    with state_lock:
        if stop_req and s != "IDLE":
            s = "STOPPING"
        sweep_state = s


def prog(**kw):
    with state_lock:
        progress.update(kw)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return "%dh %02dm %02ds" % (h, m, s)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


# ═══════════════════════════════════════════════════════════════════════════════
#  PEER COMMUNICATION
# ═══════════════════════════════════════════════════════════════════════════════
def ssh(line, timeout=None, quiet=False):
    if timeout is None:
        timeout = cfg["ssh_timeout"]
    if not quiet:
        log("ssh  > " + line)
    r = core.tcp_command(cfg["ssh_host"], cfg["ssh_port"], line, timeout)
    if not quiet:
        log("ssh  < ok=%s %s %s" % (r.get("ok"), r.get("status"), r.get("message")))
    return r


def perf(line, timeout=None, quiet=False):
    if timeout is None:
        timeout = cfg["perf_timeout"]
    if not quiet:
        log("perf > " + line)
    r = core.tcp_command(cfg["perf_host"], cfg["perf_port"], line, timeout)
    if not quiet:
        log("perf < ok=%s %s %s" % (r.get("ok"), r.get("status"), r.get("message")))
    return r


def cap(line, timeout=None, quiet=False):
    if timeout is None:
        timeout = cfg["cap_timeout"]
    if not quiet:
        log("cap  > " + line)
    r = core.tcp_command(cfg["capture_host"], cfg["capture_port"], line, timeout)
    if not quiet:
        log("cap  < ok=%s %s %s" % (r.get("ok"), r.get("status"), r.get("message")))
    return r


def cap_ok(r):
    return bool(r.get("ok"))


def perf_data():
    r = perf("PERF STATUS", cfg["perf_timeout"], quiet=True)
    d = r.get("data")
    if r.get("ok") and isinstance(d, dict):
        return d
    return None


def perf_cleanup():
    d = perf_data()
    if d is None:
        return
    if d.get("manager") == "RUNNING":
        perf("PERF STOP")
    if d.get("manager") in ("RUNNING", "STOPPING"):
        core.wait_until(
            lambda: True if (perf_data() or {}).get("manager") == "IDLE" else None,
            cfg["perf_cleanup_timeout"], cfg["perf_cleanup_poll"])


# ═══════════════════════════════════════════════════════════════════════════════
#  LIVE MBPS POLLER  (runs in its own thread while a slot is active)
# ═══════════════════════════════════════════════════════════════════════════════
def _mbps_poll_thread():
    while _mbps_poll_active:
        d = perf_data()
        if d and d.get("manager") == "RUNNING":
            mbps = d.get("current_mbps", 0.0)
            label = _current_slot_label
            core.console_print("[%s]  %.1f Mbps" % (label, mbps))
        time.sleep(cfg["mbps_poll_interval"])


def start_mbps_poll(label: str):
    global _mbps_poll_active, _current_slot_label
    _current_slot_label = label
    _mbps_poll_active   = True
    threading.Thread(target=_mbps_poll_thread, daemon=True).start()


def stop_mbps_poll():
    global _mbps_poll_active
    _mbps_poll_active = False


# ═══════════════════════════════════════════════════════════════════════════════
#  PAUSE / CONFIRM
# ═══════════════════════════════════════════════════════════════════════════════
def wait_if_paused() -> bool:
    if paused and not stop_req:
        set_state("PAUSED")
        log("paused")
        while paused and not stop_req:
            time.sleep(0.3)
        if not stop_req:
            log("resumed")
            set_state("RUNNING")
    return not stop_req


def confirm_gate(text: str) -> bool:
    global confirm_pending, confirm_answer, confirm_text, paused
    while not stop_req:
        with state_lock:
            confirm_answer  = None
            confirm_text    = text
            confirm_pending = True
        set_state("WAIT_CONFIRM")
        log("CONFIRM? " + text)
        print("\n[CONFIRM] %s (y/n)\n%s> " % (text, APP), end="", flush=True)

        while confirm_answer is None and not stop_req:
            time.sleep(0.2)
        with state_lock:
            ans             = confirm_answer
            confirm_pending = False
        if stop_req:
            return False
        if ans == "y":
            log("confirmed")
            set_state("RUNNING")
            return True

        log("answer n -> paused (sweep resume to re-confirm)")
        paused = True
        if not wait_if_paused():
            return False
    return False


def answer_confirm(ans: str) -> bool:
    global confirm_answer
    with state_lock:
        if not confirm_pending:
            return False
        confirm_answer = "y" if ans.lower() in ("y", "yes") else "n"
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  RADIO STEP  (ssh_manager APPLY: channel + txpower, blocking, verified)
# ═══════════════════════════════════════════════════════════════════════════════
def apply_radio(radio, ch, pw) -> bool:
    prog(step="radio apply")
    # APPLY blocks on the DUT: chan_switch, poll, txpower, settle, readback.
    r = ssh("APPLY %s %s %s" % (radio, ch, pw), cfg["apply_timeout"])
    d = r.get("data") if isinstance(r.get("data"), dict) else {}

    if not r.get("ok"):
        log("apply failed: %s" % r.get("message"))
        return False

    before = d.get("before") or {}
    after  = d.get("after") or {}
    log("apply verified=%s before=ch%s/%s after=ch%s/%s" % (
        d.get("verified"),
        before.get("channel"), before.get("txpower"),
        after.get("channel"), after.get("txpower")))
    prog(rssi="n/a")
    return bool(d.get("verified"))


# ═══════════════════════════════════════════════════════════════════════════════
#  PERF STEP  (custom_cmd only)
# ═══════════════════════════════════════════════════════════════════════════════
def wait_for_idle(timeout=10) -> bool:
    return core.wait_until(
        lambda: True if (perf_data() or {}).get("manager") == "IDLE" else None,
        timeout, cfg["idle_poll_interval"]) is True


def run_slot(prof, slot_num: int, fail_count: int) -> str:
    slot_duration = int(prof.get("slot_duration", DEFAULT_PROFILE["slot_duration"]))
    prog(step="slot %d (fail streak %d)" % (slot_num, fail_count))

    label = "slot %d" % slot_num

    # Start capture first so we don't miss early traffic.
    r_cap = cap("START")
    if not cap_ok(r_cap):
        log("slot %d: capture START failed: %s" % (slot_num, r_cap.get("message")))
        return "CAPTURE_FAILED"

    start_mbps_poll(label)

    r_perf = perf("PERF START", timeout=slot_duration + cfg["perf_start_buffer"])
    if not r_perf.get("ok"):
        stop_mbps_poll()
        log("slot %d: PERF START refused: %s" % (slot_num, r_perf.get("message")))
        cap("STOP")
        return "FAILED"

    # Wait until iperf_manager goes IDLE (test complete).
    if not wait_for_idle(slot_duration + cfg["perf_idle_buffer"]):
        stop_mbps_poll()
        log("slot %d: iperf_manager did not go IDLE – forcing stop" % slot_num)
        perf("PERF STOP")
        wait_for_idle(cfg["perf_cleanup_timeout"])
        cap("STOP")
        return "FAILED"

    stop_mbps_poll()
    cap("STOP")

    if stop_req:
        return "STOPPED"

    d      = perf_data() or {}
    result = d.get("last_result", {})
    state  = result.get("state", "ERROR")
    bps    = result.get("bps", 0.0)
    log("slot %d: %s  bps=%.0f" % (slot_num, state, bps))

    return "SUCCESS" if state == "SUCCESS" else "FAILED"


def capture_phase(prof, acc: dict) -> str:
    slots_needed = int(cfg["slots_needed"])

    # Push custom_cmd to iperf_manager once per combo.
    custom_cmd = prof.get("custom_cmd", "").strip()
    if custom_cmd:
        r_cmd = perf("PERF SET CMD %s" % custom_cmd)
    else:
        r_cmd = perf("PERF SET CMD CLEAR")
    if not r_cmd.get("ok"):
        return "PERF_FAILED"

    # Send SESSION once per combo attempt (or every capture start if flag set).
    if not acc["session_sent"] or cfg["session_each_start"]:
        if not cap_ok(cap("SESSION " + acc["session"])):
            return "CAPTURE_FAILED"
        acc["session_sent"] = True

    fail_streak = 0
    slot_num    = acc["slots_attempted"]

    while acc["good_slots"] < slots_needed:
        if not wait_if_paused():
            return "STOPPED"
        if stop_req:
            return "STOPPED"

        if fail_streak > 0:
            prog(step="retry wait (fail streak %d)" % fail_streak)
            if not core.sleep_ticks(cfg["perf_retry_wait"], stopped):
                return "STOPPED"
            perf_cleanup()

        slot_num += 1
        acc["slots_attempted"] = slot_num
        prog(slots_good=acc["good_slots"], slots_total=slot_num,
             fail_streak=fail_streak)

        outcome = run_slot(prof, slot_num, fail_streak)

        if outcome == "STOPPED":
            return "STOPPED"
        if outcome == "CAPTURE_FAILED":
            return "CAPTURE_FAILED"
        if outcome == "SUCCESS":
            acc["good_slots"] += 1
            fail_streak = 0
            log("good slot %d/%d" % (acc["good_slots"], slots_needed))
            prog(slots_good=acc["good_slots"])
        else:
            fail_streak        += 1
            acc["failed_slots"] += 1
            log("failed slot (streak %d, total fails %d)" %
                (fail_streak, acc["failed_slots"]))
            if fail_streak >= int(cfg["perf_retries"]):
                log("fail streak %d reached perf_retries -> recommit" % fail_streak)
                return "PERF_FAILED"

    return "DONE"


# ═══════════════════════════════════════════════════════════════════════════════
#  ONE COMBO
# ═══════════════════════════════════════════════════════════════════════════════
def run_combo(radio, ch, pw, prof, session: str, ctx: dict):
    acc = {
        "good_slots":      0,
        "failed_slots":    0,
        "slots_attempted": 0,
        "session":         session,
        "session_sent":    False,
    }
    outcome = "FAILED_RADIO"

    for attempt in range(1, int(cfg["recommit_attempts"]) + 1):
        if not wait_if_paused():
            return "STOPPED", acc
        log("%s ch=%s pw=%s attempt %d/%d" %
            (radio, ch, pw, attempt, cfg["recommit_attempts"]))
        prog(attempt=attempt)

        if not apply_radio(radio, ch, pw):
            if stop_req:
                return "STOPPED", acc
            outcome = "FAILED_RADIO"
            continue

        if prof["confirm_channel_change"] and ctx["confirmed_ch"] != str(ch):
            prog(step="waiting confirm")
            if not confirm_gate(
                    "Channel %s on %s applied. Ready to capture?" % (ch, radio)):
                return "STOPPED", acc
            ctx["confirmed_ch"] = str(ch)

        acc["good_slots"]      = 0
        acc["failed_slots"]    = 0
        acc["slots_attempted"] = 0
        acc["session_sent"]    = False

        r = capture_phase(prof, acc)
        if r == "DONE":
            return "OK", acc
        if r == "STOPPED":
            return "STOPPED", acc
        if r == "CAPTURE_FAILED":
            return "FAILED_CAPTURE", acc
        outcome = "FAILED_PERF"
        log("perf_retries exhausted -> recommit (attempt %d)" % attempt)

    return outcome, acc


# ═══════════════════════════════════════════════════════════════════════════════
#  ETA HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _combo_eta_secs(prof) -> float:
    slot_duration = int(prof.get("slot_duration", DEFAULT_PROFILE["slot_duration"]))
    slots_needed  = int(cfg.get("slots_needed", DEFAULTS["slots_needed"]))
    step_delay    = int(prof.get("step_delay",  DEFAULT_PROFILE["step_delay"]))
    # apply ~10 s overhead + slots + step delay
    return 10.0 + slots_needed * slot_duration + step_delay


def _show_combos_table(prof, name: str = ""):
    combos = [(c, p) for c in prof["channels"] for p in prof["powers"]]
    eta_each = _combo_eta_secs(prof)
    total    = eta_each * len(combos)

    header = "Profile: %s" % name if name else "Combos"
    print("\n%s" % header)
    print("%-6s  %-8s  %-8s" % ("Index", "CH", "TX"))
    print("─" * 26)
    for i, (ch, pw) in enumerate(combos, 1):
        print("%-6d  %-8s  %-8s" % (i, ch, pw))
    print("─" * 26)
    print("Total combos : %d" % len(combos))
    print("ETA per combo: %s (approx)" % _fmt_eta(eta_each))
    print("Total ETA    : %s (approx)\n" % _fmt_eta(total))


# ═══════════════════════════════════════════════════════════════════════════════
#  SWEEP THREAD
# ═══════════════════════════════════════════════════════════════════════════════
def sweep_thread(name: str, prof: dict, start_from: int = 1, db_name: str = ""):
    global sweep_state, results

    combos    = [(c, p) for c in prof["channels"] for p in prof["powers"]]
    ctx       = {"confirmed_ch": None}
    eta_each  = _combo_eta_secs(prof)

    log("=== SWEEP START profile=%s db=%s combos=%d start_from=%d ===" %
        (name, db_name, len(combos), start_from))

    with state_lock:
        results = []
        progress.clear()
        progress.update({"profile": name, "db_name": db_name,
                         "total": len(combos),
                         "index": 0, "started": now_str(), "finished": None})
    set_state("RUNNING")

    for i, (ch, pw) in enumerate(combos, 1):
        if i < start_from:
            continue
        if not wait_if_paused():
            break

        combos_remaining = len(combos) - i + 1
        total_rem        = combos_remaining * eta_each
        print("\n[combo %d/%d]  ch=%s  tx=%s  ETA remaining: %s" %
              (i, len(combos), ch, pw, _fmt_eta(total_rem)), flush=True)

        # Session name: prefix_ch<n>_pwr<n>_   (trailing underscore is the
        # separator that capture_manager prepends to the region name when
        # storing rows, giving e.g. ch6_pwr19_Burst_Power as the logical key)
        session = "%s_ch%s_pwr%s_" % (prof["session_prefix"], ch, pw)
        prog(index=i, radio=prof["radio"], channel=ch, txpower=pw,
             session=session, slots_good=0, slots_total=0,
             fail_streak=0, step="starting")
        log("--- combo %d/%d %s ch=%s pw=%s session=%s" %
            (i, len(combos), prof["radio"], ch, pw, session))

        t0           = time.time()
        outcome, acc = run_combo(prof["radio"], ch, pw, prof, session, ctx)

        elapsed = round(time.time() - t0, 1)

        rec = {
            "index": i, "radio": prof["radio"],
            "channel": ch, "txpower": pw,
            "session": session,
            "outcome": outcome,
            "good_slots":      acc["good_slots"],
            "failed_slots":    acc["failed_slots"],
            "slots_attempted": acc["slots_attempted"],
            "elapsed_secs":    elapsed,
        }
        with state_lock:
            results.append(rec)

        combos_remaining = len(combos) - i
        total_rem        = combos_remaining * eta_each
        log("RESULT combo=%d outcome=%s elapsed=%.0fs  remaining ETA: %s" %
            (i, outcome, elapsed, _fmt_eta(total_rem)))
        print("[combo %d/%d]  outcome=%s  elapsed=%ss  → remaining ETA: %s" %
              (i, len(combos), outcome, elapsed, _fmt_eta(total_rem)), flush=True)

        if outcome == "STOPPED":
            break
        if i < len(combos) and int(prof.get("step_delay",
                                            DEFAULT_PROFILE["step_delay"])) > 0:
            prog(step="step delay")
            if not core.sleep_ticks(
                    int(prof["step_delay"]), stopped, is_paused):
                break

    # Clear the custom_cmd from iperf_manager when done
    perf("PERF SET CMD CLEAR", quiet=True)

    final = "STOPPED" if stop_req else "DONE"
    with state_lock:
        progress["finished"] = final
        progress["step"]     = "-"
        summary = {
            "profile":  name,
            "finished": final,
            "at":       now_str(),
            "results":  list(results),
        }

    log("=== SWEEP %s profile=%s ===" % (final, name))

    # ── save lastrun as TOML, next to the run log (no test name) ────────────
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    lr_path = os.path.join(os.path.dirname(log_path), "lastrun_%s.toml" % ts)
    # TOML doesn't support list-of-dicts at top level natively,
    # so we flatten results as numbered sections.
    toml_data: dict = {
        "profile":  name,
        "db_name":  db_name,
        "finished": final,
        "at":       now_str(),
    }
    for idx, rec in enumerate(summary["results"], 1):
        toml_data["result_%d" % idx] = rec
    core.save_toml_file(lr_path, toml_data)
    log("lastrun saved → %s" % lr_path)
    print("Lastrun saved: %s" % lr_path, flush=True)

    set_state("IDLE")


# ═══════════════════════════════════════════════════════════════════════════════
#  PROFILES  (TOML)
# ═══════════════════════════════════════════════════════════════════════════════
def load_profiles_local() -> dict:
    data = core.load_profiles(APP, None)
    if not isinstance(data, dict) or \
       not isinstance(data.get("profiles"), dict) or \
       not data["profiles"]:
        data = {"active": "default",
                "profiles": {"default": copy.deepcopy(DEFAULT_PROFILE)}}
        log("profiles.toml missing/invalid – created default")
    for p in data["profiles"].values():
        for k, v in DEFAULT_PROFILE.items():
            p.setdefault(k, copy.deepcopy(v))
    if data.get("active") not in data["profiles"]:
        data["active"] = next(iter(data["profiles"]))
    return data


def save_profiles_local():
    core.save_profiles(APP, profiles)


# ═══════════════════════════════════════════════════════════════════════════════
#  PROFILE VALUE PARSING
# ═══════════════════════════════════════════════════════════════════════════════
def to_bool(v: str):
    v = v.lower()
    if v in ("on", "true", "1", "yes"):
        return True, None
    if v in ("off", "false", "0", "no"):
        return False, None
    return None, "must be on/off"


def parse_profile_value(param: str, raw: list):
    joined = " ".join(raw)
    if param == "radio":
        if not re.fullmatch(r"radio\d+", joined.lower()):
            return None, "radio must look like radio0"
        return joined.lower(), None
    if param in ("channels", "powers"):
        toks = [x for x in re.split(r"[\s,]+", joined) if x]
        if not toks:
            return None, "give at least one value"
        out = []
        for tk in toks:
            if tk.isdigit():
                out.append(int(tk))
            elif param == "channels" and tk.lower() == "auto":
                return None, "auto is not supported by ssh_manager"
            else:
                return None, "bad value %r" % tk
        lo, hi = cfg["txpower_min"], cfg["txpower_max"]
        if param == "powers" and any(not lo <= x <= hi for x in out):
            return None, "powers must be %d-%d" % (lo, hi)
        return out, None
    if param in ("step_delay", "slot_duration"):
        if len(raw) != 1 or not raw[0].isdigit():
            return None, "%s must be a whole number" % param
        v   = int(raw[0])
        lim = {"step_delay": (0, 3600),
               "slot_duration": (1, 3600)}[param]
        if not lim[0] <= v <= lim[1]:
            return None, "%s must be %d-%d" % (param, lim[0], lim[1])
        return v, None
    if param == "session_prefix":
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", joined):
            return None, "prefix: letters, digits, _ and - only"
        return joined, None
    if param == "confirm":
        return to_bool(joined) if len(raw) == 1 else (None, "must be on/off")
    if param == "custom_cmd":
        if not raw or (len(raw) == 1 and raw[0].upper() == "CLEAR"):
            return "", None
        try:
            shlex.split(joined)
        except ValueError as e:
            return None, "could not parse cmd (check quoting): %s" % e
        return joined, None
    return None, "unknown profile param %r" % param


# ═══════════════════════════════════════════════════════════════════════════════
#  NODE CHECK
# ═══════════════════════════════════════════════════════════════════════════════
def check_nodes(radio=None) -> tuple:
    if radio is None:
        radio = profiles["profiles"][profiles["active"]]["radio"]
    nodes = {}

    a = ssh("SSHALIVE", cfg["check_ssh_timeout"], quiet=True)
    if not a.get("ok"):
        nodes["ssh_manager"] = {"ok": False, "detail": a.get("message")}
        nodes["dut_ssh"]     = {"ok": False, "detail": "unknown (ssh_manager down)"}
        nodes["radio"]       = {"ok": False, "detail": "unknown (ssh_manager down)"}
    else:
        nodes["ssh_manager"] = {"ok": True, "detail": "reachable"}
        alive = bool((a.get("data") or {}).get("alive"))
        nodes["dut_ssh"] = {"ok": alive,
                            "detail": "alive" if alive else "ssh link down"}
        if alive:
            s      = ssh("STATUS", cfg["check_status_timeout"], quiet=True)
            radios = (s.get("data") or {}).get("radios") or {}
            found  = radio in radios
            nodes["radio"] = {
                "ok":     found,
                "detail": "%s: %s" % (radio, radios[radio]) if found
                          else "%s not on DUT (has: %s)" % (
                              radio, ", ".join(radios) or "none")}
        else:
            nodes["radio"] = {"ok": False, "detail": "unknown (DUT ssh down)"}

    d = perf_data()
    if d is None:
        nodes["iperf_manager"] = {"ok": False, "detail": "unreachable"}
    else:
        nodes["iperf_manager"] = {"ok": d.get("manager") == "IDLE",
                                  "detail": d.get("manager")}

    # capture_manager speaks JSON so use tcp_command for a real health check.
    r  = cap("STATUS", cfg["check_cap_timeout"], quiet=True)
    up = r.get("ok", False)
    nodes["capture"] = {
        "ok":     up,
        "detail": "%s:%s %s" % (cfg["capture_host"], cfg["capture_port"],
                                "ok" if up else r.get("message", "not answering"))}

    for n, v in nodes.items():
        log("check %-14s %s  %s" %
            (n, "OK  " if v["ok"] else "FAIL", v["detail"]))
    return all(v["ok"] for v in nodes.values()), nodes


def cmd_check() -> dict:
    ok, nodes = check_nodes()
    bad = [k for k, v in nodes.items() if not v["ok"]]
    return core.send_response(ok, "OK" if ok else "FAIL",
                              "all nodes ok" if ok else "failed: " + ", ".join(bad),
                              {"nodes": nodes})


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════
def status_data() -> dict:
    with state_lock:
        return {
            "state":           sweep_state,
            "paused":          paused,
            "confirm_pending": confirm_pending,
            "confirm_text":    confirm_text if confirm_pending else "",
            "active_profile":  profiles.get("active"),
            "progress":        copy.deepcopy(progress),
            "results":         copy.deepcopy(results),
        }


def check_db_name(db_name) -> str:
    if not db_name:
        return ("db name required: sweep start [profile] [from <n>] db <name>")
    if len(db_name) > 64:
        return "db name too long (max 64 characters)"
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", db_name):
        return "db name: letters, digits, _ and - only"
    return ""


def cmd_start(t: list) -> dict:
    global paused, stop_req, sweep_state

    # Parse: sweep start [profile] [from <n>] db <name>
    start_from = 1
    profile_name = None
    db_name = None
    i = 0
    while i < len(t):
        if t[i].lower() == "db":
            if i + 1 >= len(t):
                return core.send_response(False, "ERROR",
                                          "db needs a name: sweep start ... db <name>")
            db_name = t[i + 1]
            i += 2
        elif t[i].lower() == "from" and i + 1 < len(t):
            try:
                start_from = int(t[i + 1])
                if start_from < 1:
                    return core.send_response(False, "ERROR",
                                              "from must be >= 1")
            except ValueError:
                return core.send_response(False, "ERROR",
                                          "from must be a number")
            i += 2
        else:
            if profile_name is None:
                profile_name = t[i]
            i += 1

    with state_lock:
        if sweep_state != "IDLE":
            return core.send_response(False, sweep_state,
                                      "a sweep is already active")

    err = check_db_name(db_name)
    if err:
        return core.send_response(False, "ERROR", err)

    name = profile_name if profile_name else profiles["active"]
    if name not in profiles["profiles"]:
        return core.send_response(False, "ERROR", "no such profile %r" % name)

    prof = copy.deepcopy(profiles["profiles"][name])
    if not prof["channels"] or not prof["powers"]:
        return core.send_response(False, "ERROR",
                                  "profile needs channels and powers")
    if not prof.get("custom_cmd"):
        return core.send_response(False, "ERROR",
            "profile has no custom_cmd – set it with: "
            "sweep profile set custom_cmd <full iperf3 args>")

    combos = [(c, p) for c in prof["channels"] for p in prof["powers"]]
    if start_from > len(combos):
        return core.send_response(False, "ERROR",
            "from %d exceeds total combos (%d)" % (start_from, len(combos)))

    ok, nodes = check_nodes(prof["radio"])
    if not ok:
        bad = [k for k, v in nodes.items() if not v["ok"]]
        return core.send_response(False, "ERROR",
                                  "node check failed: " + ", ".join(bad),
                                  {"nodes": nodes})

    # Point capture_manager at the requested database before anything runs.
    # Done after the node check so a failed start never changes the DB.
    r = cap("DBNAME " + db_name)
    if not cap_ok(r):
        return core.send_response(False, "ERROR",
            "capture_manager rejected DB name %r: %s" % (db_name, r.get("message")))
    got = (r.get("data") or {}).get("db_name")
    if got != db_name:
        return core.send_response(False, "ERROR",
            "capture_manager did not confirm DB name %r (got %r)" % (db_name, got))
    active_db = (r.get("data") or {}).get("active_db_name", db_name)
    log("capture DB set: %s (active: %s)" % (db_name, active_db))

    with state_lock:
        paused      = False
        stop_req    = False
        sweep_state = "RUNNING"

    threading.Thread(target=sweep_thread,
                     args=(name, prof, start_from, db_name),
                     daemon=True).start()
    return core.send_response(True, "STARTED", "sweep started",
                              {"profile": name,
                               "combos": len(combos),
                               "start_from": start_from,
                               "db_name": db_name,
                               "active_db_name": active_db})


def cmd_stop() -> dict:
    global stop_req
    with state_lock:
        if sweep_state == "IDLE":
            return core.send_response(False, "IDLE", "no sweep running")
        stop_req = True
        set_state("STOPPING")
    stop_mbps_poll()
    log("STOP requested")
    return core.send_response(True, "STOPPING", "stop requested")


def cmd_pause() -> dict:
    global paused
    with state_lock:
        if sweep_state == "IDLE":
            return core.send_response(False, "IDLE", "no sweep running")
        paused = True
    log("PAUSE requested")
    return core.send_response(True, "PAUSING",
                              "pause requested; active slot finishes first")


def cmd_resume() -> dict:
    global paused
    with state_lock:
        if not paused:
            return core.send_response(False, sweep_state, "not paused")
        paused = False
    log("RESUME requested")
    return core.send_response(True, "RUNNING", "resumed")


def cmd_set(t: list) -> dict:
    if len(t) != 2:
        return core.send_response(False, "ERROR",
                                  "usage: sweep set <param> <value>",
                                  {"params": sorted(SETTABLE)})
    param, raw = t[0].lower(), t[1]
    kind = SETTABLE.get(param)
    if kind is None:
        return core.send_response(False, "ERROR", "unknown param",
                                  {"params": sorted(SETTABLE)})
    if kind == "int":
        if not raw.isdigit():
            return core.send_response(False, "ERROR",
                                      "%s must be a whole number" % param)
        val = int(raw)
        if param.endswith("_port") and not 1 <= val <= 65535:
            return core.send_response(False, "ERROR", "port must be 1-65535")
    elif kind == "float":
        try:
            val = float(raw)
        except ValueError:
            return core.send_response(False, "ERROR", "%s must be a number" % param)
        if val < 0:
            return core.send_response(False, "ERROR", "%s must be >= 0" % param)
    elif kind == "bool":
        val, err = to_bool(raw)
        if err:
            return core.send_response(False, "ERROR", "%s %s" % (param, err))
    else:
        val = raw
    cfg[param] = val
    core.save_config(APP, cfg)
    return core.send_response(True, "OK", "%s updated" % param, {param: val})


def handle_profile(t: list) -> dict:
    global profiles
    if not t:
        return core.send_response(False, "ERROR",
            "usage: sweep profile list|show [name]|use <name>|new <name>|"
            "copy <src> <dst>|set <param> <value>")
    sub   = t[0].lower()
    plist = profiles["profiles"]

    if sub == "list":
        return core.send_response(True, "OK", "profiles",
            {"active": profiles["active"], "profiles": list(plist)})
    if sub == "show":
        name = t[1] if len(t) > 1 else profiles["active"]
        if name not in plist:
            return core.send_response(False, "ERROR", "no such profile %r" % name)
        return core.send_response(True, "OK", name, {"profile": plist[name]})
    if sub == "use":
        if len(t) != 2 or t[1] not in plist:
            return core.send_response(False, "ERROR",
                                      "usage: sweep profile use <existing name>")
        profiles["active"] = t[1]
        save_profiles_local()
        return core.send_response(True, "OK", "active profile: %s" % t[1])
    if sub == "new":
        if len(t) != 2:
            return core.send_response(False, "ERROR",
                                      "usage: sweep profile new <name>")
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", t[1]) or t[1] in plist:
            return core.send_response(False, "ERROR",
                                      "name invalid or already exists")
        plist[t[1]] = copy.deepcopy(DEFAULT_PROFILE)
        save_profiles_local()
        return core.send_response(True, "OK", "created %s" % t[1])
    if sub == "copy":
        if len(t) != 3 or t[1] not in plist:
            return core.send_response(False, "ERROR",
                                      "usage: sweep profile copy <existing> <new>")
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", t[2]) or t[2] in plist:
            return core.send_response(False, "ERROR",
                                      "new name invalid or already exists")
        plist[t[2]] = copy.deepcopy(plist[t[1]])
        save_profiles_local()
        return core.send_response(True, "OK", "created %s" % t[2])
    if sub == "set":
        if len(t) < 3:
            return core.send_response(False, "ERROR",
                                      "usage: sweep profile set <param> <value>")
        param    = t[1].lower()
        val, err = parse_profile_value(param, t[2:])
        if err:
            return core.send_response(False, "ERROR", err)
        key = "confirm_channel_change" if param == "confirm" else param
        plist[profiles["active"]][key] = val
        save_profiles_local()
        return core.send_response(True, "OK", "%s updated" % key, {key: val})
    return core.send_response(False, "ERROR",
                              "unknown profile command %r" % t[0])


def cmd_show_combos(t: list) -> dict:
    name = t[0] if t else profiles["active"]
    if name not in profiles["profiles"]:
        return core.send_response(False, "ERROR", "no such profile %r" % name)
    prof = profiles["profiles"][name]
    _show_combos_table(prof, name)
    combos = [(c, p) for c in prof["channels"] for p in prof["powers"]]
    return core.send_response(True, "OK", "combos shown",
                              {"profile": name, "count": len(combos)})


def handle_sweep(t: list) -> dict:
    if len(t) < 2:
        return core.send_response(False, "ERROR",
            "usage: sweep start|stop|pause|resume|confirm|"
            "status|check|show|profile|set|help")
    sub, rest = t[1].lower(), t[2:]

    if sub == "start":   return cmd_start(rest)
    if sub == "stop":    return cmd_stop()
    if sub == "pause":   return cmd_pause()
    if sub == "resume":  return cmd_resume()
    if sub == "status":
        return core.send_response(True, sweep_state, "status ok", status_data())
    if sub == "check":   return cmd_check()
    if sub == "confirm":
        if len(rest) != 1 or rest[0].lower() not in ("y", "yes", "n", "no"):
            return core.send_response(False, "ERROR", "usage: sweep confirm y|n")
        if not answer_confirm(rest[0]):
            return core.send_response(False, "ERROR", "no confirmation pending")
        return core.send_response(True, "OK", "answer recorded")
    if sub == "show":
        if rest and rest[0].lower() == "combos":
            return cmd_show_combos(rest[1:])
        return core.send_response(False, "ERROR", "usage: sweep show combos [profile]")
    if sub == "profile": return handle_profile(rest)
    if sub == "set":     return cmd_set(rest)
    if sub == "help":
        return core.send_response(True, "OK", "help", {"commands": [
            "sweep start [profile] [from <n>] db <name>",
            "sweep stop", "sweep pause", "sweep resume",
            "sweep confirm y|n", "sweep status", "sweep check",
            "sweep show combos [profile]",
            "sweep profile list|show|use|new|copy|set",
            "sweep set <param> <value>",
            "manual sshalive | status | apply <radio> <ch> <pwr>",
            "manual perf <PERF command>",
            "manual capture <SESSION|START|STOP|NUM|STATUS>",
        ]})
    return core.send_response(False, "ERROR",
                              "unknown sweep command %r" % t[1])


def handle_manual(t: list) -> dict:
    if not t:
        return core.send_response(False, "ERROR",
            "usage: manual <sshalive|status|apply ...> | perf <cmd> | capture <cmd>")
    first = t[0].lower()
    if first == "perf":
        target   = "perf"
        cmd      = " ".join(t)
        readonly = len(t) > 1 and t[1].upper() == "STATUS"
    elif first in ("capture", "cap"):
        target   = "capture"
        cmd      = " ".join(t[1:])
        readonly = len(t) > 1 and t[1].upper() in ("STATUS", "NUM")
    else:
        target   = "ssh"
        cmd      = " ".join(t[1:] if first == "ssh" else t)
        words    = cmd.split()
        readonly = bool(words) and words[0].upper() in READONLY_SSH

    if not cmd.strip():
        return core.send_response(False, "ERROR", "empty %s command" % target)
    if sweep_state != "IDLE" and not readonly:
        return core.send_response(False, sweep_state,
            "sweep is active; only read-only commands allowed (sweep stop first)")

    if target == "capture":
        r = cap(cmd)
        return core.send_response(r.get("ok"), r.get("status", "OK"),
                                  r.get("message", ""),
                                  {"target": target, "command": cmd, "reply": r})

    if target == "perf":
        reply = perf(cmd, cfg["manual_timeout"])
    else:
        # APPLY blocks on the DUT, so give it the longer apply timeout.
        is_apply = bool(cmd.split()) and cmd.split()[0].upper() == "APPLY"
        reply = ssh(cmd, cfg["apply_timeout"] if is_apply else cfg["manual_timeout"])
    return core.send_response(reply.get("ok"), reply.get("status", "OK"),
                              reply.get("message", ""),
                              {"target": target, "command": cmd, "reply": reply})


def handle_command(line: str) -> dict:
    t = line.split()
    if not t:
        return core.send_response(False, "ERROR", "empty command")
    head = t[0].lower()
    if head == "sweep":  return handle_sweep(t)
    if head == "manual": return handle_manual(t[1:])
    if head == "help":   return handle_sweep(["sweep", "help"])
    if head == "stopme":
        # Sent by capture_manager when every region's hash has been
        # unchanged for stopme_idle_seconds (nothing left to capture for
        # the current slot/combo). Treat it exactly like "sweep pause":
        # the active slot finishes, then the sweep waits for a resume/
        # confirm instead of burning through remaining slots for nothing.
        log("STOPME received from capture_manager -> pausing sweep")
        r = cmd_pause()
        if not r.get("ok"):
            # Not an error worth surfacing loudly — sweep may already be
            # IDLE/paused/stopping. Just report status back as-is.
            log("STOPME: pause not applied (%s: %s)" %
                (r.get("status"), r.get("message")))
        return r
    return core.send_response(False, "ERROR",
                              "commands start with 'sweep' or 'manual'")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global cfg, profiles, running, log_path

    core.setup_env(APP)
    cfg      = core.load_config(APP, DEFAULTS)
    profiles = load_profiles_local()
    save_profiles_local()
    log_path = core.start_run_log(APP)
    log("=== Mediator start Version=%s ===" % APP_Version)

    threading.Thread(
        target=core.run_tcp_server,
        args=(APP, cfg["tcp_host"], cfg["tcp_port"],
              handle_command, lambda: running),
        daemon=True,
    ).start()

    prompt = "%s> " % APP
    while True:
        core.set_active_prompt(prompt)
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            break
        finally:
            core.clear_active_prompt()
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            break
        if line.lower() in ("y", "yes", "n", "no") and confirm_pending:
            answer_confirm(line)
            continue

        log("> " + line)
        try:
            result = handle_command(line)
        except Exception as e:
            result = core.send_response(False, "ERROR", "internal error: %s" % e)
        # File: raw JSON (unchanged, wire-level record).
        # Console: simple two-column table, easier to scan than raw JSON.
        log("< " + json.dumps(result),
            console="<\n" + core.humanize_table(result))

    if sweep_state != "IDLE":
        cmd_stop()
        time.sleep(2)
    running = False
    log("=== session end ===")


if __name__ == "__main__":
    main()
