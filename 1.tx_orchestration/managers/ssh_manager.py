#!/usr/bin/env python3
"""
ssh_manager.py - persistent SSH link to an OpenWrt DUT with live radio control.

Changes channel with hostapd_cli and txpower with iw, on the running system.
No UCI, no wifi reload, no restarts. A heartbeat keeps the SSH connection up
and reconnects it after a drop. All folder, config, log and TCP
infrastructure lives in core/core.py; this file only holds the SSH and
iw / hostapd_cli logic.

Usage:
    python ssh_manager.py
    The console shows only the prompt "ssh> ". Everything else goes to the
    run log. To reach the DUT through a jump server, start jump_tunnel.py and
    set dut_host = "127.0.0.1", dut_port = <tunnel local port>.

TCP commands (one per line, case-insensitive, JSON reply):
    SSHALIVE                            is the SSH link currently up
    STATUS                              live channel/txpower of every configured
                                        radio (iw dev <if> info)
    APPLY <radio> <chan> <txpower_dBm>  blocking: change channel if needed, set
                                        txpower, settle, read back, and reply
                                        with before / after / verified

Radios are mapped to DUT interfaces with flat config keys:
    radio0_iface = "wlan0"    # 2.4 GHz
    radio1_iface = "wlan1"    # 5 GHz (add when available)
The phy of each interface is detected from `iw dev <if> info`. The band is
derived from the interface's current frequency (below 3000 MHz = 2.4 GHz).

Configuration (ssh_manager_config.toml; every tunable lives here):
    dut_host, dut_port, dut_user, dut_pass   DUT SSH credentials
    tcp_host, tcp_port                       this manager's TCP listener
    radio0_iface, radio1_iface               radio name -> interface
    connect_timeout, cmd_timeout             SSH timeouts, seconds
    heartbeat_interval,
    heartbeat_lock_timeout                   heartbeat timing, seconds
    csa_count                                beacons for chan_switch (default 5)
    chan_switch_extra                        extra hostapd_cli chan_switch args,
                                             e.g. "ht" (default "")
    hostapd_cli_extra                        extra hostapd_cli global args,
                                             e.g. "-p /var/run/hostapd"
    chan_switch_deadline                     max seconds to see the new channel
    chan_poll_interval                       seconds between channel polls
    delay_after_chan, delay_after_tx         waits after each step, seconds
    commit_settle                            settle before readback (default 3)
    txpower_tolerance                        dBm tolerance for verification
    txpower_min, txpower_max                 valid txpower range, dBm

    The DUT defaults can also be set with the environment variables
    DUT_HOST, DUT_PORT, DUT_USER and DUT_PASS.

Files (created by core.setup_env):
    Service/Service_configs/ssh_manager/ssh_manager_config.toml
    Service/Service_logs/ssh_manager/ssh_manager_<timestamp>.log

Example:
    echo STATUS | ncat 127.0.0.1 9000

Requirements:
    Python 3.11+ and paramiko (pip install paramiko). Needs core/core.py.
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

APP = "ssh_manager"
APP_version = core.get_version()

DEFAULTS = {
    "dut_host": os.environ.get("DUT_HOST", "192.168.99.1"),
    "dut_port": int(os.environ.get("DUT_PORT", 22)),
    "dut_user": os.environ.get("DUT_USER", "root"),
    "dut_pass": os.environ.get("DUT_PASS", "7890"),
    "tcp_host": "127.0.0.1",
    "tcp_port": 9000,
    "radio0_iface": "wlan0",
    "radio1_iface": "",
    "connect_timeout": 5,
    "cmd_timeout": 10,
    "heartbeat_interval": 3,
    "heartbeat_lock_timeout": 0.5,
    "csa_count": 5,
    "chan_switch_extra": "",
    "hostapd_cli_extra": "",
    "chan_switch_deadline": 20,
    "chan_poll_interval": 0.5,
    "delay_after_chan": 1.0,
    "delay_after_tx": 1.0,
    "commit_settle": 3.0,
    "txpower_tolerance": 0.5,
    "txpower_min": 0,
    "txpower_max": 40,
}

# ---------------------------------------------------------------- globals ---
cfg = {}
running = True
is_ssh_alive = False
client = None

lock = threading.RLock()       # guards the ssh channel + `client`
cmd_lock = threading.Lock()    # one TCP/prompt command at a time

_phy_cache = {}                # ifname -> "phyN"


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
        if not lock.acquire(timeout=cfg["heartbeat_lock_timeout"]):
            continue   # a command is busy
        try:
            ok = _helper_exec("true", cfg["connect_timeout"])[0] == 0
        except Exception:
            ok = False
        finally:
            lock.release()
        helper_set_alive(ok)
        if not ok:
            try:
                connect()
            except Exception:
                pass


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
    rc, out, err = helper_run("iw dev %s info" % ifname)
    if rc != 0:
        return {"ifname": ifname, "ok": False,
                "error": (err.strip() or out.strip() or "iw rc=%d" % rc)}
    snap = helper_parse_iw_info(out, ifname)
    if snap["phy"]:
        _phy_cache[ifname] = snap["phy"]
    return snap


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


def helper_chan_to_freq(ch: int, freq_now):
    if freq_now is None:
        return None, "current frequency unknown, cannot pick band"
    if freq_now < 3000:                      # 2.4 GHz
        if ch == 14:
            return 2484, None
        if 1 <= ch <= 13:
            return 2407 + 5 * ch, None
        return None, "channel %d invalid for 2.4 GHz (1-14)" % ch
    if 1 <= ch <= 196:                       # 5 GHz
        return 5000 + 5 * ch, None
    return None, "channel %d invalid for 5 GHz" % ch


def helper_radio_iface(radio: str):
    return (cfg.get("%s_iface" % radio) or "").strip() or None


def helper_configured_radios() -> dict:
    out = {}
    for k, v in cfg.items():
        m = re.fullmatch(r"(radio\d+)_iface", k)
        if m and str(v).strip():
            out[m.group(1)] = str(v).strip()
    return out


# ------------------------------------------------------------------ apply ---
def apply_radio(radio: str, ch_s: str, tx_s: str):
    ifname = helper_radio_iface(radio)
    if not ifname:
        return None, "unknown radio %r (configured: %s)" % (
            radio, ", ".join(helper_configured_radios()) or "none")

    lo, hi = cfg["txpower_min"], cfg["txpower_max"]
    if not ch_s.isdigit():
        return None, "channel must be a number (auto is not supported)"
    if not tx_s.isdigit() or not lo <= int(tx_s) <= hi:
        return None, "txpower must be a whole dBm value %d-%d" % (lo, hi)
    ch, tx = int(ch_s), int(tx_s)

    before = helper_read_iface(ifname)
    if not before["ok"]:
        return None, "cannot read %s: %s" % (ifname, before.get("error", "no channel/txpower in iw output"))
    if before.get("type") not in (None, "AP"):
        return None, "%s is type %s, chan_switch needs AP" % (ifname, before["type"])
    phy = before["phy"] or _phy_cache.get(ifname)
    if not phy:
        return None, "cannot determine phy of %s" % ifname

    helper_log("apply %s(%s/%s) %s -> ch=%d tx=%d dBm" % (
        radio, ifname, phy, "ch=%s tx=%s" % (before["channel"], before["txpower"]), ch, tx))

    # ── channel ─────────────────────────────────────────────────────────────
    if before["channel"] != ch:
        freq, err = helper_chan_to_freq(ch, before["freq"])
        if err:
            return None, err
        extra = (" " + cfg["chan_switch_extra"].strip()) if cfg["chan_switch_extra"].strip() else ""
        hcli  = (cfg["hostapd_cli_extra"].strip() + " ") if cfg["hostapd_cli_extra"].strip() else ""
        cmd = "hostapd_cli %s-i %s chan_switch %d %d%s" % (
            hcli, ifname, int(cfg["csa_count"]), freq, extra)
        rc, out, e = helper_run(cmd)
        helper_log("chan_switch: rc=%s out=%r err=%r" % (rc, out.strip(), e.strip()))
        if rc != 0 or "OK" not in out:
            return None, "chan_switch failed: %s" % (out.strip() or e.strip() or "rc=%d" % rc)

        # CSA completes after csa_count beacons - poll until iw shows it.
        deadline = time.time() + float(cfg["chan_switch_deadline"])
        seen = False
        while time.time() < deadline:
            time.sleep(float(cfg["chan_poll_interval"]))
            s = helper_read_iface(ifname)
            if s["ok"] and s["channel"] == ch:
                seen = True
                break
        if not seen:
            return None, "channel did not change to %d within %ss" % (
                ch, cfg["chan_switch_deadline"])
        time.sleep(float(cfg["delay_after_chan"]))
    else:
        helper_log("channel already %d - skipping chan_switch" % ch)

    # ── txpower (after channel: some drivers reset it on a channel change) ──
    cur = helper_read_iface(ifname)
    if not cur["ok"] or cur["txpower"] is None or \
            abs(cur["txpower"] - tx) > float(cfg["txpower_tolerance"]):
        rc, out, e = helper_run("iw phy %s set txpower limit %d" % (phy, tx * 100))
        helper_log("set txpower: rc=%s err=%r" % (rc, e.strip()))
        if rc != 0:
            return None, "iw set txpower failed: %s" % (e.strip() or out.strip() or "rc=%d" % rc)
        time.sleep(float(cfg["delay_after_tx"]))
    else:
        helper_log("txpower already %s dBm - skipping" % cur["txpower"])

    # ── settle + readback ───────────────────────────────────────────────────
    time.sleep(float(cfg["commit_settle"]))
    after = helper_read_iface(ifname)

    verified = bool(after["ok"] and after["channel"] == ch and
                    abs(after["txpower"] - tx) <= float(cfg["txpower_tolerance"]))
    helper_log("verify: %s  after ch=%s tx=%s" % (
        "OK" if verified else "FAILED", after.get("channel"), after.get("txpower")))

    return {
        "radio": radio, "iface": ifname,
        "requested": {"channel": ch, "txpower": tx},
        "before": helper_snap_public(before),
        "after": helper_snap_public(after),
        "verified": verified,
    }, None


# --------------------------------------------------------------- commands ---
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
                radios = {}
                for r, ifname in helper_configured_radios().items():
                    radios[r] = helper_snap_public(helper_read_iface(ifname))
                all_ok = bool(radios) and all(v["ok"] for v in radios.values())
                return core.send_response(all_ok, "OK" if all_ok else "ERROR",
                    "status read" if all_ok else "some radios unreadable",
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

    helper_log("=== SSH start Version=%s ===" % APP_version)

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
        # File: raw JSON (wire-level record). Console: human-readable table.
        helper_log("< " + json.dumps(result),
            console="<\n" + core.humanize_table(result))

    running = False
    helper_log("=== session end ===")


if __name__ == "__main__":
    main()
