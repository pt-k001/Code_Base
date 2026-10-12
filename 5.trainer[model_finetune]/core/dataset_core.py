"""Dataset maker engine: labelling project, OCR worker and tesstrain export.

A project is a folder holding ONE SQLite file (``[dataset] db_name``).  It stores
the random sample, the preprocessed PNG of every image (exactly what Tesseract
receives), the raw OCR text and the label you typed or accepted.  Nothing is
written as png / gt.txt files until :func:`export_dataset` runs.

Public API used by the UI:

    DatasetProject.create / open      new project from a source database / resume one
    peek_project                      cheap look at a folder (is it a project, how far)
    Prefetcher                        background worker: preprocess + OCR the images
    split_counts / export_dataset     random train/eval split, tesstrain files
    png_to_display_b64                stored PNG -> Tk PhotoImage data

The OCR itself is the toolkit's (ocr_core): same decoding, same filters, same
single pass, so the stored image and text match what a toolkit run would use.
"""
import base64
import itertools
import os
import queue
import random
import re
import sqlite3
import threading
import time
import tomllib
from contextlib import closing
from pathlib import Path

import cv2
import numpy as np

from . import ocr_core
from .core import dump_toml, require

PENDING, ACCEPTED, SKIPPED = "pending", "accepted", "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,   -- position in the random sample (1..N)
    source_id  INTEGER NOT NULL,      -- captures.id in the source database
    session    TEXT,
    region     TEXT,
    seq        INTEGER,
    version    INTEGER,
    image      BLOB,                  -- preprocessed PNG (what OCR saw)
    ocr_text   TEXT,                  -- raw OCR text
    confidence REAL,
    suspicious INTEGER,
    error      TEXT,                  -- set when the source image cannot be read
    label      TEXT,                  -- accepted ground truth
    status     TEXT NOT NULL DEFAULT 'pending',
    updated    REAL
);
CREATE INDEX IF NOT EXISTS items_status ON items (status, id);
"""

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def clean_label(text):
    return " ".join(str(text or "").split())


def file_stem(row, strip_prefix):
    column = ocr_core.make_column_name(row["session"] or "", row["region"] or "", strip_prefix)
    column = _UNSAFE.sub("_", column).strip("_")[:80]
    parts = [f"{row['id']:06d}", column, f"{int(row['seq'] or 0):04d}"]
    return "_".join(p for p in parts if p)


def png_to_display_b64(png_bytes, max_w, max_h):
    img = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError("stored image cannot be decoded")
    h, w = img.shape[:2]
    scale = min(max_w / w, max_h / h)
    if scale >= 1:
        img = cv2.resize(img, None, fx=int(scale), fy=int(scale), interpolation=cv2.INTER_NEAREST)
    else:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    return base64.b64encode(cv2.imencode(".png", img)[1].tobytes()).decode("ascii")


def _counts_from(rows):
    counts = {PENDING: 0, ACCEPTED: 0, SKIPPED: 0}
    for status, n in rows:
        counts[status] = n
    counts["total"] = counts[PENDING] + counts[ACCEPTED] + counts[SKIPPED]
    return counts


def peek_project(folder, cfg):
    path = os.path.join(folder, require(cfg, "dataset", "db_name"))
    if not os.path.isfile(path):
        return None
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=cfg["database"]["busy_timeout_s"])) as conn:
            counts = _counts_from(conn.execute(
                "SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
            row = conn.execute("SELECT value FROM meta WHERE key='source_db'").fetchone()
            name = conn.execute("SELECT value FROM meta WHERE key='name'").fetchone()
        return {"counts": counts, "source_db": row[0] if row else "",
                "name": name[0] if name else os.path.basename(os.path.abspath(folder))}
    except sqlite3.Error as exc:
        return {"error": str(exc)}


# ══ project ══════════════════════════════════════════════════════════════════
class DatasetProject:

    def __init__(self, folder, cfg):
        self.folder, self.cfg = os.path.abspath(folder), cfg
        self.db_path = os.path.join(self.folder, require(cfg, "dataset", "db_name"))
        self._lock = threading.RLock()
        self._conn = None
        self.name, self.source_db, self.params, self.use_whitelist = "", "", {}, False

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _connect(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                     timeout=self.cfg["database"]["busy_timeout_s"])
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _load_meta(self):
        meta = {r["key"]: r["value"] for r in self._q("SELECT key, value FROM meta")}
        if "source_db" not in meta or "params" not in meta:
            raise ValueError(f"{self.db_path} is not a dataset project.")
        self.name = meta.get("name", "") or os.path.basename(self.folder)
        self.source_db = meta["source_db"]
        self.use_whitelist = meta.get("use_whitelist") == "1"
        self.params = ocr_core.coerce_params(tomllib.loads(meta["params"]), self.cfg)

    @classmethod
    def open(cls, folder, cfg):
        project = cls(folder, cfg)
        if not os.path.isfile(project.db_path):
            raise ValueError(f"No project in {folder}.")
        project._connect()
        try:
            project._load_meta()
        except Exception:
            project.close()
            raise
        return project

    @classmethod
    def create(cls, folder, source_db, sample_size, cfg, params=None, use_whitelist=None, name=""):
        if sample_size < 1:
            raise ValueError("Sample size must be at least 1.")
        project = cls(folder, cfg)
        if os.path.exists(project.db_path):
            raise ValueError(f"{project.db_path} already exists.")
        params = ocr_core.coerce_params(params, cfg) if params else ocr_core.default_params(cfg)
        if use_whitelist is None:
            use_whitelist = require(cfg, "dataset", "use_whitelist")
        tasks, _columns = ocr_core.scan_tasks(source_db, params["strip_prefix"], cfg)
        if not tasks:
            raise ValueError("The source database has no images.")
        chosen = random.SystemRandom().sample(tasks, min(sample_size, len(tasks)))
        ids = [task[2] for task in chosen]

        meta = {}
        with closing(ocr_core.open_db_readonly(source_db, cfg["database"]["busy_timeout_s"])) as src:
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                marks = ",".join("?" * len(part))
                for rid, session, region, seq, version in src.execute(
                        "SELECT id, session, region, seq, version FROM captures "
                        f"WHERE id IN ({marks})", part):
                    meta[rid] = (session, region, seq, version)

        os.makedirs(project.folder, exist_ok=True)
        project._connect()
        try:
            with project._lock, project._conn:
                project._conn.executemany("INSERT INTO meta VALUES (?, ?)", [
                    ("source_db", os.path.abspath(source_db)),
                    ("params", dump_toml(params)),
                    ("use_whitelist", "1" if use_whitelist else "0"),
                    ("name", name or os.path.basename(project.folder)),
                    ("source_total", str(len(tasks))),
                    ("created", str(time.time()))])
                project._conn.executemany(
                    "INSERT INTO items (id, source_id, session, region, seq, version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(pos, rid, *meta.get(rid, (None, None, None, None)))
                     for pos, rid in enumerate(ids, start=1)])
            project._load_meta()
        except Exception:
            project.close()
            try:
                os.remove(project.db_path)
            except OSError:
                pass
            raise
        return project

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ── queries ───────────────────────────────────────────────────────────
    def _q(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _one(self, sql, args=()):
        rows = self._q(sql, args)
        return rows[0][0] if rows else None

    def counts(self):
        return _counts_from([(r[0], r[1]) for r in self._q(
            "SELECT status, COUNT(*) FROM items GROUP BY status")])

    def get_item(self, item_id):
        rows = self._q("SELECT * FROM items WHERE id = ?", (item_id,))
        return dict(rows[0]) if rows else None

    def has_result(self, item_id):
        return bool(self._one("SELECT image IS NOT NULL OR error IS NOT NULL "
                              "FROM items WHERE id = ?", (item_id,)))

    def source_id(self, item_id):
        return self._one("SELECT source_id FROM items WHERE id = ?", (item_id,))

    def first_pending(self):
        return self._one("SELECT MIN(id) FROM items WHERE status = ?", (PENDING,))

    def next_pending(self, after_id):
        nxt = self._one("SELECT MIN(id) FROM items WHERE status = ? AND id > ?",
                        (PENDING, after_id))
        if nxt is None:
            nxt = self._one("SELECT MIN(id) FROM items WHERE status = ? AND id <> ?",
                            (PENDING, after_id))
        return nxt

    def unprocessed_after(self, after_id, limit):
        return [r[0] for r in self._q(
            "SELECT id FROM items WHERE status = ? AND id > ? AND image IS NULL "
            "AND error IS NULL ORDER BY id LIMIT ?", (PENDING, after_id, limit))]

    def accepted_rows(self):
        return [dict(r) for r in self._q(
            "SELECT id, session, region, seq FROM items WHERE status = ? "
            "AND label IS NOT NULL AND label <> '' ORDER BY id", (ACCEPTED,))]

    # ── writes ────────────────────────────────────────────────────────────
    def save_result(self, item_id, result):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE items SET image=?, ocr_text=?, confidence=?, suspicious=?, error=?, "
                "updated=? WHERE id=? AND image IS NULL AND error IS NULL",
                (result["image"], result["text"], result["confidence"],
                 int(result["suspicious"]), result["error"], time.time(), item_id))

    def set_label(self, item_id, label, status):
        with self._lock, self._conn:
            self._conn.execute("UPDATE items SET label=?, status=?, updated=? WHERE id=?",
                               (label, status, time.time(), item_id))


# ══ OCR worker ═══════════════════════════════════════════════════════════════
class OcrEngine:

    def __init__(self, source_db, params, use_whitelist, cfg):
        self.o = ocr_core.build_options(params, cfg)
        if not use_whitelist:
            self.o["whitelist"] = ""                # raw text: units stay in
        self.conn = ocr_core.open_db_readonly(source_db, self.o["db_timeout_s"])
        try:
            self.api = ocr_core.new_api(self.o)
        except Exception:
            self.conn.close()
            raise

    def process(self, source_id):
        o = self.o
        gray = ocr_core.fetch_gray(self.conn, source_id, o["gray_mode"])
        if gray is None:
            return {"image": None, "text": "", "confidence": 0.0, "suspicious": True,
                    "error": "image could not be read/decoded"}
        base = ocr_core.apply_base_options(gray, o)
        read = ocr_core.single_pass_ocr(self.api, base, o)
        ok, buf = cv2.imencode(".png", ocr_core.render_stage(base, o["stage"], o))
        if not ok:
            return {"image": None, "text": read["text"], "confidence": read["confidence"],
                    "suspicious": True, "error": "preprocessed image could not be encoded"}
        # Raw text keeps its units, so the toolkit's number regex would flag every result;
        # here "suspicious" means low confidence only.
        return {"image": buf.tobytes(), "text": read["text"], "confidence": read["confidence"],
                "suspicious": read["confidence"] < float(o["suspicious_confidence"]),
                "error": None}

    def close(self):
        for closer in (getattr(self.api, "End", None), self.conn.close):
            try:
                closer and closer()
            except Exception:
                pass


class Prefetcher:

    def __init__(self, project):
        self.project, self.done = project, queue.Queue()
        self._q, self._seq = queue.PriorityQueue(), itertools.count()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def request(self, item_id, urgent=False):
        self._q.put((0 if urgent else 1, next(self._seq), item_id))

    def stop(self, timeout):
        self._q.put((-1, next(self._seq), None))
        self._thread.join(timeout)

    def _loop(self):
        project, engine = self.project, None
        try:
            while True:
                _prio, _n, item_id = self._q.get()
                if item_id is None:
                    break
                try:
                    if not project.has_result(item_id):
                        if engine is None:
                            engine = OcrEngine(project.source_db, project.params,
                                               project.use_whitelist, project.cfg)
                        project.save_result(item_id, engine.process(project.source_id(item_id)))
                except Exception as exc:
                    self.done.put((item_id, f"{type(exc).__name__}: {exc}"))
                    continue
                self.done.put((item_id, None))
        finally:
            if engine is not None:
                engine.close()


# ══ export ═══════════════════════════════════════════════════════════════════
def split_counts(n, fraction):
    n_eval = int(round(n * fraction))
    if n < 2:
        n_eval = 0
    elif fraction > 0:
        n_eval = max(1, min(n - 1, n_eval))
    return n - n_eval, n_eval


def _split_dirs(out_dir, cfg):
    return (os.path.join(out_dir, require(cfg, "dataset", "train_dir")),
            os.path.join(out_dir, require(cfg, "dataset", "eval_dir")))


def existing_export_files(out_dir, cfg):
    found = []
    for folder in _split_dirs(out_dir, cfg):
        if os.path.isdir(folder):
            found += [os.path.join(folder, name) for name in os.listdir(folder)
                      if name.lower().endswith((".png", ".gt.txt"))]
    return found


def export_dataset(project, out_dir, eval_fraction, seed, progress=None):
    if not 0 <= eval_fraction < 1:
        raise ValueError("Eval fraction must be at least 0 and below 1.")
    rows = project.accepted_rows()
    if not rows:
        raise ValueError("There are no accepted labels to export.")
    _n_train, n_eval = split_counts(len(rows), eval_fraction)
    ids = [r["id"] for r in rows]
    random.Random(seed).shuffle(ids)
    eval_ids = set(ids[:n_eval])

    train_dir, eval_dir = _split_dirs(out_dir, project.cfg)
    for folder in (train_dir, eval_dir):
        os.makedirs(folder, exist_ok=True)
    for path in existing_export_files(out_dir, project.cfg):
        os.remove(path)

    strip, written = project.params["strip_prefix"], {"train": 0, "eval": 0}
    for i, row in enumerate(rows, start=1):
        item = project.get_item(row["id"])
        split = "eval" if row["id"] in eval_ids else "train"
        stem = os.path.join(eval_dir if split == "eval" else train_dir, file_stem(row, strip))
        with open(stem + ".png", "wb") as f:
            f.write(item["image"])
        with open(stem + ".gt.txt", "w", encoding="utf-8", newline="\n") as f:
            f.write(item["label"] + "\n")
        written[split] += 1
        if progress:
            progress(i, len(rows))
    return {"train": written["train"], "eval": written["eval"], "folder": out_dir}
