import json
import os
import re
import sys
import tomllib

APP_NAME = "ssh_log"

REQUIRED_KEYS = {
    "paths": ("log_file", "key_file"),
    "ssh": ("connect_timeout", "command_timeout", "max_output_bytes", "keepalive_s",
            "read_chunk", "poll_sleep_s", "reconnect_pause_s"),
    "limits": ("min_interval_s", "max_interval_s", "default_interval_s", "max_profile_name",
               "max_username", "max_command", "max_command_rows"),
    "reconnect": ("max_attempts", "delay_s", "watchdog_ms"),
    "viewer": ("max_view_lines", "trim_lines", "max_block_chars", "max_memory_blocks",
               "keep_memory_blocks", "preload_lines", "tail_chunk_bytes", "poll_ms",
               "max_events_per_tick"),
    "gui": ("title", "window_geometry", "min_width", "min_height"),
}


_config_path = None


class ConfigError(RuntimeError):
    pass


def _helper_repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def helper_resolve_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(_helper_repo_root(), path)


def helper_service_file(kind: str, name: str) -> str:
    folder = os.path.join(_helper_repo_root(), "Service", kind, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


def helper_read_toml(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None


def helper_read_version(path: str) -> str:
    try:
        version = helper_read_toml(path).get("version")
    except ConfigError:
        return ""
    return str(version).strip() if version else ""


def helper_load_config(path: str) -> dict:
    global _config_path
    _config_path = helper_resolve_path(path)
    cfg = helper_read_toml(_config_path)
    for section, keys in REQUIRED_KEYS.items():
        table = cfg.get(section)
        if not isinstance(table, dict):
            raise ConfigError(f"config: missing section [{section}]")
        for key in keys:
            if key not in table:
                raise ConfigError(f"config: missing key '{key}' in [{section}]")
    return cfg


def helper_remember_path(key: str, value: str) -> None:
    if not _config_path or not value:
        return
    line = f"{key} = {json.dumps(value)}"
    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        start = next(i for i, text in enumerate(lines) if text.strip() == "[paths]")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        for i in range(start + 1, end):
            if lines[i].split("=")[0].strip() == key:
                lines[i] = line
                break
        else:
            lines.insert(start + 1, line)
        tmp = _config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, _config_path)
    except (OSError, StopIteration):
        pass


def helper_dialog_start(path: str) -> dict:
    folder = os.path.dirname(path) if path else ""
    if folder and os.path.isdir(folder):
        return {"initialdir": folder, "initialfile": os.path.basename(path)}
    return {}


# ── DPI awareness / scaling ───────────────────────────────────────────────────
def helper_enable_dpi_awareness() -> bool:
    """Make the process DPI aware on Windows. Must run BEFORE the first Tk window.

    System-DPI awareness is used on purpose: Tk does not handle WM_DPICHANGED,
    so per-monitor awareness would leave widgets mis-sized after a window is
    dragged to a monitor with a different scale factor.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
    except ImportError:
        return False
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # Win 8.1+
        return True
    except (AttributeError, OSError):
        pass
    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())   # Vista+
    except (AttributeError, OSError):
        return False


def helper_dpi_scale(widget) -> float:
    """Return the display scale factor (1.0 == 96 DPI). Never below 1.0."""
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return 1.0


def helper_scale_px(value, scale) -> int:
    return max(1, int(round(float(value) * float(scale))))


def helper_scale_geometry(geometry: str, scale) -> str:
    """Scale the WxH part of a Tk geometry string; keep any +x+y offset as is."""
    match = re.match(r"^\s*(\d+)x(\d+)(.*)$", str(geometry))
    if not match:
        return str(geometry)
    width, height, rest = match.groups()
    return f"{helper_scale_px(width, scale)}x{helper_scale_px(height, scale)}{rest.strip()}"
