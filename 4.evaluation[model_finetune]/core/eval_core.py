import difflib
import os
import re
from collections import Counter

import cv2
import numpy as np


def normalize(text):
    return " ".join(str(text or "").split())


def find_pairs(folder):
    pairs = []
    for root, _dirs, files in os.walk(folder):
        present = set(files)
        for name in sorted(files):
            if name.lower().endswith(".gt.txt") and name[:-7] + ".png" in present:
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    pairs.append((os.path.join(root, name[:-7] + ".png"), normalize(f.read())))
    return pairs


def list_models(tessdata_dir):
    try:
        return sorted(n[:-12] for n in os.listdir(tessdata_dir) if n.endswith(".traineddata"))
    except OSError:
        return []


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def mistakes(truth, reading):
    found = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, truth, reading,
                                                       autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            found += [f"{t}->{r}" for t, r in zip(truth[i1:i2], reading[j1:j2])]
        else:
            found.append(f"{truth[i1:i2] or chr(8709)}->{reading[j1:j2] or chr(8709)}")
    return found


def first_number(text, pattern):
    match = re.search(pattern, text or "")
    return match.group(0) if match else ""


def read_gray(path):
    # np.fromfile + imdecode: cv2.imread cannot open non-ASCII Windows paths
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)


def _tesserocr():
    try:
        import tesserocr
    except ImportError as exc:
        raise RuntimeError("tesserocr is not installed (pip install tesserocr)") from exc
    return tesserocr


def evaluate(pairs, lang, tessdata_dir, psm, whitelist, cfg, progress=None, stop=None):
    tesserocr = _tesserocr()
    modes = {int(getattr(tesserocr.PSM, k)): getattr(tesserocr.PSM, k)
             for k in dir(tesserocr.PSM) if k.isupper()}
    psm_mode = modes.get(int(psm), modes[int(cfg["engine"]["fallback_psm"])])
    pattern = cfg["engine"]["number_regex"]
    api = tesserocr.PyTessBaseAPI(path=tessdata_dir, lang=lang)
    if whitelist:
        api.SetVariable("tessedit_char_whitelist", whitelist)
    res = {"lang": lang, "n": 0, "exact": 0, "value_n": 0, "value_ok": 0, "edits": 0,
           "ref_chars": 0, "unreadable": 0, "stopped": False, "errors": [],
           "confusions": Counter()}
    try:
        for done, (path, truth) in enumerate(pairs, start=1):
            if stop is not None and stop.is_set():
                res["stopped"] = True
                break
            img = read_gray(path)
            if img is None:
                res["unreadable"] += 1
                continue
            h, w = img.shape
            api.SetPageSegMode(psm_mode)
            api.SetImageBytes(np.ascontiguousarray(img).tobytes(), w, h, 1, w)
            api.Recognize()
            reading = normalize(api.GetUTF8Text())
            api.Clear()

            res["n"] += 1
            res["edits"] += edit_distance(truth, reading)
            res["ref_chars"] += len(truth)
            if reading == truth:
                res["exact"] += 1
            else:
                res["errors"].append((os.path.basename(path), truth, reading))
                res["confusions"].update(mistakes(truth, reading))
            want = first_number(truth, pattern)
            if want:
                res["value_n"] += 1
                res["value_ok"] += first_number(reading, pattern) == want
            if progress and (done % 5 == 0 or done == len(pairs)):
                progress(done)
    finally:
        try:
            api.End()
        except Exception:
            pass
    return res


def percent(part, whole):
    return 100.0 * part / whole if whole else 0.0
