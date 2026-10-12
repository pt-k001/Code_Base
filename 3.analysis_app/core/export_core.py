import os

import pandas as pd

from .analysis_core import describe_filters


def _write_excel(path: str, sheets: dict) -> None:
    try:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            for name, frame in sheets.items():
                frame.to_excel(writer, sheet_name=name, index=False)
    except PermissionError as exc:
        raise PermissionError(f"Cannot write '{os.path.basename(path)}' - is it open in Excel?") from exc


def _settings_frame(result: dict, params: dict) -> pd.DataFrame:
    rows = [("Source", params["source"]),
            ("Finished", result["finished"]),
            ("Combinations", result["n_sheets"]),
            ("Combinations with a passing row", result["n_pass"]),
            ("Top-N per combination", params["top_n"]),
            ("Rank by", params["sort_col"] or "(first enabled filter)"),
            ("Order", "ascending (lowest first)" if params["ascending"]
             else "descending (highest first)")]
    lines = describe_filters(params["filters"])
    rows += [("Filter", line) for line in lines] or [("Filter", "none")]
    return pd.DataFrame(rows, columns=["Setting", "Value"])


def export_results(result: dict, params: dict, out_dir: str, stem: str,
                   write_reshaped: bool, write_summary: bool, log) -> dict:
    paths = {"reshaped": None, "summary": None}
    if not (write_reshaped or write_summary):
        log("No files selected - nothing written.")
        return paths
    os.makedirs(out_dir, exist_ok=True)

    if write_reshaped:
        path = os.path.join(out_dir, f"{stem}_reshaped.xlsx")
        _write_excel(path, result["sheets"])
        paths["reshaped"] = path
        log(f"Wrote {path}  ({result['n_sheets']} sheets)")

    if write_summary:
        summary = result["summary_df"]
        if summary.empty:
            log("No rows pass the filters - summary workbook not written.")
        else:
            path = os.path.join(out_dir, f"{stem}_summary.xlsx")
            _write_excel(path, {"SUMMARY": summary,
                                "SETTINGS": _settings_frame(result, params)})
            paths["summary"] = path
            log(f"Wrote {path}  ({len(summary)} rows)")
    return paths
