import re

import pandas as pd

_INVALID_SHEET_CHARS = re.compile(r"[\\/*\[\]:?]")
_MAX_SHEET_LEN = 31


def _safe_sheet_name(name: str, used: set) -> str:
    base = _INVALID_SHEET_CHARS.sub("_", name).strip()[:_MAX_SHEET_LEN] or "Sheet"
    safe, i = base, 1
    while safe in used:
        suffix = f"_{i}"
        safe = base[:_MAX_SHEET_LEN - len(suffix)] + suffix
        i += 1
    used.add(safe)
    return safe


def _split_datetime(df: pd.DataFrame) -> pd.DataFrame:
    parsed = pd.to_datetime(df["time"], errors="coerce")
    keep = parsed.notna()
    out = df.loc[keep].copy()
    parsed = parsed.loc[keep]
    out.insert(0, "Date", parsed.dt.date)
    out.insert(1, "Time", parsed.dt.time)
    return out.drop(columns=["time"])


def export_to_excel(path: str, db, modules) -> None:
    used = set()
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        sheets = 0
        for module in modules:
            df = db.read_all_df(module.id)
            if df.empty:
                continue
            df = _split_datetime(df).rename(columns=module.excel_rename())
            df.to_excel(writer, sheet_name=_safe_sheet_name(module.name or module.id, used), index=False)
            sheets += 1
        if not sheets:
            raise ValueError("No data to export.")
