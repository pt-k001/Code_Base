#!/usr/bin/env python3
"""
capture_manager.py - RF capture station with a Tkinter GUI and a TCP control port.

Captures the configured screen regions at a fixed interval (mss + OpenCV),
skips frames that did not change, and stores the PNG frames in a SQLite
database. Capture can be driven from the GUI or remotely over TCP by an
orchestrator such as mediator.py.

Usage:
    python capture_manager.py
    The window itself lives in capture_manager_ui.py. It has three tabs: Capture (regions, start/stop, previews), Log,
    and Settings (interval, TCP port, database options, stop-me options).

TCP commands (one per line, JSON reply via the core.py protocol):
    SESSION <name>    set the session name (version is bumped if the name is
                      reused)
    DBNAME [<name>]   set the database name (folder and file name); without
                      an argument, report the current one
    START             begin capturing (needs at least one region)
    STOP              stop capturing
    NUM               current total frame count
    STATUS            full state: running, session, version, total, regions,
                      database name, pending writes, database path

    The TCP port is set in the Settings tab (default 9002).

Stop-me notification:
    When enabled in Settings, the manager sends "STOPME" to the orchestrator
    (orchestrator_host:orchestrator_port, default 127.0.0.1:9100) once no
    region has changed for stopme_idle_seconds (default 30).

Files (created by core.setup_env):
    Service/Service_configs/capture_manager/capture_manager_config.toml
    Service/Service_logs/capture_manager/capture_manager_<timestamp>.log
    Service/Service_profiles/capture_manager/capture_manager_profiles.toml
                                           saved region sets (profiles)

Databases:
    Service/Service_DBs/DBs/<db_name>/<db_name>.db    primary (can be switched off)
    <secondary_dir>/<db_name>/<db_name>.db            optional second location

    At least one of primary / secondary must be on, otherwise capture will
    not start (GUI Start and TCP START both refuse).

    Table "captures": id, session, region, seq, version, ts, hash, image (PNG).

Requirements:
    Python 3.11+ with tkinter, and: opencv-python, numpy, mss, Pillow.
    Needs core/core.py.
"""

import threading
import queue
import time
import os
import re
import hashlib
import sqlite3
import cv2
import numpy as np
import mss
import sys
from concurrent.futures import ThreadPoolExecutor

from datetime import datetime

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.core as core  # noqa: E402

APP         = "capture_manager"
APP_VERSION = core.get_version()

# ─────────────────────────────────────────────────────────────────────────────
# Paths / Constants
# ─────────────────────────────────────────────────────────────────────────────
# Config / logs / profiles live where core.setup_env puts them:
#   Service_configs/<APP>/<APP>_config.toml
#   Service_logs/<APP>/<APP>_<ts>.log
#   Service_profiles/<APP>/<APP>_profiles.toml
# Databases:
#   <root>/Service_DBs/DBs/<db_name>/<db_name>.db
#   <secondary_dir>/<db_name>/<db_name>.db   (optional parallel copy)
ENV = core.setup_env(APP)
DBS_ROOT = os.path.join(ENV["dir"], "Service", "Service_DBs", "DBs")
os.makedirs(DBS_ROOT, exist_ok=True)

DEFAULT_HOST            = "0.0.0.0"
DEFAULT_PORT            = 9002
DEFAULT_QUEUE_BATCH     = 100
DEFAULT_QUEUE_TIMEOUT   = 5.0
DEFAULT_PNG_COMPRESSION = 1

MAX_LABEL_LEN = 64

CONFIG_DEFAULTS = {
    "interval":        1.0,
    "port":            DEFAULT_PORT,
    "queue_batch":     DEFAULT_QUEUE_BATCH,
    "queue_timeout":   DEFAULT_QUEUE_TIMEOUT,
    "png_compression": DEFAULT_PNG_COMPRESSION,
    "db_name":         "unnamed",
    "primary_enabled":   True,
    "secondary_enabled": False,
    "secondary_dir":     "",
    "last_stopped":    False,
    "auto_timestamp_db_name": False,
    "log_level":       "INFO",
    "log_retention_days": 14,
    "always_on_top":   False,
    "stopme_enabled":      False,
    "orchestrator_host":   "127.0.0.1",
    "orchestrator_port":   9100,
    "stopme_idle_seconds": 30.0,
}

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}



# ─────────────────────────────────────────────────────────────────────────────
# Global State
# ─────────────────────────────────────────────────────────────────────────────
class GlobalState:
    def __init__(self):
        self.is_running      = False
        self.current_session = "unnamed"
        self.session_version = 1
        self.db_name          = "unnamed"
        self.active_db_name   = "unnamed"
        self.primary_enabled   = True
        self.secondary_enabled = False
        self.secondary_dir     = ""
        self.regions         = []
        self.counters        = {}
        self.total_captured  = 0
        self.interval        = 1.0
        self.port            = DEFAULT_PORT
        self.queue_batch     = DEFAULT_QUEUE_BATCH
        self.queue_timeout   = DEFAULT_QUEUE_TIMEOUT
        self.png_compression = DEFAULT_PNG_COMPRESSION
        self.last_hash        = {}
        self.last_stopped     = False

        self.auto_timestamp_db_name = False
        self.log_level        = "INFO"
        self.log_retention_days = 14
        self.always_on_top    = False

        self.stopme_enabled      = False
        self.orchestrator_host   = "127.0.0.1"
        self.orchestrator_port   = 9100
        self.stopme_idle_seconds = 30.0
        self.last_change_time    = 0.0
        self.stopme_sent         = False

        self.lock            = threading.Lock()

state = GlobalState()


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────────────────
def _helper_num(v, typ, default, lo=None, hi=None):
    if isinstance(v, bool) and typ is not bool:
        return default
    try:
        x = typ(v)
    except (TypeError, ValueError):
        return default
    if x != x:                      # NaN
        return default
    if lo is not None and x < lo:
        x = typ(lo)
    if hi is not None and x > hi:
        x = typ(hi)
    return x

def _helper_bool(v, default):
    return v if isinstance(v, bool) else default

def _helper_str(v, default):
    return v if isinstance(v, str) else default

def helper_clean_label(s, maxlen: int = MAX_LABEL_LEN) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip()
    if not s or len(s) > maxlen:
        return ""
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        return ""
    return s

def _helper_natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

def helper_clean_region(r):
    try:
        name = helper_clean_label(r["name"])
        x, y, w, h = (int(v) for v in r["rect"])
    except (KeyError, TypeError, ValueError):
        return None
    if not name or w <= 0 or h <= 0:
        return None
    return {"name": name, "rect": (x, y, w, h)}


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────
def load_settings():
    cfg = core.load_config(APP, CONFIG_DEFAULTS)
    d   = CONFIG_DEFAULTS
    state.interval        = _helper_num(cfg.get("interval"), float, d["interval"], 0.0, 3600.0)
    state.port            = _helper_num(cfg.get("port"), int, d["port"], 1, 65535)
    state.queue_batch     = _helper_num(cfg.get("queue_batch"), int, d["queue_batch"], 1, 100000)
    state.queue_timeout   = _helper_num(cfg.get("queue_timeout"), float, d["queue_timeout"], 0.1, 3600.0)
    state.png_compression = _helper_num(cfg.get("png_compression"), int, d["png_compression"], 0, 9)
    state.db_name         = helper_clean_label(_helper_str(cfg.get("db_name"), d["db_name"])) or d["db_name"]
    state.last_stopped    = _helper_bool(cfg.get("last_stopped"), False)
    state.primary_enabled   = _helper_bool(cfg.get("primary_enabled"), True)
    state.secondary_enabled = _helper_bool(cfg.get("secondary_enabled"), False)
    state.secondary_dir     = _helper_str(cfg.get("secondary_dir"), "")
    if not (state.primary_enabled or (state.secondary_enabled and state.secondary_dir.strip())):
        state.primary_enabled = True
        core.logmsg(APP, "No storage path enabled in config - primary re-enabled")
    state.auto_timestamp_db_name = _helper_bool(cfg.get("auto_timestamp_db_name"), False)
    lvl = _helper_str(cfg.get("log_level"), "INFO").upper()
    state.log_level          = lvl if lvl in LOG_LEVELS else "INFO"
    state.log_retention_days = _helper_num(cfg.get("log_retention_days"), int,
                                    d["log_retention_days"], 0, 36500)
    state.always_on_top      = _helper_bool(cfg.get("always_on_top"), False)
    state.stopme_enabled      = _helper_bool(cfg.get("stopme_enabled"), False)
    state.orchestrator_host   = _helper_str(cfg.get("orchestrator_host"), d["orchestrator_host"]).strip() \
                                or d["orchestrator_host"]
    state.orchestrator_port   = _helper_num(cfg.get("orchestrator_port"), int,
                                     d["orchestrator_port"], 1, 65535)
    state.stopme_idle_seconds = _helper_num(cfg.get("stopme_idle_seconds"), float,
                                     d["stopme_idle_seconds"], 0.0, 86400.0)

def save_settings():
    try:
        core.save_config(APP, {
            "interval":        state.interval,
            "port":            state.port,
            "queue_batch":     state.queue_batch,
            "queue_timeout":   state.queue_timeout,
            "png_compression": state.png_compression,
            "db_name":         state.db_name,
            "last_stopped":    state.last_stopped,
            "primary_enabled":   state.primary_enabled,
            "secondary_enabled": state.secondary_enabled,
            "secondary_dir":     state.secondary_dir,
            "auto_timestamp_db_name": state.auto_timestamp_db_name,
            "log_level":          state.log_level,
            "log_retention_days": state.log_retention_days,
            "always_on_top":      state.always_on_top,
            "stopme_enabled":      state.stopme_enabled,
            "orchestrator_host":   state.orchestrator_host,
            "orchestrator_port":   state.orchestrator_port,
            "stopme_idle_seconds": state.stopme_idle_seconds,
        })
    except OSError as e:
        core.logmsg(APP, "Could not save settings: %s" % e)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

def helper_safe_db_name(db_name: str) -> str:
    return _SAFE_NAME_RE.sub("_", (db_name or "").strip()).strip("_") or "unnamed"

def helper_db_name_dir(db_name: str, root: str = None) -> str:
    return os.path.join(root or DBS_ROOT, helper_safe_db_name(db_name))

def helper_db_file_path(db_name: str, root: str = None) -> str:
    return os.path.join(helper_db_name_dir(db_name, root), helper_safe_db_name(db_name) + ".db")

def helper_primary_db_path(db_name: str):
    return helper_db_file_path(db_name) if state.primary_enabled else None

def helper_secondary_db_path(db_name: str):
    d = (state.secondary_dir or "").strip()
    if not state.secondary_enabled or not d:
        return None
    return helper_db_file_path(db_name, d)

def helper_has_storage(db_name: str) -> bool:
    return bool(helper_primary_db_path(db_name) or helper_secondary_db_path(db_name))

NO_STORAGE_MSG = "No storage path enabled - turn on primary or set a secondary folder"

def helper_session_db_path(db_name: str) -> str:
    return helper_primary_db_path(db_name) or helper_secondary_db_path(db_name) or helper_db_file_path(db_name)

def helper_make_active_db_name(base_name: str) -> str:
    base = (base_name or "unnamed").strip() or "unnamed"
    if state.auto_timestamp_db_name:
        return "%s_%s" % (base, datetime.now().strftime("%Y%m%d_%H%M%S"))
    return base

# ── Profiles: Service_profiles/<APP>/<APP>_profiles.toml ─────────────────────
# Stored as a list of {name, regions} tables, because core's TOML writer does
# not quote keys (a profile called "my profile" would break as a key).
def load_profiles() -> dict:
    data = core.load_profiles(APP, {})
    out  = {}
    items = data.get("profiles", []) if isinstance(data, dict) else []
    if isinstance(items, list):
        for p in items:
            if not isinstance(p, dict):
                continue
            pname = helper_clean_label(p.get("name"))
            if not pname:
                continue
            regs = [helper_clean_region(r) for r in p.get("regions", []) if isinstance(r, dict)]
            out[pname] = [r for r in regs if r]
    return out

def save_profiles(profiles: dict):
    core.save_profiles(APP, {"profiles": [
        {"name": n,
         "regions": [{"name": r["name"], "rect": list(r["rect"])} for r in regs]}
        for n, regs in profiles.items()
    ]})

def helper_prune_old_logs(retention_days):
    if not retention_days or retention_days <= 0:
        return
    log_dir = ENV["log_dir"]
    if not os.path.isdir(log_dir):
        return
    cutoff = time.time() - retention_days * 86400
    for entry in os.listdir(log_dir):
        if not entry.lower().endswith(".log"):
            continue
        path = os.path.join(log_dir, entry)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass

def helper_next_session_version(db_path: str, session: str) -> int:
    if not os.path.exists(db_path):
        return 1
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        row  = conn.execute(
            "SELECT MAX(version) FROM captures WHERE session = ?", (session,)).fetchone()
        if row and row[0]:
            return int(row[0]) + 1
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# SQLite capture store
# ─────────────────────────────────────────────────────────────────────────────
class DBWriter:

    MAX_RETRY_BUF = 2000    # frames kept in RAM while the target is unreachable
    MAX_QUEUE     = 3000    # frames allowed waiting in the queue

    def __init__(self, app: "CaptureGUI", label: str):
        self.app     = app
        self.label   = label
        self.q: "queue.Queue" = queue.Queue()
        self.conn    = None
        self._stop   = threading.Event()
        self._thread = None
        self.db_path = None
        self._dropped = 0

    def start(self, db_path: str):
        self.db_path = db_path
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="db-" + self.label)
        self._thread.start()

    def switch(self, db_path: str):
        self.q.put(("SWITCH", db_path))

    def enqueue(self, row):
        if self.q.qsize() >= self.MAX_QUEUE:
            self._dropped += 1
            if self._dropped % 100 == 1:
                self.app.queue_log("%s DB queue full - dropped %d frame(s) so far" %
                                   (self.label, self._dropped), level="ERROR")
            return
        self.q.put(("DATA",) + row)

    def _open(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.conn    = sqlite3.connect(db_path, timeout=10.0)
        cur          = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS captures (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT    NOT NULL,
                region  TEXT    NOT NULL,
                seq     INTEGER NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                ts      REAL    NOT NULL,
                hash    TEXT    NOT NULL,
                image   BLOB    NOT NULL
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_captures_session_region
            ON captures(session, region)
        """)
        self.conn.commit()

    def _try_open(self, db_path) -> bool:
        try:
            self._open(db_path)
            return True
        except Exception as e:
            self._close()
            self.app.queue_log("%s DB open failed (%s): %s" %
                               (self.label, db_path, e), level="ERROR")
            return False

    def _close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def _cap(self, buf):
        if len(buf) > self.MAX_RETRY_BUF:
            self.app.queue_log("%s DB unreachable - dropped %d frames" %
                               (self.label, len(buf) - self.MAX_RETRY_BUF),
                               level="ERROR")
            buf = buf[-self.MAX_RETRY_BUF:]
        return buf

    def _flush(self, buf):
        if not buf:
            return []
        if self.conn is None and not self._try_open(self.db_path):
            return self._cap(buf)
        try:
            self.conn.executemany(
                "INSERT INTO captures (session, region, seq, version, ts, hash, image) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                buf,
            )
            self.conn.commit()
            return []
        except Exception as e:
            self.app.queue_log("%s DB write error: %s" % (self.label, e), level="ERROR")
            try:
                self.conn.rollback()
            except Exception:
                pass
            self._close()            # force a clean reopen on the next attempt
            return self._cap(buf)

    def _run(self):
        self._try_open(self.db_path)
        buf        = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                batch_size    = state.queue_batch
                batch_timeout = state.queue_timeout
                timeout = max(0.0, batch_timeout - (time.time() - last_flush))
                try:
                    item = self.q.get(timeout=timeout if timeout > 0 else 0.05)
                    if item[0] == "SWITCH":
                        buf = self._flush(buf)
                        if buf:
                            self.app.queue_log(
                                "%s DB: discarded %d unsaved frames on switch" %
                                (self.label, len(buf)), level="WARNING")
                            buf = []
                        self._close()
                        self._try_open(item[1])
                        self.db_path = item[1]
                        self.app.queue_log("%s DB switched to: %s" %
                                           (self.label, self.db_path))
                        last_flush = time.time()
                        continue
                    buf.append(item[1:])
                except queue.Empty:
                    pass
                now = time.time()
                if buf and (len(buf) >= batch_size or now - last_flush >= batch_timeout):
                    buf        = self._flush(buf)
                    last_flush = now
            except Exception as e:      # never let the writer thread die
                self.app.queue_log("%s writer error: %s" % (self.label, e), level="ERROR")
                time.sleep(0.5)

        while True:
            try:
                item = self.q.get_nowait()
                if item[0] == "DATA":
                    buf.append(item[1:])
            except queue.Empty:
                break
        self._flush(buf)
        self._close()

    def pending(self) -> int:
        return self.q.qsize()

    def file_size(self) -> int:
        try:
            return os.path.getsize(self.db_path)
        except Exception:
            return 0

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)


class CaptureStore:

    def __init__(self, app: "CaptureGUI"):
        self.app       = app
        self.primary   = None
        self.secondary = None

    @property
    def db_path(self):
        pri = self.primary
        return pri.db_path if pri else None

    @property
    def secondary_path(self):
        sec = self.secondary
        return sec.db_path if sec else None

    def start(self, primary_path: str = None, secondary_path: str = None):
        self.set_primary(primary_path)
        self.set_secondary(secondary_path)

    def change_target(self, primary_path: str = None, secondary_path: str = None):
        self.set_primary(primary_path)
        self.set_secondary(secondary_path)

    def _set_writer(self, attr: str, label: str, path: str = None):
        writer = getattr(self, attr)
        if path is None:
            if writer:
                setattr(self, attr, None)
                writer.stop()
                self.app.queue_log("%s save disabled" % label)
        elif writer is None:
            writer = DBWriter(self.app, label)
            writer.start(path)
            setattr(self, attr, writer)
            self.app.queue_log("%s save enabled: %s" % (label, path))
        elif writer.db_path != path:
            writer.switch(path)

    def set_primary(self, path: str = None):
        self._set_writer("primary", "Primary", path)

    def set_secondary(self, path: str = None):
        self._set_writer("secondary", "Secondary", path)

    def enqueue(self, session, region, seq, version, img_hash, png_bytes):
        row = (session, region, seq, version, time.time(), img_hash, png_bytes)
        for writer in (self.primary, self.secondary):
            if writer:
                writer.enqueue(row)

    def pending(self) -> int:
        pri = self.primary
        return pri.pending() if pri else 0

    def secondary_pending(self) -> int:
        sec = self.secondary
        return sec.pending() if sec else 0

    def is_busy(self) -> bool:
        return (self.pending() > state.queue_batch or
                self.secondary_pending() > state.queue_batch)

    def file_size(self) -> int:
        pri = self.primary
        return pri.file_size() if pri else 0

    def stop(self):
        writers = [w for w in (self.primary, self.secondary) if w]
        for writer in writers:
            writer._stop.set()
        for writer in writers:
            writer.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Capture Engine
# ─────────────────────────────────────────────────────────────────────────────
class CaptureEngine:
    def __init__(self, app: "CaptureGUI"):
        self.app  = app
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="encoder")

    def _crop_and_hash(self, reg, img, ox, oy):
        x, y, w, h = reg["rect"]
        name       = reg["name"]
        H, W       = img.shape[:2]
        x0 = max(0, x - ox);      y0 = max(0, y - oy)
        x1 = min(W, x - ox + w);  y1 = min(H, y - oy + h)
        if x1 <= x0 or y1 <= y0:
            return name, None, None
        crop = img[y0:y1, x0:x1]
        img_hash = hashlib.sha256(crop.tobytes()).hexdigest()[:16]
        return name, crop, img_hash

    def _write_region(self, name, crop, img_hash, session):
        try:
            encode_params = [cv2.IMWRITE_PNG_COMPRESSION, int(state.png_compression)]
            ok, encoded   = cv2.imencode(".png", crop, encode_params)
            if not ok:
                self.app.queue_log("Encode failed for region %s" % name, level="ERROR")
                return

            png_bytes = encoded.tobytes()

            with state.lock:
                count                = state.counters.get(name, 1)
                state.counters[name] = count + 1
                state.total_captured += 1
                version              = state.session_version

            self.app.store.enqueue(session, name, count, version, img_hash, png_bytes)

            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            self.app.receive_preview(name, rgb, count)      # thread-safe queue
        except Exception as e:
            self.app.queue_log("Write failed for region %s: %s" % (name, e), level="ERROR")

    def run_loop(self):
        while True:
            try:
                with mss.mss() as sct:
                    while True:
                        try:
                            self._cycle(sct)
                        except Exception as e:
                            self.app.queue_log("Capture error: %s" % e, level="ERROR")
                            time.sleep(1)
            except Exception as e:      # mss itself failed; retry
                self.app.queue_log("Capture engine restart: %s" % e, level="ERROR")
                time.sleep(2)

    def _cycle(self, sct):
        if not state.is_running:
            time.sleep(0.1)
            return

        with state.lock:
            regions  = list(state.regions)
            session  = state.current_session
            interval = state.interval

        if not regions:
            time.sleep(0.25)
            return

        # Whole virtual screen, so regions on any monitor work (the overlay
        # reports virtual-screen coordinates).
        mon        = sct.monitors[0]
        screenshot = sct.grab(mon)
        img        = cv2.cvtColor(np.array(screenshot), cv2.COLOR_BGRA2BGR)
        ox, oy     = mon["left"], mon["top"]

        crops = list(self.pool.map(
            lambda r: self._crop_and_hash(r, img, ox, oy), regions))

        # Regions that fall outside the screen are ignored for dedup.
        valid = [c for c in crops if c[2] is not None]

        # Batch-level dedup: skip the cycle only if EVERY valid region's hash
        # is unchanged; otherwise write all regions.
        with state.lock:
            all_unchanged = all(state.last_hash.get(n) == h for n, _, h in valid)
            for n, _, h in valid:
                state.last_hash[n] = h

        if not all_unchanged:
            list(self.pool.map(
                lambda item: self._write_region(item[0], item[1], item[2], session)
                if item[1] is not None else None,
                crops))
            state.last_change_time = time.time()
            state.stopme_sent      = False
        elif state.stopme_enabled and not state.stopme_sent:
            idle = time.time() - state.last_change_time
            if idle >= state.stopme_idle_seconds:
                state.stopme_sent = True
                threading.Thread(target=self._send_stopme, args=(idle,),
                                 daemon=True).start()

        if interval > 0:
            time.sleep(interval)

    def _send_stopme(self, idle_seconds: float):
        host, port = state.orchestrator_host, state.orchestrator_port
        try:
            result = core.tcp_command(host, port, "STOPME")
        except Exception as e:
            result = core.send_response(False, "ERROR", str(e))
        if result.get("ok"):
            self.app.queue_log(
                "STOPME sent to %s:%s after %.1fs idle - %s" %
                (host, port, idle_seconds, result.get("message")))
        else:
            self.app.queue_log(
                "STOPME to %s:%s failed: %s" %
                (host, port, result.get("message")), level="WARNING")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    # Imported here (not at the top) because capture_manager_ui itself imports
    # this module; this keeps a single shared copy of `state`.
    from capture_manager_ui import CaptureGUI
    app = CaptureGUI()
    app.mainloop()


if __name__ == "__main__":
    # Re-enter through the normal module name so the UI and the engine share
    # one `state`, not one per module copy (__main__ vs capture_manager).
    import capture_manager
    capture_manager.main()
