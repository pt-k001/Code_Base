"""OCR engine used by the dataset maker: parameters, capture database, image processing, Tesseract.

Every tunable comes from the config file; this module holds no numeric defaults.

    default_params / coerce_params   OCR parameter form
    find_databases / scan_tasks      database discovery, work list of the newest session versions
    make_preview                     what Tesseract sees
    single_pass_ocr                  one OCR pass on a prepared image
"""
import base64
import os
import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from .core import natural_sort_key, require, resolve_path


# ══ parameters ═══════════════════════════════════════════════════════════════
# (key, label, kind, choices)   kind: str | int | float | bool | choice
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
    ("image_prepare", "Image: prepare", [
        ("img_crop", "Crop edges (px)", "int", None),
        ("img_invert", "Invert", "bool", None),
        ("img_scale", "Scale", "float", None),
        ("img_interp", "Interpolation", "choice", ["nearest", "linear", "cubic", "lanczos"]),
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

_NON_NEGATIVE = ("img_crop", "img_pad", "img_denoise_size", "img_thr_block", "img_morph_size")
_POSITIVE = (("img_scale", "Scale"), ("img_enlarge", "Enlarge factor"), ("img_gamma", "Gamma"))


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


# ══ database ═════════════════════════════════════════════════════════════════
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


# ══ images ═══════════════════════════════════════════════════════════════════
_INTERP = {"nearest": cv2.INTER_NEAREST, "linear": cv2.INTER_LINEAR,
           "cubic": cv2.INTER_CUBIC, "lanczos": cv2.INTER_LANCZOS4}


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


# ══ OCR ══════════════════════════════════════════════════════════════════════


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


def _run_pass(api, gray, o):
    api.SetPageSegMode(_psm_enum(o["psm"], o["fallback_psm"]))
    proc = np.ascontiguousarray(render_stage(gray, o["stage"], o), dtype=np.uint8)
    h, w = proc.shape
    api.SetImageBytes(proc.tobytes(), w, h, 1, w)
    api.Recognize()                         # run recognition once, explicitly
    text = (api.GetUTF8Text() or "").strip()
    confidence = _read_confidence(api)
    api.Clear()
    return {"text": text, "confidence": confidence, "preprocess": o["stage"]}


def single_pass_ocr(api, gray, o):
    result = _run_pass(api, gray, o)
    result["regex_ok"] = validate_text(result["text"], o["regex"])
    result["suspicious"] = (result["confidence"] < float(o["suspicious_confidence"])
                            or not result["regex_ok"])
    return result
