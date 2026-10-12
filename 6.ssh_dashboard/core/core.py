import json
import os
import tomllib

APP_NAME = "ssh_dashboard"

REQUIRED_KEYS = {
    "paths": ("log_file", "database_file", "chart_file"),
    "storage": ("db_file", "state_file"),
    "feed": ("progress_lines", "max_gap_samples"),
    "refresh": ("default", "queue_poll_ms"),
    "refresh_options": (),
    "charts": ("default_window", "height", "group_height", "inactive_after_min", "png_dpi"),
    "time_windows": (),
    "gui": ("title", "window_geometry", "min_width", "min_height", "tree_width", "log_height"),
}


class ConfigError(RuntimeError):
    pass


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(repo_root(), path)


def service_file(kind: str, name: str) -> str:
    folder = os.path.join(repo_root(), "Service", kind, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


def read_toml(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None


def read_version(path: str) -> str:
    try:
        version = read_toml(path).get("version")
    except ConfigError:
        return ""
    return str(version).strip() if version else ""


def load_config(path: str) -> dict:
    global _config_path
    _config_path = resolve_path(path)
    cfg = read_toml(_config_path)
    for section, keys in REQUIRED_KEYS.items():
        table = cfg.get(section)
        if not isinstance(table, dict):
            raise ConfigError(f"config: missing section [{section}]")
        if not keys and not table:
            raise ConfigError(f"config: section [{section}] is empty")
        for key in keys:
            if key not in table:
                raise ConfigError(f"config: missing key '{key}' in [{section}]")
    if cfg["refresh"]["default"] not in cfg["refresh_options"]:
        raise ConfigError("config: [refresh] default is not in [refresh_options]")
    if cfg["charts"]["default_window"] not in cfg["time_windows"]:
        raise ConfigError("config: [charts] default_window is not in [time_windows]")
    return cfg


_config_path = None


def remember_path(key: str, value: str) -> None:
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


def dialog_start(path: str) -> dict:
    folder = os.path.dirname(path) if path else ""
    if folder and os.path.isdir(folder):
        return {"initialdir": folder, "initialfile": os.path.basename(path)}
    return {}
