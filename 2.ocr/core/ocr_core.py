import base64
import os
import queue
import re
import sqlite3
import threading
import time
from contextlib import closing
from functools import lru_cache
from multiprocessing import Process, Queue, Value, cpu_count
from pathlib import Path

import cv2
import numpy as np
import psutil

from .core import CONFIDENCE_IDX, natural_sort_key, require, resolve_path

CORES_TOTAL = cpu_count()
ERR_READ_FAIL = "Error: Read Fail"
NOT_AVAILABLE = "N/A"


_INTERP = {"nearest": cv2.INTER_NEAREST, "linear": cv2.INTER_LINEAR,
           "cubic": cv2.INTER_CUBIC, "lanczos": cv2.INTER_LANCZOS4}


PARAM_SPECS = [
    ("naming", "Naming & decoding", [
        ("gray_mode", "Gray mode", "choice", ["min", "lum"]),
        ("strip_prefix", "Strip prefix", "str", None),
    ]),
    ("ocr", "OCR", [
        ("lang", "Language", "str", None),
        ("psm", "PSM", "int", None),
        ("stage", "Stage (single pass)", "choice", ["gray", "otsu", "enlarge_otsu"]),
        ("regex", "Validation regex", "str", None),
    ]),
    ("workers_memory", "Workers & memory", [
        ("workers", "Workers (0 = auto)", "int", None),
        ("chunk_size", "Chunk size (0 = auto)", "int", None),
        ("queue_maxsize", "Queue max size", "int", None),
        ("max_ram_percent", "Max RAM %", "float", None),
        ("worker_timeout", "Worker timeout (s)", "float", None),
    ]),
    ("image_prepare", "Image: prepare", [
        ("img_crop", "Crop edges (px)", "int", None),
        ("img_invert", "Invert", "bool", None),
        ("img_scale", "Scale", "float", None),
        ("img_interp", "Interpolation", "choice", list(_INTERP)),
        ("img_denoise", "Denoise", "choice", ["none", "median", "gaussian", "bilateral", "nlmeans"]),
        ("img_denoise_size", "Denoise size", "int", None),
        ("img_contrast", "Contrast", "choice", ["none", "clahe", "normalize"]),
        ("img_clahe_clip", "CLAHE clip", "float", None),
        ("img_gamma", "Gamma", "float", None),
        ("img_sharpen", "Sharpen", "float", None),
    ]),
    ("image_threshold", "Image: threshold", [
        ("img_threshold", "Method", "choice",
         ["otsu", "triangle", "adaptive_gaussian", "adaptive_mean", "fixed"]),
        ("img_thr_fixed", "Fixed level", "int", None),
        ("img_thr_block", "Adaptive block", "int", None),
        ("img_thr_c", "Adaptive C", "float", None),
        ("img_morph", "Morphology", "choice",
         ["none", "thicken", "thin", "despeckle_dark", "despeckle_light"]),
        ("img_morph_size", "Morphology size", "int", None),
        ("img_pad", "Border pad (px)", "int", None),
        ("img_enlarge", "Enlarge factor", "float", None),
    ]),
]

_NON_NEGATIVE = ("img_crop", "img_pad", "img_denoise_size", "img_thr_block", "img_morph_size",
                 "workers", "chunk_size", "queue_maxsize")
_POSITIVE = (("img_scale", "Scale"), ("img_enlarge", "Enlarge factor"),
             ("img_gamma", "Gamma"), ("worker_timeout", "Worker timeout"))


def default_params(cfg):
    return {key: require(cfg, section, key)
            for section, _title, items in PARAM_SPECS
            for key, *_ in items}


def _coerce_value(value, label, kind, choices):
    try:
        if kind == "int":
            return int(str(value).strip())
        if kind == "float":
            return float(str(value).strip())
        if kind == "bool":
            return bool(value)
        if kind == "choice":
            value = str(value)
            if value not in choices:
                raise ValueError("invalid choice")
            return value
        return str(value)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from None


def coerce_params(raw, cfg):
    defaults = default_params(cfg)
    params = {key: _coerce_value(raw.get(key, defaults[key]), label, kind, choices)
              for _section, _title, items in PARAM_SPECS
              for key, label, kind, choices in items}
    _validate_params(params)
    return params


def _validate_params(p):
    try:
        re.compile(p["regex"])
    except re.error as exc:
        raise ValueError(f"Validation regex: {exc}") from None
    if not 0 < p["max_ram_percent"] <= 100:
        raise ValueError("Max RAM % must be in (0, 100]")
    for key, label in _POSITIVE:
        if p[key] <= 0:
            raise ValueError(f"{label} must be greater than 0")
    if not 0 <= p["img_thr_fixed"] <= 255:
        raise ValueError("Fixed level must be 0-255")
    for key in _NON_NEGATIVE:
        if p[key] < 0:
            raise ValueError(f"{key} cannot be negative")


def build_options(params, cfg):
    return {**params,
            **cfg["image_tuning"],
            **cfg["ocr_engine"],
            "tessdata_dir": resolve_path(cfg["paths"]["tessdata_dir"]),
            "db_timeout_s": cfg["database"]["busy_timeout_s"]}


def open_db_readonly(db_path, timeout):
    try:
        return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                               uri=True, timeout=timeout)
    except sqlite3.OperationalError:
        return sqlite3.connect(db_path, timeout=timeout)


def find_databases(root, cfg):
    settings = cfg["database"]
    exts, skip = tuple(settings["extensions"]), set(settings["skip_dirs"])
    max_depth = settings["max_scan_depth"]
    root = os.path.abspath(root)
    found = []
    for dirpath, dirnames, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth < max_depth:
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in skip)
        else:
            dirnames[:] = []
        for name in sorted(files):
            if not name.lower().endswith(exts):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            found.append({"path": path, "rel": os.path.relpath(path, root),
                          "size": st.st_size, "mtime": st.st_mtime})
    return found


def make_column_name(session, region, strip_prefix):
    s = session
    if strip_prefix and s.startswith(strip_prefix):
        s = s[len(strip_prefix):]
    s = s.strip("_")
    r = re.sub(r"\s+", "_", region.strip())
    return f"{s}_{r}" if s else r


def scan_tasks(db_path, strip_prefix, cfg):
    with closing(open_db_readonly(db_path, cfg["database"]["busy_timeout_s"])) as conn:
        rows = conn.execute("""
            SELECT c.id, c.session, c.region, c.seq FROM captures c
            JOIN (SELECT session, MAX(version) AS v FROM captures GROUP BY session) m
              ON c.session = m.session AND c.version = m.v
            ORDER BY c.session, c.region, c.seq""").fetchall()
    by_col = {}
    for row_id, session, region, seq in rows:
        by_col.setdefault(make_column_name(session, region, strip_prefix), []).append((seq, row_id))
    tasks, columns = [], {}
    for col in sorted(by_col, key=natural_sort_key):
        items = sorted(by_col[col])
        columns[col] = len(items)
        tasks += [(col, seq - 1, rid, f"{seq:04d}.png") for seq, rid in items]
    return tasks, columns


def get_db_info(db_path, strip_prefix, cfg):
    st = os.stat(db_path)
    with closing(open_db_readonly(db_path, cfg["database"]["busy_timeout_s"])) as conn:
        has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                 "AND name='captures'").fetchone()
        if not has_table:
            raise ValueError("This database has no 'captures' table.")
        rows = conn.execute("""
            SELECT session, version, region, COUNT(*), MIN(seq), MAX(seq), MIN(id)
            FROM captures GROUP BY session, version, region
            ORDER BY session, version, region""").fetchall()
    newest = {}
    for session, version, *_ in rows:
        newest[session] = max(newest.get(session, version), version)
    groups = [{"session": s, "version": v, "region": rg, "count": n, "min_seq": a,
               "max_seq": b, "gaps": max(0, (b - a + 1) - n), "first_id": fid,
               "latest": v == newest[s]} for s, v, rg, n, a, b, fid in rows]
    tasks, columns = scan_tasks(db_path, strip_prefix, cfg)
    return {"path": db_path, "size": st.st_size, "mtime": st.st_mtime,
            "groups": groups, "tasks": tasks, "columns": columns}


def decode_png_gray(blob, gray_mode):
    try:
        if not blob:
            return None
        buf = np.frombuffer(blob, dtype=np.uint8)
        if buf.size == 0:
            return None
        if gray_mode == "lum":
            return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return None if bgr is None else np.ascontiguousarray(bgr.min(axis=2))
    except Exception:
        return None


def fetch_gray(conn, row_id, gray_mode):
    row = conn.execute("SELECT image FROM captures WHERE id = ?", (row_id,)).fetchone()
    return decode_png_gray(row[0], gray_mode) if row else None


def _odd(n, lo=3):
    n = max(lo, int(n))
    return n if n % 2 else n + 1


def _crop(img, margin):
    margin = int(margin)
    if margin > 0 and img.shape[0] > 2 * margin + 2 and img.shape[1] > 2 * margin + 2:
        return img[margin:-margin, margin:-margin]
    return img


def _denoise(img, o):
    mode, k = o["img_denoise"], _odd(o["img_denoise_size"])
    if mode == "median":
        return cv2.medianBlur(img, k)
    if mode == "gaussian":
        return cv2.GaussianBlur(img, (k, k), 0)
    if mode == "bilateral":
        return cv2.bilateralFilter(img, k, o["bilateral_sigma"], o["bilateral_sigma"])
    if mode == "nlmeans":
        return cv2.fastNlMeansDenoising(img, None, h=o["nlmeans_strength"])
    return img


def _contrast(img, o):
    mode = o["img_contrast"]
    if mode == "clahe":
        tile = int(o["clahe_tile"])
        return cv2.createCLAHE(clipLimit=float(o["img_clahe_clip"]),
                               tileGridSize=(tile, tile)).apply(img)
    if mode == "normalize":
        return cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX)
    return img


def _gamma(img, gamma):
    if gamma == 1.0:
        return img
    lut = ((np.arange(256) / 255.0) ** (1.0 / gamma) * 255).astype(np.uint8)
    return cv2.LUT(img, lut)


def _sharpen(img, amount, sigma):
    if amount <= 0:
        return img
    return cv2.addWeighted(img, 1 + amount, cv2.GaussianBlur(img, (0, 0), sigma), -amount, 0)


def apply_base_options(gray, o):
    img = _crop(gray, o["img_crop"])
    if o["img_invert"]:
        img = cv2.bitwise_not(img)
    scale = float(o["img_scale"])
    if scale != 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=_INTERP[o["img_interp"]])
    img = _denoise(img, o)
    img = _contrast(img, o)
    img = _gamma(img, float(o["img_gamma"]))
    img = _sharpen(img, float(o["img_sharpen"]), o["sharpen_sigma"])
    return np.ascontiguousarray(img)


def _morph(img, mode, size):
    kernel = np.ones((size, size), np.uint8)
    if mode == "thicken":
        return cv2.erode(img, kernel)
    if mode == "thin":
        return cv2.dilate(img, kernel)
    if mode == "despeckle_dark":
        return cv2.morphologyEx(img, cv2.MORPH_CLOSE, kernel)
    if mode == "despeckle_light":
        return cv2.morphologyEx(img, cv2.MORPH_OPEN, kernel)
    return img


def _binarize(img, o):
    method = o["img_threshold"]
    if method in ("adaptive_gaussian", "adaptive_mean"):
        adaptive = (cv2.ADAPTIVE_THRESH_GAUSSIAN_C if method == "adaptive_gaussian"
                    else cv2.ADAPTIVE_THRESH_MEAN_C)
        binary = cv2.adaptiveThreshold(img, 255, adaptive, cv2.THRESH_BINARY,
                                       _odd(o["img_thr_block"]), float(o["img_thr_c"]))
    elif method == "triangle":
        binary = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_TRIANGLE)[1]
    elif method == "fixed":
        binary = cv2.threshold(img, int(o["img_thr_fixed"]), 255, cv2.THRESH_BINARY)[1]
    else:
        binary = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    return _morph(binary, o["img_morph"], max(1, int(o["img_morph_size"])))


def render_stage(gray, stage, o):
    if stage == "otsu":
        proc = _binarize(gray, o)
    elif stage == "enlarge_otsu":
        factor = float(o["img_enlarge"])
        enlarged = cv2.resize(gray, None, fx=factor, fy=factor,
                              interpolation=_INTERP[o["enlarge_interp"]])
        proc = _binarize(enlarged, o)
    else:
        proc = gray
    pad = int(o["img_pad"])
    if pad > 0:
        proc = cv2.copyMakeBorder(proc, pad, pad, pad, pad, cv2.BORDER_CONSTANT,
                                  value=o["pad_value"])
    return np.ascontiguousarray(proc)


def _fit_for_display(img, max_w, max_h):
    h, w = img.shape[:2]
    scale = min(max_w / w, max_h / h)
    if scale >= 1:
        factor = int(scale)
        return cv2.resize(img, None, fx=factor, fy=factor, interpolation=cv2.INTER_NEAREST)
    return cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                      interpolation=cv2.INTER_AREA)


def make_preview(db_path, row_id, params, cfg, stage, max_w, max_h):
    o = build_options(params, cfg)
    with closing(open_db_readonly(db_path, o["db_timeout_s"])) as conn:
        meta = conn.execute("SELECT session, region, seq, version FROM captures WHERE id=?",
                            (row_id,)).fetchone()
        gray = fetch_gray(conn, row_id, o["gray_mode"])
    if gray is None or meta is None:
        raise ValueError("image could not be read/decoded")
    proc = render_stage(apply_base_options(gray, o), stage, o)
    png = cv2.imencode(".png", _fit_for_display(proc, max_w, max_h))[1]
    h, w = proc.shape[:2]
    return (base64.b64encode(png.tobytes()).decode("ascii"),
            {"session": meta[0], "region": meta[1], "seq": meta[2], "version": meta[3],
             "w": w, "h": h, "stage": stage, "id": row_id})


_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _tesserocr():
    try:
        import tesserocr
    except ImportError as exc:
        raise RuntimeError("tesserocr is not installed (pip install tesserocr)") from exc
    return tesserocr


@lru_cache(maxsize=1)
def _psm_table():
    psm = _tesserocr().PSM
    return {int(getattr(psm, k)): getattr(psm, k) for k in dir(psm) if k.isupper()}


def _psm_enum(value, fallback):
    table = _psm_table()
    return table.get(int(value), table[int(fallback)])


def new_api(o):
    api = _tesserocr().PyTessBaseAPI(path=o["tessdata_dir"], lang=o["lang"])
    if o["whitelist"]:
        api.SetVariable("tessedit_char_whitelist", o["whitelist"])
    return api


def extract_number(text):
    match = _NUMBER_RE.search(text or "")
    return match.group(0) if match else ""


def validate_text(text, pattern):
    try:
        return bool(re.fullmatch(pattern, text.strip()))
    except re.error:
        return True


def _conf_mean(api):
    return float(api.MeanTextConf())


def _conf_words(api):
    values = [c for c in (api.AllWordConfidences() or []) if c > 0]
    return sum(values) / len(values) if values else 0.0


def _conf_iterator(api):
    levels = _tesserocr().RIL
    for level in (levels.SYMBOL, levels.WORD, levels.TEXTLINE):
        values, it = [], api.GetIterator()
        if it is not None:
            while True:
                c = it.Confidence(level)
                if c is not None and c > 0:
                    values.append(c)
                if not it.Next(level):
                    break
        if values:
            return sum(values) / len(values)
    return 0.0


def _read_confidence(api):
    for reader in (_conf_mean, _conf_words, _conf_iterator):
        try:
            conf = reader(api)
        except Exception:
            continue
        if conf > 0:
            return conf
    return 0.0


def single_pass_ocr(api, gray, o):
    api.SetPageSegMode(_psm_enum(o["psm"], o["fallback_psm"]))
    proc = np.ascontiguousarray(render_stage(gray, o["stage"], o), dtype=np.uint8)
    h, w = proc.shape
    api.SetImageBytes(proc.tobytes(), w, h, 1, w)
    api.Recognize()
    text = (api.GetUTF8Text() or "").strip()
    confidence = _read_confidence(api)
    api.Clear()
    regex_ok = validate_text(text, o["regex"])
    return {"text": text, "confidence": confidence, "preprocess": o["stage"],
            "regex_ok": regex_ok,
            "suspicious": confidence < float(o["suspicious_confidence"]) or not regex_ok}


class _PreviewOcr:

    def __init__(self):
        self._lock = threading.Lock()
        self._api = None
        self._key = None

    def read(self, db_path, row_id, o):
        key = (o["lang"], o["tessdata_dir"], o["whitelist"])
        with self._lock:
            if self._api is None or self._key != key:
                self._end()
                self._api, self._key = new_api(o), key
            with closing(open_db_readonly(db_path, o["db_timeout_s"])) as conn:
                gray = fetch_gray(conn, row_id, o["gray_mode"])
            if gray is None:
                raise ValueError("image could not be read/decoded")
            return single_pass_ocr(self._api, apply_base_options(gray, o), o)

    def close(self):
        with self._lock:
            self._end()

    def _end(self):
        if self._api is not None:
            try:
                self._api.End()
            except Exception:
                pass
            self._api = self._key = None


_preview_ocr = _PreviewOcr()


def ocr_preview(db_path, row_id, params, cfg):
    r = _preview_ocr.read(db_path, row_id, build_options(params, cfg))
    return {"text": extract_number(r["text"]), "confidence": r["confidence"],
            "preprocess": r["preprocess"], "suspicious": r["suspicious"]}


def release_preview():
    _preview_ocr.close()


def system_usage():
    mem = psutil.virtual_memory()
    return {"ram_percent": mem.percent, "ram_used": mem.used, "ram_total": mem.total,
            "cpu_percent": psutil.cpu_percent(interval=None), "cores_total": CORES_TOTAL}


class ResourceController:

    def __init__(self, max_ram_percent, start_chunk, runtime):
        self.max_ram = max_ram_percent
        self.min_chunk = runtime["min_chunk"]
        self.max_chunk = max(runtime["max_chunk_floor"], start_chunk * runtime["max_chunk_factor"])
        self.shrink = runtime["chunk_shrink_factor"]
        self.grow = runtime["chunk_grow_factor"]
        self.band = runtime["ram_hysteresis_percent"]
        self.chunk, self.paused = start_chunk, False

    def sample_and_adjust(self):
        used = psutil.virtual_memory().percent
        self.paused = used >= self.max_ram
        if self.paused:
            self.chunk = max(self.min_chunk, int(self.chunk * self.shrink))
        elif used < self.max_ram - self.band:
            self.chunk = min(self.max_chunk, int(self.chunk * self.grow) + 1)


def _ocr_task(api, conn, task, o):
    col, idx, row_id, filename = task
    try:
        gray = fetch_gray(conn, row_id, o["gray_mode"])
        if gray is None:
            return (col, idx, filename, ERR_READ_FAIL, 0.0, NOT_AVAILABLE, True)
        r = single_pass_ocr(api, apply_base_options(gray, o), o)
        return (col, idx, filename, extract_number(r["text"]),
                r["confidence"], r["preprocess"], r["suspicious"])
    except Exception as exc:
        return (col, idx, filename, f"Error: {exc}", 0.0, NOT_AVAILABLE, True)


def _send(q, item, stop_flag, timeout):
    while not stop_flag.value:
        try:
            q.put(item, timeout=timeout)
            return True
        except queue.Full:
            continue
    return False


def worker_loop(wid, in_q, out_q, stop_flag, db_path, o, runtime):
    api = conn = None
    put_timeout = runtime["queue_put_timeout_s"]
    try:
        api = new_api(o)
        conn = open_db_readonly(db_path, o["db_timeout_s"])
        while not stop_flag.value:
            try:
                chunk = in_q.get(timeout=runtime["poll_interval_s"])
            except queue.Empty:
                continue
            if chunk is None:
                break
            rows = []
            for task in chunk:
                if stop_flag.value:
                    break
                rows.append(_ocr_task(api, conn, task, o))
            if rows:
                _send(out_q, (wid, rows), stop_flag, put_timeout)
    except Exception as exc:
        try:
            out_q.put((wid, f"Worker {wid} failed: {type(exc).__name__}: {exc}"), timeout=put_timeout)
        except Exception:
            pass
    finally:
        for closer in (getattr(api, "End", None), getattr(conn, "close", None)):
            try:
                closer and closer()
            except Exception:
                pass


class OcrRunner:
    ACTIVE = ("running", "stopping")

    def __init__(self, db_path, params, tasks, cfg):
        self.db_path, self.tasks = db_path, list(tasks)
        self.opts = build_options(params, cfg)
        self._rt = cfg["runtime"]
        self.state, self.error = "idle", None
        self.processed, self.total = 0, len(self.tasks)
        self.low_confidence = 0
        self.rows = []
        self.stop_flag = Value("b", False)
        self.finished = threading.Event()
        self._workers, self._task_queues, self._out_q, self._ctrl = [], [], None, None

    @property
    def is_active(self):
        return self.state in self.ACTIVE

    def progress(self):
        return self.processed, self.total

    def snapshot(self):
        return {"state": self.state,
                "paused": bool(self._ctrl and self._ctrl.paused),
                "processed": self.processed, "total": self.total,
                "low_confidence": self.low_confidence,
                "workers_used": sum(1 for w in self._workers if w.is_alive()),
                "cores_total": CORES_TOTAL}

    def start(self):
        self.state = "running"
        threading.Thread(target=self._guard, daemon=True).start()

    def stop(self):
        if self.is_active:
            self.stop_flag.value = True
            self.state = "stopping"

    def _guard(self):
        try:
            self._run()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "error"
        finally:
            self.finished.set()

    def _collect(self, rows):
        self.rows.extend(rows)
        threshold = float(self.opts["suspicious_confidence"])
        self.low_confidence += sum(1 for row in rows if row[CONFIDENCE_IDX] < threshold)
        self.processed = len(self.rows)

    def _put(self, q, item):
        return _send(q, item, self.stop_flag, self._rt["queue_put_timeout_s"])

    def _feed(self, ctrl):
        batch, k, last = [], 0, len(self.tasks) - 1
        for i, task in enumerate(self.tasks):
            while ctrl.paused and not self.stop_flag.value:
                time.sleep(self._rt["pause_poll_s"])
            if self.stop_flag.value:
                return
            batch.append(task)
            if len(batch) >= ctrl.chunk or i == last:
                if not self._put(self._task_queues[k % len(self._task_queues)], batch):
                    return
                batch, k = [], k + 1
        for q in self._task_queues:
            self._put(q, None)

    def _monitor(self, ctrl, done):
        while not done.wait(self._rt["resource_sample_s"]):
            ctrl.sample_and_adjust()

    def _collect_loop(self, feeder):
        timeout = self.opts["worker_timeout"]
        last_progress = time.time()
        while self.processed < self.total and not self.stop_flag.value:
            try:
                _wid, payload = self._out_q.get(timeout=self._rt["poll_interval_s"])
            except queue.Empty:
                if not any(w.is_alive() for w in self._workers) and not feeder.is_alive():
                    break
                if not self._ctrl.paused and time.time() - last_progress > timeout:
                    self.error = f"No progress for {timeout:.0f}s."
                    break
                continue
            last_progress = time.time()
            if isinstance(payload, str):
                self.error = payload
                break
            self._collect(payload)

    def _shutdown(self, feeder):
        self.stop_flag.value = True
        while True:
            try:
                _wid, payload = self._out_q.get(timeout=self._rt["drain_timeout_s"])
            except queue.Empty:
                break
            if not isinstance(payload, str):
                self._collect(payload)
        feeder.join(timeout=self._rt["feeder_join_timeout_s"])
        for w in self._workers:
            w.join(timeout=self._rt["join_timeout_s"])
            if w.is_alive():
                w.terminate()

    def _run(self):
        p, rt = self.opts, self._rt
        if not self.total:
            raise RuntimeError("No images found in database.")
        n_workers = p["workers"] or max(1, CORES_TOTAL - rt["reserved_cores"])
        chunk = p["chunk_size"] or max(rt["auto_chunk_min"], min(
            rt["auto_chunk_max"],
            self.total // max(1, n_workers * rt["auto_chunk_batches_per_worker"])))
        self._ctrl = ctrl = ResourceController(p["max_ram_percent"], chunk, rt)
        queue_size = max(rt["min_queue_size"], p["queue_maxsize"])
        self._out_q = Queue(maxsize=queue_size * n_workers)
        self._task_queues = [Queue(maxsize=queue_size) for _ in range(n_workers)]
        self._workers = [Process(target=worker_loop, daemon=True,
                                 args=(i, self._task_queues[i], self._out_q, self.stop_flag,
                                       self.db_path, p, rt))
                         for i in range(n_workers)]
        for w in self._workers:
            w.start()
        feeder = threading.Thread(target=self._feed, args=(ctrl,), daemon=True)
        feeder.start()
        monitor_done = threading.Event()
        threading.Thread(target=self._monitor, args=(ctrl, monitor_done), daemon=True).start()

        user_stopped = False
        try:
            self._collect_loop(feeder)
            user_stopped = self.state == "stopping"
        finally:
            monitor_done.set()
            self._shutdown(feeder)

        if self.error:
            self.state = "error"
        elif user_stopped:
            self.state = "stopped"
        elif self.processed < self.total:
            self.state = "incomplete"
        else:
            self.state = "done"
