import datetime
import math
import re
from pathlib import Path

import pandas as pd

NON_METRIC = ("filename", "channel", "tx_power")


def _quiet(_msg):
    pass


def _combo_name(ch, pwr) -> str:
    return f"ch{int(ch)}_pwr{int(pwr)}"


def _enabled(filters) -> list:
    return [f for f in (filters or []) if f["enabled"]]


def _bounded(filters) -> list:
    return [f for f in _enabled(filters) if f["min"] is not None or f["max"] is not None]


def metric_columns(pivot_df: pd.DataFrame) -> list:
    return [c for c in pivot_df.columns if c not in NON_METRIC]


def _normalize_header(name) -> str:
    return re.sub(r"[\s\-]+", "_", str(name).strip().lower())


def _find_filename_column(df: pd.DataFrame, aliases) -> str:
    if "filename" in df.columns:
        return "filename"
    normalized = {_normalize_header(c): c for c in df.columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    raise KeyError("Could not find a filename column. Expected one of "
                   f"{sorted(aliases)}. Found: {list(df.columns)[:8]} ...")


def load_table(path: str, sheet_name: str, aliases) -> tuple:
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        raw = pd.read_csv(path)
    elif ext in (".xlsx", ".xlsm"):
        names = pd.ExcelFile(path, engine="openpyxl").sheet_names
        raw = pd.read_excel(path, sheet_name=sheet_name if sheet_name in names else names[0],
                            engine="openpyxl")
    else:
        raise ValueError(f"Unsupported file type '{ext}'. Use xlsx or csv.")

    raw = raw.rename(columns={_find_filename_column(raw, aliases): "filename"})
    df = raw.copy()
    df["filename"] = df["filename"].astype(str)
    bad = 0
    for col in df.columns:
        if col == "filename":
            continue
        original = df[col]
        numeric = pd.to_numeric(original, errors="coerce")
        bad += int((original.notna() & numeric.isna()).sum())
        df[col] = numeric
    return df, bad


def _tags(col):
    ch = re.search(r"ch(\d+)", str(col), re.IGNORECASE)
    pwr = re.search(r"pwr(\d+)", str(col), re.IGNORECASE)
    return (int(ch.group(1)), int(pwr.group(1))) if ch and pwr else None


def _metric_of(col) -> str:
    m = re.sub(r"ch\d+_?", "", str(col), flags=re.IGNORECASE)
    m = re.sub(r"pwr\d+_?", "", m, flags=re.IGNORECASE)
    return m.strip("_")


def extract_metric_names(columns) -> list:
    metrics, seen = [], set()
    for col in columns:
        if col == "filename" or _tags(col) is None:
            continue
        m = _metric_of(col)
        if m and m not in seen:
            seen.add(m)
            metrics.append(m)
    return metrics


def reshape_data(df: pd.DataFrame, log=_quiet) -> pd.DataFrame:
    long_rows, skipped = [], []
    for col in df.columns:
        if col == "filename":
            continue
        tags = _tags(col)
        metric = _metric_of(col) if tags else ""
        if tags is None or not metric:
            skipped.append(col)
            continue
        long_rows.append(pd.DataFrame({
            "filename": df["filename"], "channel": float(tags[0]),
            "tx_power": float(tags[1]), "metric": metric, "value": df[col]}))
    if skipped:
        log(f"  Skipped {len(skipped)} column(s) without ch##/pwr## tags.")
    if not long_rows:
        raise ValueError("No columns with ch##/pwr## tags found.")
    long_df = pd.concat(long_rows, ignore_index=True)
    long_df["value"] = pd.to_numeric(long_df["value"], errors="coerce")
    key = ["filename", "channel", "tx_power", "metric"]
    dupes = long_df.duplicated(subset=key, keep=False)
    if dupes.any():
        log(f"  [!] {long_df[dupes].groupby(key).ngroups} duplicate metric combos - first kept.")
    pivot = long_df.pivot_table(index=["filename", "channel", "tx_power"],
                                columns="metric", values="value",
                                aggfunc="first").reset_index()
    pivot.columns.name = None
    return pivot


def _sorted_groups(pivot_df):
    return sorted(pivot_df.groupby(["channel", "tx_power"]),
                  key=lambda t: (float(t[0][0]), float(t[0][1])))


def split_groups(pivot_df: pd.DataFrame) -> dict:
    groups = _sorted_groups(pivot_df)
    if not groups:
        raise ValueError("No channel/power groups found after reshape.")
    return {_combo_name(ch, pwr): g.reset_index(drop=True) for (ch, pwr), g in groups}


def describe_filters(filters: list) -> list:
    out = []
    for f in _enabled(filters):
        lo = f["min"] if f["min"] is not None else "-inf"
        hi = f["max"] if f["max"] is not None else "+inf"
        out.append(f"{f['col']}  [{lo} ... {hi}]")
    return out


def _sheet_sort_key(name: str):
    return _tags(name) or (9999, 9999)


def filter_best_rows(sheets: dict, filters: list, sort_col, top_n: int,
                     ascending: bool, log=_quiet) -> list:
    active = _enabled(filters)
    order = "ascending" if ascending else "descending"
    if not active:
        log(f"No filters enabled - all rows kept (Top-{top_n} still applied).")
    else:
        log(f"Filtering: {'; '.join(describe_filters(active))}  |  "
            f"Top-{top_n} per combo, sorted {order}")
    summary_rows = []
    for sheet, df in sorted(sheets.items(), key=lambda kv: _sheet_sort_key(kv[0])):
        if df.empty:
            continue
        missing = [f["col"] for f in active if f["col"] not in df.columns]
        if missing:
            log(f"  [!] {sheet}: columns not found: {missing} - their filters skipped.")
        mask, _fails, _per = _combo_mask(df, active)
        filtered = df[mask].copy()
        if filtered.empty:
            continue
        use = sort_col
        if use is None or use not in filtered.columns:
            cands = [f["col"] for f in active if f["col"] in filtered.columns]
            use = cands[0] if cands else None
        if use:
            filtered = filtered.sort_values(by=use, ascending=ascending,
                                            na_position="last", kind="stable")
        for rank, (_, row) in enumerate(filtered.head(top_n).iterrows(), start=1):
            tagged = row.copy()
            tagged["source_sheet"] = sheet
            tagged["rank"] = rank
            summary_rows.append(tagged)
    return summary_rows


def build_summary_df(summary_rows: list) -> pd.DataFrame:
    if not summary_rows:
        return pd.DataFrame()
    df = pd.DataFrame(summary_rows)
    lead = [c for c in ("source_sheet", "rank") if c in df.columns]
    return df[lead + [c for c in df.columns if c not in lead]].reset_index(drop=True)


def run_pipeline(df: pd.DataFrame, filters: list, sort_col, top_n: int,
                 ascending: bool, log=_quiet, pivot=None) -> dict:
    log(f"Using {len(df):,} rows, {len(df.columns) - 1} columns")
    if pivot is None:
        pivot = reshape_data(df, log)
    log(f"Reshaped: {len(pivot):,} rows")
    sheets = split_groups(pivot)
    log(f"Split into {len(sheets)} channel/power combinations")
    rows = filter_best_rows(sheets, filters, sort_col, top_n, ascending, log)
    summary_df = build_summary_df(rows)
    n_pass = int(summary_df["source_sheet"].nunique()) if not summary_df.empty else 0
    log(f"Combinations with a passing row: {n_pass}/{len(sheets)}   "
        f"summary rows: {len(summary_df)}")
    return {"pivot": pivot, "sheets": sheets, "sheet_names": list(sheets),
            "summary_df": summary_df, "n_sheets": len(sheets), "n_pass": n_pass,
            "finished": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def dataset_overview(df: pd.DataFrame) -> list:
    cols = [c for c in df.columns if c != "filename"]
    tagged = [c for c in cols if _tags(c)]
    combos = {_tags(c) for c in tagged}
    chs = sorted({t[0] for t in combos})
    pws = sorted({t[1] for t in combos})
    metrics = extract_metric_names(df.columns)
    cells = int(df[cols].size) if cols else 0
    blank = int(df[cols].isna().sum().sum()) if cols else 0

    def short(xs, n=12):
        return ", ".join(map(str, xs[:n])) + (" ..." if len(xs) > n else "")
    return [
        ("Data", "Rows", f"{len(df):,}"),
        ("Data", "Data columns", f"{len(cols):,}"),
        ("Data", "ch##/pwr##-tagged columns", f"{len(tagged):,}"),
        ("Data", "Untagged columns (ignored)", f"{len(cols) - len(tagged):,}"),
        ("Structure", "Channels", f"{len(chs)}:  {short(chs)}"),
        ("Structure", "TX powers", f"{len(pws)}:  {short(pws)}"),
        ("Structure", "ch/pwr combos", f"{len(combos):,}"),
        ("Structure", "Metrics", f"{len(metrics)}:  {short(metrics, 6)}"),
        ("Quality", "Cells total", f"{cells:,}"),
        ("Quality", "Blank cells",
         f"{blank:,}  ({100 * blank / cells:.2f}%)" if cells else "0"),
    ]


def spec_text(lo, hi) -> str:
    if lo is not None and hi is not None:
        return f"{lo:g} to {hi:g}"
    if hi is not None:
        return f"<= {hi:g}"
    if lo is not None:
        return f">= {lo:g}"
    return ""


def metric_stats(pivot_df: pd.DataFrame, filters=None) -> list:
    bounds = {f["col"]: (f["min"], f["max"]) for f in _bounded(filters)}
    rows = []
    for c in metric_columns(pivot_df):
        s = pd.to_numeric(pivot_df[c], errors="coerce")
        v = s.dropna()
        n = len(v)
        r = dict(
            metric=c, valid=n, blank=len(s) - n,
            blank_pct=100.0 * (len(s) - n) / len(s) if len(s) else 0.0,
            min=v.min() if n else None, max=v.max() if n else None,
            mean=v.mean() if n else None, median=v.median() if n else None,
            std=v.std() if n > 1 else None,
            p05=v.quantile(0.05) if n else None, p95=v.quantile(0.95) if n else None,
            spec="", in_spec=None, margin_med=None, margin_worst=None)
        if c in bounds and n:
            lo, hi = bounds[c]
            ok = pd.Series(True, index=v.index)
            worst, med = [], []
            if lo is not None:
                ok &= v >= lo
                worst.append(v.min() - lo)
                med.append(v.median() - lo)
            if hi is not None:
                ok &= v <= hi
                worst.append(hi - v.max())
                med.append(hi - v.median())
            r.update(spec=spec_text(lo, hi), in_spec=100.0 * ok.mean(),
                     margin_med=min(med), margin_worst=min(worst))
        rows.append(r)
    return rows


def _combo_mask(g: pd.DataFrame, active: list):
    mask = pd.Series(True, index=g.index)
    fails, per = {}, {}
    for f in active:
        if f["col"] not in g.columns:
            continue
        c = pd.to_numeric(g[f["col"]], errors="coerce")
        m = pd.Series(True, index=g.index)
        if f["min"] is not None:
            m &= c >= f["min"]
        if f["max"] is not None:
            m &= c <= f["max"]
        per[f["col"]] = m
        fails[f["col"]] = int((~m).sum())
        mask &= m
    return mask, fails, per


def combo_report(pivot_df, filters, sort_col=None, ascending=True):
    active = _enabled(filters)
    if sort_col is None or sort_col not in pivot_df.columns:
        sort_col = next((f["col"] for f in active if f["col"] in pivot_df.columns), None)
    rows = []
    for (ch, pwr), g in _sorted_groups(pivot_df):
        mask, fails, _ = _combo_mask(g, active)
        total, n_pass = len(g), int(mask.sum())
        status = ("All pass" if total and n_pass == total
                  else "Partial" if n_pass else "None pass")
        r = dict(name=_combo_name(ch, pwr), channel=int(ch), power=int(pwr),
                 total=total, n_pass=n_pass, status=status,
                 pass_pct=100.0 * n_pass / total if total else 0.0, fails=fails,
                 best=None, mean_pass=None, median_all=None, best_file="", best_vals={})
        if sort_col:
            v = pd.to_numeric(g[sort_col], errors="coerce")
            va, vp = v.dropna(), v[mask].dropna()
            if len(va):
                r["median_all"] = va.median()
            if len(vp):
                idx = vp.idxmin() if ascending else vp.idxmax()
                r["best"] = vp[idx]
                r["mean_pass"] = vp.mean()
                r["best_file"] = str(g.loc[idx, "filename"])
                r["best_vals"] = {f["col"]: g.loc[idx, f["col"]]
                                  for f in active if f["col"] in g.columns}
        rows.append(r)
    return rows, sort_col


def filter_impact(pivot_df: pd.DataFrame, filters: list) -> dict:
    active = [f for f in _enabled(filters) if f["col"] in pivot_df.columns]
    total = len(pivot_df)
    mask, fails, per = _combo_mask(pivot_df, active)
    items = []
    for f in active:
        others = pd.Series(True, index=pivot_df.index)
        for k, m in per.items():
            if k != f["col"]:
                others &= m
        v = pd.to_numeric(pivot_df[f["col"]], errors="coerce").dropna()
        need_lo = v.min() if (f["min"] is not None and len(v)) else None
        need_hi = v.max() if (f["max"] is not None and len(v)) else None
        only = int((~per[f["col"]] & others).sum())
        items.append(dict(metric=f["col"], spec=spec_text(f["min"], f["max"]) or "no bound",
                          fails=fails[f["col"]],
                          pct=100.0 * fails[f["col"]] / total if total else 0.0,
                          only=only, only_pct=100.0 * only / total if total else 0.0,
                          limit_all=spec_text(need_lo, need_hi)))
    items.sort(key=lambda i: -i["fails"])
    return dict(total=total, n_pass=int(mask.sum()), items=items)


def matrix_from_report(rows: list, key: str):
    chs = sorted({r["channel"] for r in rows})
    pws = sorted({r["power"] for r in rows})
    return chs, pws, {(r["channel"], r["power"]): r[key] for r in rows}


def build_distribution(pivot_df, metric, bin_width, filters) -> list:
    if metric not in pivot_df.columns:
        raise KeyError(f"Metric '{metric}' not found in the data.")
    if bin_width <= 0:
        raise ValueError("Bin width must be greater than 0.")
    active = _enabled(filters)
    groups = [("All combos", pivot_df)]
    groups += [(_combo_name(ch, pwr), g) for (ch, pwr), g in _sorted_groups(pivot_df)]
    out = []
    for name, g in groups:
        valid = pd.to_numeric(g[metric], errors="coerce").dropna()
        mask, _fails, _per = _combo_mask(g, active)
        combo = dict(name=name, total=len(g), valid=len(valid), n_pass=int(mask.sum()),
                     vmin=valid.min() if len(valid) else None,
                     vmedian=valid.median() if len(valid) else None,
                     vmax=valid.max() if len(valid) else None, bins=[])
        if len(valid):
            lo = math.floor(valid.min() / bin_width) * bin_width
            idx = ((valid - lo) / bin_width).map(math.floor).astype(int)
            counts = idx.value_counts()
            for i in range(int(idx.max()) + 1):
                n = int(counts.get(i, 0))
                start = round(lo + i * bin_width, 6)
                combo["bins"].append((start, round(start + bin_width, 6), n,
                                      100.0 * n / len(valid)))
        out.append(combo)
    return out


def analyze(pivot_df, filters, sort_col=None, ascending=True) -> dict:
    combos, used = combo_report(pivot_df, filters, sort_col, ascending)
    return dict(metric_stats=metric_stats(pivot_df, filters), combos=combos, sort_col=used,
                ascending=ascending, impact=filter_impact(pivot_df, filters),
                metrics=metric_columns(pivot_df),
                active_cols=[f["col"] for f in _enabled(filters)
                             if f["col"] in pivot_df.columns])


def headline(a: dict) -> str:
    imp, combos = a["impact"], a["combos"]
    tot = imp["total"]
    parts = [f"{imp['n_pass']:,} of {tot:,} rows pass ({100.0 * imp['n_pass'] / tot if tot else 0:.1f}%)",
             f"{sum(1 for c in combos if c['n_pass'])}/{len(combos)} combos have a passing row"]
    ranked = [c for c in combos if c["best"] is not None]
    if ranked:
        b = (min if a["ascending"] else max)(ranked, key=lambda c: c["best"])
        parts.append(f"best {b['name']} {a['sort_col'] or ''} {b['best']:.2f}")
    if combos:
        w = min(combos, key=lambda c: c["pass_pct"])
        parts.append(f"weakest {w['name']} ({w['pass_pct']:.0f}%)")
    if imp["items"] and imp["items"][0]["fails"]:
        t = imp["items"][0]
        parts.append(f"top blocker {t['metric']} ({t['pct']:.0f}% fail)")
    return "   |   ".join(parts)
