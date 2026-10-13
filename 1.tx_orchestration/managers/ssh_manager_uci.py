#!/usr/bin/env python3
"""
ssh_manager_uci.py - persistent SSH link to an OpenWrt DUT, radio control via UCI.

Drop-in replacement for ssh_manager.py (same TCP protocol, same default port),
so mediator.py and core.py need no change. Instead of hostapd_cli / iw set,
a change is applied through the normal OpenWrt config path:

    uci set wireless.<radio>.channel=<ch>
    uci set wireless.<radio>.txpower=<dBm>
    uci commit wireless
    wifi reload <radio>          (detached, only that radio)
    wait, reconnect ssh, wait for radio up, read back

Safe-by-design points:
    * One change = one commit + one reload (channel and txpower together).
      Nothing is touched when both already match (config AND live state).
    * Only the radio under test is reloaded.
    * The reload is detached on the DUT (starts 1 s after the ssh command has
      returned), so the ssh exec never hangs on a dying link.
    * The heartbeat stands down while APPLY owns the reload, so there is no
      reconnect tug-of-war.
    * Gentle reconnect: grace period, then backoff (2, 3, 5, 5 ... s) up to a
      deadline. No tight loops against a DUT that is still starting hostapd.
    * Readiness is polled (ubus network.wireless status: up and not pending),
      not guessed with a fixed sleep.
    * Dead-man's switch on the DUT: before every reload a small background
      job is armed on the DUT. If this manager never confirms success (ssh
      cannot come back, manager crashes, ...), the DUT restores the original
      channel/txpower and reloads that radio by itself after guard_seconds.
      It is disarmed on success and at the start of the next APPLY.
    * If the link is back but the result is wrong, the original values are
      restored (restore_on_fail) and an error is returned.
    * Never retries a failed APPLY on its own; it returns the error and stops.

Radios are the UCI wifi-device sections (radio0, radio1, ...) discovered from
the DUT, so no interface names are configured. The AP interface of a radio is
found at run time from ubus. The band is read from the live frequency (or the
UCI band/hwmode when the radio is down), so it works for any radio layout and
for DUTs with a single radio.

TCP commands (one per line, case-insensitive, JSON reply):
    SSHALIVE                            is the SSH link currently up
    STATUS                              every radio: uci values and live values
    APPLY <radio> <chan> <txpower_dBm>  blocking, see above

Configuration (ssh_manager_uci_config.toml):
    dut_host, dut_port, dut_user, dut_pass   DUT SSH credentials
    tcp_host, tcp_port                       TCP listener
    connect_timeout, cmd_timeout             SSH timeouts, seconds
    heartbeat_interval, heartbeat_lock_timeout
    reload_cmd                               "wifi reload {radio}"
    reload_start_delay                       DUT-side delay before reload (s)
    reconnect_grace                          wait before first reconnect (s)
    reconnect_deadline                       give up reconnecting after (s)
    ready_deadline                           radio must be up within (s)
    chan_wait_deadline, chan_poll_interval   see the new channel within (s)
    commit_settle                            settle before readback (s)
    txpower_tolerance                        dBm tolerance for verification
    txpower_min, txpower_max                 valid txpower range, dBm
    restore_on_fail                          restore original values on failure
    guard_enable, guard_seconds              DUT dead-man's switch; guard_seconds
                                             must exceed reconnect_deadline +
                                             ready_deadline + chan_wait_deadline

Mediator side: APPLY now takes much longer than with chan_switch. Raise it:
    sweep set apply_timeout 180

Files (created by core.setup_env):
    Service/Service_configs/ssh_manager_uci/ssh_manager_uci_config.toml
    Service/Service_logs/ssh_manager_uci/ssh_manager_uci_<timestamp>.log

Requirements:
    Python 3.11+ and paramiko. Needs core/core.py.
"""

import json
import os
import re
import threading
import time

import paramiko

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.core as core  # noqa: E402

APP = "ssh_manager_uci"
APP_version = core.get_version()

DEFAULTS = {
    "dut_host": os.environ.get("DUT_HOST", "192.168.99.1"),
    "dut_port": int(os.environ.get("DUT_PORT", 22)),
    "dut_user": os.environ.get("DUT_USER", "root"),
    "dut_pass": os.environ.get("DUT_PASS", "7890"),
    "tcp_host": "127.0.0.1",
    "tcp_port": 9000,
    "connect_timeout": 5,
    "cmd_timeout": 10,
    "heartbeat_interval": 3,
    "heartbeat_lock_timeout": 0.5,
    # ── recommended values (tuned for a DUT reached over wifi) ──────────────
    "reload_cmd": "wifi reload {radio}",  # reload only the radio under test
    "reload_start_delay": 1,     # DUT waits this long so the ssh exec returns first
    "reconnect_grace": 5.0,      # leave the DUT alone while hostapd restarts
    "reconnect_deadline": 60,    # give up reconnecting after this (backoff 2,3,5 s)
    "ready_deadline": 60,        # radio must be up (ubus) within this
    "chan_wait_deadline": 20,    # new channel must show in iw within this
    "chan_poll_interval": 1.0,
    "commit_settle": 3.0,        # let the driver settle before readback
    "txpower_tolerance": 0.5,
    "txpower_min": 0,
    "txpower_max": 40,
    "restore_on_fail": True,     # put old values back if the result is wrong
    "guard_enable": True,        # DUT-side dead-man's switch (strongly advised)
    "guard_seconds": 150,        # must exceed reconnect+ready+chan_wait (=140)
}

# Worst-case APPLY time, used to log the mediator's recommended apply_timeout.
def recommended_apply_timeout(c: dict) -> int:
    worst = (float(c["reload_start_delay"]) + float(c["reconnect_grace"]) +
             float(c["reconnect_deadline"]) + float(c["ready_deadline"]) +
             float(c["chan_wait_deadline"]) + float(c["commit_settle"]))
    return int(worst + 30)       # +30 s margin for uci/ubus/iw commands

# ---------------------------------------------------------------- globals ---
cfg = {}
running = True
is_ssh_alive = False
client = None

lock = threading.RLock()       # guards the ssh channel + `client`
cmd_lock = threading.Lock()    # one TCP/prompt command at a time
reloading = threading.Event()  # set while APPLY owns the link; heartbeat idles

_SAFE_NAME = re.compile(r"[A-Za-z0-9_]+")
_SAFE_VAL = re.compile(r"[A-Za-z0-9._-]+")


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def helper_log(text: str, console: str = None):
    core.logmsg(APP, text, console=console)


# --------------------------------------------------------------- ssh link ---
def helper_set_alive(v):
    global is_ssh_alive
    if v != is_ssh_alive:
        is_ssh_alive = v
        helper_log("SSH ALIVE" if v else "SSH DEAD")


def connect():
    global client
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["dut_host"], cfg["dut_port"], cfg["dut_user"], cfg["dut_pass"],
              timeout=cfg["connect_timeout"], look_for_keys=False, allow_agent=False)
    with lock:
        client = c
    helper_set_alive(True)


def _helper_exec(cmd, timeout):
    _, o, e = client.exec_command(cmd, timeout=timeout)
    out, err = o.read().decode(), e.read().decode()
    return o.channel.recv_exit_status(), out, err


def helper_run(cmd, timeout=None):
    if timeout is None:
        timeout = cfg["cmd_timeout"]
    with lock:
        if not is_ssh_alive:
            raise ConnectionError("ssh down")
        try:
            return _helper_exec(cmd, timeout)
        except Exception as ex:
            helper_set_alive(False)
            raise ConnectionError(str(ex))


def heartbeat():
    while running:
        time.sleep(cfg["heartbeat_interval"])
        if reloading.is_set():
            continue   # APPLY owns the link during a reload
        if not lock.acquire(timeout=cfg["heartbeat_lock_timeout"]):
            continue   # a command is busy
        try:
            ok = _helper_exec("true", cfg["connect_timeout"])[0] == 0
        except Exception:
            ok = False
        finally:
            lock.release()
        if reloading.is_set():
            continue
        helper_set_alive(ok)
        if not ok:
            try:
                connect()
            except Exception:
                pass


def helper_reconnect(deadline_s, grace_s=0.0) -> bool:
    """Gentle reconnect with backoff. Used only by APPLY while reloading."""
    global client
    helper_set_alive(False)
    with lock:
        try:
            if client is not None:
                client.close()
        except Exception:
            pass
    if grace_s > 0:
        time.sleep(grace_s)
    delays = [2, 3, 5]
    end = time.time() + float(deadline_s)
    n = 0
    while time.time() < end and running:
        try:
            connect()
            helper_log("reconnected (attempt %d)" % (n + 1))
            return True
        except Exception as e:
            helper_log("reconnect attempt %d failed: %s" % (n + 1, e))
        time.sleep(delays[min(n, len(delays) - 1)])
        n += 1
    return False


# ------------------------------------------------------------- iw parsing ---
_RE_WIPHY = re.compile(r"^\s*wiphy\s+(\d+)", re.M)
_RE_TYPE  = re.compile(r"^\s*type\s+(\S+)", re.M)
_RE_CHAN  = re.compile(
    r"^\s*channel\s+(\d+)\s+\((\d+)\s*MHz\)(?:,\s*width:\s*(\d+)\s*MHz)?", re.M)
_RE_TX    = re.compile(r"^\s*txpower\s+(-?[\d.]+)\s*dBm", re.M)


def helper_parse_iw_info(text: str, ifname: str) -> dict:
    snap = {"ifname": ifname, "phy": None, "type": None, "channel": None,
            "freq": None, "width": None, "txpower": None, "ok": False}
    m = _RE_WIPHY.search(text)
    if m:
        snap["phy"] = "phy%s" % m.group(1)
    m = _RE_TYPE.search(text)
    if m:
        snap["type"] = m.group(1)
    m = _RE_CHAN.search(text)
    if m:
        snap["channel"] = int(m.group(1))
        snap["freq"]    = int(m.group(2))
        if m.group(3):
            snap["width"] = int(m.group(3))
    m = _RE_TX.search(text)
    if m:
        snap["txpower"] = float(m.group(1))
    snap["ok"] = snap["channel"] is not None and snap["txpower"] is not None
    return snap


def helper_read_iface(ifname: str) -> dict:
    """Read-only: iw dev <if> info."""
    rc, out, err = helper_run("iw dev %s info" % ifname)
    if rc != 0:
        return {"ifname": ifname, "ok": False,
                "error": (err.strip() or out.strip() or "iw rc=%d" % rc)}
    return helper_parse_iw_info(out, ifname)


def helper_band_of(freq):
    if freq is None:
        return "?"
    return "2.4GHz" if freq < 3000 else "5GHz"


def helper_snap_public(s: dict) -> dict:
    out = {k: s.get(k) for k in ("ifname", "phy", "channel", "freq",
                                  "width", "txpower", "ok")}
    out["band"] = helper_band_of(s.get("freq"))
    if s.get("error"):
        out["error"] = s["error"]
    return out


# ------------------------------------------------------------ uci / ubus ---
def helper_uci_radios() -> list:
    """All wifi-device sections on the DUT (radio0, radio1, ...)."""
    rc, out, err = helper_run("uci -q show wireless")
    radios = []
    for line in out.splitlines():
        m = re.fullmatch(r"wireless\.([A-Za-z0-9_]+)=wifi-device", line.strip())
        if m:
            radios.append(m.group(1))
    return radios


def helper_uci_read(radio: str) -> dict:
    cmd = ("echo \"type=$(uci -q get wireless.%(r)s)\";"
           "echo \"channel=$(uci -q get wireless.%(r)s.channel)\";"
           "echo \"txpower=$(uci -q get wireless.%(r)s.txpower)\";"
           "echo \"band=$(uci -q get wireless.%(r)s.band)\";"
           "echo \"hwmode=$(uci -q get wireless.%(r)s.hwmode)\";"
           "echo \"disabled=$(uci -q get wireless.%(r)s.disabled)\"") % {"r": radio}
    rc, out, err = helper_run(cmd)
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        d[k.strip()] = v.strip()
    return d


def helper_ubus_status() -> dict:
    rc, out, err = helper_run("ubus call network.wireless status")
    if rc != 0:
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def helper_ap_ifname(st: dict, radio: str):
    r = st.get(radio) or {}
    for i in r.get("interfaces") or []:
        conf = i.get("config") or {}
        if conf.get("mode") == "ap" and i.get("ifname"):
            return i["ifname"]
    return None


def helper_live_read(radio: str):
    """Live iw snapshot of the radio's AP interface, or None if none is up."""
    ifname = helper_ap_ifname(helper_ubus_status(), radio)
    if not ifname:
        return None
    return helper_read_iface(ifname)


def helper_radio_band(live, uci: dict):
    """'2.4', '5' or None when unknown."""
    if live and live.get("freq"):
        return "2.4" if live["freq"] < 3000 else "5"
    band = (uci.get("band") or "").lower()
    hwmode = (uci.get("hwmode") or "").lower()
    if band == "2g" or hwmode in ("11g", "11b"):
        return "2.4"
    if band == "5g" or hwmode == "11a":
        return "5"
    return None


def helper_check_channel(ch: int, band):
    if band == "2.4" and not 1 <= ch <= 14:
        return "channel %d invalid for 2.4 GHz (1-14)" % ch
    if band == "5" and not 32 <= ch <= 196:
        return "channel %d invalid for 5 GHz" % ch
    return None


def helper_num_eq(a, b, tol=0.0) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


# ------------------------------------------------- dead-man's switch (DUT) ---
def helper_guard_file(radio: str) -> str:
    return "/tmp/.ssh_mgr_guard_%s" % radio


def helper_guard_disarm(radio: str):
    try:
        helper_run("rm -f %s" % helper_guard_file(radio))
    except Exception:
        pass


def helper_restore_cmds(radio: str, orig: dict) -> str:
    parts = []
    for key in ("channel", "txpower"):
        v = orig.get(key, "")
        if v and _SAFE_VAL.fullmatch(v):
            parts.append("uci set wireless.%s.%s=%s" % (radio, key, v))
        else:
            parts.append("uci -q delete wireless.%s.%s" % (radio, key))
    parts.append("uci commit wireless")
    return "; ".join(parts)


def helper_guard_arm(radio: str, orig: dict):
    gf = helper_guard_file(radio)
    token = str(int(time.time()))
    restore = helper_restore_cmds(radio, orig)
    reload_cmd = cfg["reload_cmd"].format(radio=radio)
    cmd = ("echo %(tok)s > %(gf)s; "
           "( sleep %(sec)d; "
           "[ \"$(cat %(gf)s 2>/dev/null)\" = \"%(tok)s\" ] && "
           "{ %(restore)s; %(reload)s; rm -f %(gf)s; } ) "
           "</dev/null >/dev/null 2>&1 &") % {
        "tok": token, "gf": gf, "sec": int(cfg["guard_seconds"]),
        "restore": restore, "reload": reload_cmd}
    rc, out, err = helper_run(cmd)
    if rc != 0:
        raise RuntimeError("could not arm DUT guard: %s" % (err.strip() or rc))
    helper_log("DUT guard armed for %s (%ss)" % (radio, cfg["guard_seconds"]))


# ------------------------------------------------------------ reload steps ---
def helper_start_reload(radio: str):
    """Detached reload, begins on the DUT after reload_start_delay seconds."""
    reload_cmd = cfg["reload_cmd"].format(radio=radio)
    cmd = "( sleep %d; %s ) </dev/null >/dev/null 2>&1 &" % (
        int(cfg["reload_start_delay"]), reload_cmd)
    helper_log("reload: %s" % reload_cmd)
    rc, out, err = helper_run(cmd)
    if rc != 0:
        raise RuntimeError("could not start reload: %s" % (err.strip() or rc))


def helper_wait_ready(radio: str, deadline_s):
    """Wait until ubus says the radio is up (not pending). Returns ifname."""
    end = time.time() + float(deadline_s)
    while time.time() < end and running:
        try:
            if not is_ssh_alive:
                if not helper_reconnect(max(1.0, end - time.time())):
                    return None
            st = helper_ubus_status()
            r = st.get(radio) or {}
            ifname = helper_ap_ifname(st, radio)
            if r.get("up") and not r.get("pending") and ifname:
                return ifname
        except Exception as e:
            helper_log("wait_ready: %s" % e)
        time.sleep(2.0)
    return None


def helper_wait_channel(radio: str, ch: int, deadline_s):
    end = time.time() + float(deadline_s)
    last = None
    while time.time() < end and running:
        try:
            s = helper_live_read(radio)
            if s is not None:
                last = s
                if s["ok"] and s["channel"] == ch:
                    return s
        except Exception as e:
            helper_log("wait_channel: %s" % e)
        time.sleep(float(cfg["chan_poll_interval"]))
    return last


def helper_restore(radio: str, orig: dict):
    """Best effort: put the original values back and reload once."""
    helper_log("restoring original values on %s: %s" % (radio, orig))
    try:
        if not is_ssh_alive and not helper_reconnect(cfg["reconnect_deadline"]):
            helper_log("restore: ssh not back, DUT guard will restore")
            return
        helper_run(helper_restore_cmds(radio, orig))
        helper_start_reload(radio)
        time.sleep(float(cfg["reconnect_grace"]))
        helper_reconnect(cfg["reconnect_deadline"])
        helper_wait_ready(radio, cfg["ready_deadline"])
    except Exception as e:
        helper_log("restore failed: %s (DUT guard remains armed)" % e)
        return
    helper_guard_disarm(radio)


# ------------------------------------------------------------------ apply ---
def apply_radio(radio: str, ch_s: str, tx_s: str):
    if not _SAFE_NAME.fullmatch(radio):
        return None, "bad radio name %r" % radio
    lo, hi = cfg["txpower_min"], cfg["txpower_max"]
    if not ch_s.isdigit():
        return None, "channel must be a number (auto is not supported)"
    if not tx_s.isdigit() or not lo <= int(tx_s) <= hi:
        return None, "txpower must be a whole dBm value %d-%d" % (lo, hi)
    ch, tx = int(ch_s), int(tx_s)
    tol = float(cfg["txpower_tolerance"])

    radios = helper_uci_radios()
    if radio not in radios:
        return None, "unknown radio %r (DUT has: %s)" % (
            radio, ", ".join(radios) or "none")

    helper_guard_disarm(radio)          # a guard from an earlier APPLY is stale

    uci = helper_uci_read(radio)
    try:
        before = helper_live_read(radio) or {"ifname": None, "ok": False,
                                            "error": "no AP interface up"}
    except ConnectionError as e:
        return None, "cannot read live state: %s" % e

    band = helper_radio_band(before if before.get("ok") else None, uci)
    err = helper_check_channel(ch, band)
    if err:
        return None, err

    uci_ok = (uci.get("channel") == str(ch) and
              helper_num_eq(uci.get("txpower"), tx))
    live_ok = bool(before.get("ok") and before["channel"] == ch and
                   helper_num_eq(before["txpower"], tx, tol))

    helper_log("apply %s: uci ch=%s tx=%s | live ch=%s tx=%s | want ch=%d tx=%d" % (
        radio, uci.get("channel"), uci.get("txpower"),
        before.get("channel"), before.get("txpower"), ch, tx))

    if uci_ok and live_ok:
        helper_log("already applied - skipping uci/reload")
        after = before
        return {
            "radio": radio, "iface": after.get("ifname"),
            "requested": {"channel": ch, "txpower": tx},
            "before": helper_snap_public(before),
            "after": helper_snap_public(after),
            "reloaded": False, "verified": True,
        }, None

    orig = {"channel": uci.get("channel", ""), "txpower": uci.get("txpower", "")}

    # ── write config (fast, no effect on the link yet) ──────────────────────
    cmd = ("uci set wireless.%(r)s.channel=%(c)d && "
           "uci set wireless.%(r)s.txpower=%(t)d && "
           "uci commit wireless") % {"r": radio, "c": ch, "t": tx}
    rc, out, e = helper_run(cmd)
    if rc != 0:
        try:
            helper_run("uci revert wireless")
        except Exception:
            pass
        return None, "uci set/commit failed: %s" % (e.strip() or out.strip() or "rc=%d" % rc)

    # ── arm the DUT safety net, then reload ─────────────────────────────────
    if cfg["guard_enable"]:
        try:
            helper_guard_arm(radio, orig)
        except Exception as ex:
            # Cannot protect the change: put the old values back, do not reload.
            try:
                helper_run(helper_restore_cmds(radio, orig))
            except Exception:
                pass
            return None, "aborted before reload: %s" % ex

    failure = None
    after = None
    t0 = time.time()
    reloading.set()
    try:
        try:
            helper_start_reload(radio)
        except ConnectionError as ex:
            helper_log("link dropped while starting reload: %s" % ex)
        except Exception as ex:
            failure = str(ex)

        if failure is None:
            if not helper_reconnect(cfg["reconnect_deadline"],
                                    cfg["reconnect_grace"] + cfg["reload_start_delay"]):
                # No ssh: nothing more can be sent. The DUT guard will restore.
                return None, ("ssh did not come back within %ss after reload; "
                              "DUT guard will restore the old values in %ss" % (
                                  cfg["reconnect_deadline"], cfg["guard_seconds"]))
            helper_log("link back after %.1fs" % (time.time() - t0))

            ifname = helper_wait_ready(radio, cfg["ready_deadline"])
            if not ifname:
                failure = "radio %s not up within %ss" % (radio, cfg["ready_deadline"])
            else:
                s = helper_wait_channel(radio, ch, cfg["chan_wait_deadline"])
                if not (s and s.get("ok") and s["channel"] == ch):
                    failure = "channel did not become %d within %ss" % (
                        ch, cfg["chan_wait_deadline"])
                else:
                    time.sleep(float(cfg["commit_settle"]))
                    after = helper_live_read(radio)

        if failure is None and after is None:
            failure = "readback failed after reload"

        if failure is not None:
            helper_log("APPLY failed: %s" % failure)
            if cfg["restore_on_fail"]:
                helper_restore(radio, orig)
            return None, failure
    finally:
        reloading.clear()

    helper_guard_disarm(radio)

    verified = bool(after["ok"] and after["channel"] == ch and
                    helper_num_eq(after["txpower"], tx, tol))
    helper_log("verify: %s  after ch=%s tx=%s (total %.1fs)" % (
        "OK" if verified else "FAILED", after.get("channel"),
        after.get("txpower"), time.time() - t0))

    return {
        "radio": radio, "iface": after.get("ifname"),
        "requested": {"channel": ch, "txpower": tx},
        "before": helper_snap_public(before),
        "after": helper_snap_public(after),
        "reloaded": True,
        "verified": verified,
    }, None


# --------------------------------------------------------------- commands ---
def helper_status_data() -> dict:
    radios = {}
    for radio in helper_uci_radios():
        uci = helper_uci_read(radio)
        live = helper_live_read(radio)
        entry = {
            "ok": bool(live and live.get("ok")),
            "uci": {"channel": uci.get("channel"), "txpower": uci.get("txpower"),
                    "disabled": uci.get("disabled") == "1"},
        }
        if live:
            entry.update(helper_snap_public(live))
        else:
            entry["error"] = "no AP interface up"
        radios[radio] = entry
    return radios


def handle_command(line):
    t = [x for x in re.split(r"[\s,]+", line.strip()) if x]
    if not t:
        return core.send_response(False, "ERROR", "empty command")

    cmd = t[0].upper()
    if cmd == "SSHALIVE":
        return core.send_response(True, "OK", "ssh alive status", {"alive": is_ssh_alive})

    if cmd not in ("STATUS", "APPLY"):
        return core.send_response(False, "ERROR",
            "commands: SSHALIVE | STATUS | APPLY <radio> <chan> <txpower_dBm>")

    with cmd_lock:
        try:
            if not is_ssh_alive:
                connect()

            if cmd == "STATUS":
                radios = helper_status_data()
                all_ok = bool(radios) and all(v["ok"] for v in radios.values())
                return core.send_response(all_ok, "OK" if all_ok else "ERROR",
                    "status read" if all_ok else "some radios not live",
                    {"radios": radios})

            if len(t) != 4:
                return core.send_response(False, "ERROR",
                    "usage: APPLY <radio> <chan> <txpower_dBm>")
            data, err = apply_radio(t[1].lower(), t[2], t[3])
            if err:
                helper_log("APPLY error: %s" % err)
                return core.send_response(False, "ERROR", err)
            return core.send_response(True, "VERIFIED" if data["verified"] else "UNVERIFIED",
                                      "applied", data)
        except Exception as e:
            return core.send_response(False, "ERROR", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global cfg, running

    core.setup_env(APP)
    cfg = core.load_config(APP, DEFAULTS)
    core.start_run_log(APP)

    helper_log("=== SSH(UCI) start Version=%s ===" % APP_version)

    # Keep the DUT guard longer than the worst-case wait, whatever the config says.
    need = int(float(cfg["reconnect_deadline"]) + float(cfg["ready_deadline"]) +
               float(cfg["chan_wait_deadline"])) + 10
    if cfg["guard_enable"] and int(cfg["guard_seconds"]) < need:
        helper_log("guard_seconds %s too short for the configured waits - using %d" % (
            cfg["guard_seconds"], need))
        cfg["guard_seconds"] = need
    helper_log("mediator apply_timeout should be >= %d  "
               "(sweep set apply_timeout %d)" % (
                   recommended_apply_timeout(cfg), recommended_apply_timeout(cfg)))

    try:
        connect()
    except Exception as e:
        helper_log("initial connect failed: %s (heartbeat will retry)" % e)

    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(
        target=core.run_tcp_server,
        args=(APP, cfg["tcp_host"], cfg["tcp_port"], handle_command, lambda: running),
        daemon=True,
    ).start()

    while True:
        try:
            line = input("ssh> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line == "":
            continue
        if line.lower() in ("exit", "quit"):
            break
        if line.lower() == "help":
            helper_log("commands: SSHALIVE | STATUS | APPLY <radio> <chan> <txpower_dBm> | exit")
            continue

        helper_log("> " + line)
        try:
            result = handle_command(line)
        except Exception as e:
            result = core.send_response(False, "ERROR", "internal error: %s" % e)
        helper_log("< " + json.dumps(result),
            console="<\n" + core.humanize_table(result))

    running = False
    helper_log("=== session end ===")


if __name__ == "__main__":
    main()
