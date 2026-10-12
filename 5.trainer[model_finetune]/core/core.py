import os
import re
import tomllib
from datetime import datetime


class ConfigError(RuntimeError):
    pass


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(_repo_root(), path)


def read_toml(path: str):
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
    return read_toml(resolve_path(path))


def require(cfg: dict, section: str, key: str):
    try:
        return cfg[section][key]
    except (KeyError, TypeError):
        raise ConfigError(f"config: missing key '{key}' in [{section}]") from None


def _key(name) -> str:
    name = str(name)
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _value(name)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + "}"
    text = str(v).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{text}"'


def dump_toml(table: dict) -> str:
    return "\n".join(f"{_key(k)} = {_value(v)}" for k, v in table.items()) + "\n"


def fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.2f} {unit}"
        n /= 1024


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def natural_sort_key(text) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", str(text))]
