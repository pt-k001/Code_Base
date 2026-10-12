import os

import pandas as pd

from .core import RESULT_COLS, natural_sort_key

FORMATS = ("xlsx", "csv")


class ExportTooLarge(ValueError):
    pass


def rows_to_wide(rows) -> pd.DataFrame:
    raw = pd.DataFrame(rows, columns=RESULT_COLS)
    wide = raw.pivot_table(index="index", columns="folder", values="text", aggfunc="first")
    filenames = raw.sort_values("index").drop_duplicates("index").set_index("index")["filename"]
    wide.insert(0, "filename", filenames)
    wide = wide.reset_index(drop=True)
    wide.columns.name = None

    for col in wide.columns[1:]:
        text = wide[col]
        numeric = pd.to_numeric(text, errors="coerce")
        keep_text = text.notna() & numeric.isna() & (text.astype(str) != "")
        wide[col] = numeric.astype(object).where(~keep_text, text) if keep_text.any() else numeric

    columns = ["filename"] + sorted((c for c in wide.columns if c != "filename"),
                                    key=natural_sort_key)
    return wide[columns]


def _target_path(output_path: str, fmt: str) -> str:
    base, ext = os.path.splitext(output_path)
    if ext.lower().lstrip(".") not in FORMATS:
        base = output_path
    return f"{base}.{fmt}"


def export_results(rows, output_path: str, fmt: str, cfg: dict) -> str:
    if not rows:
        raise ValueError("There are no OCR results to export.")
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format '{fmt}'.")

    settings = cfg["export"]
    wide = rows_to_wide(rows)
    limit = int(settings["xlsx_row_limit"])
    if fmt == "xlsx" and len(wide) > limit:
        raise ExportTooLarge(f"{len(wide):,} rows do not fit in one Excel sheet "
                             f"(limit {limit:,}). Choose csv instead.")

    out = _target_path(output_path, fmt)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    try:
        if fmt == "xlsx":
            wide.to_excel(out, index=False, sheet_name=settings["sheet_name"], engine="openpyxl")
        else:
            wide.to_csv(out, index=False)
    except PermissionError as exc:
        raise PermissionError(f"Cannot write '{os.path.basename(out)}' - is it open in Excel?") from exc
    return out
