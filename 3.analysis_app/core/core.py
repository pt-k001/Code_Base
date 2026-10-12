import os
import re
import tomllib


_CONFIG_PATH = ""


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
    global _CONFIG_PATH
    path = resolve_path(path)
    cfg = read_toml(path)
    _CONFIG_PATH = path
    return cfg


def load_state() -> dict:
    try:
        state = read_toml(_CONFIG_PATH).get("state")
    except ConfigError:
        return {}
    return state if isinstance(state, dict) else {}


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


def _dump(table: dict, prefix: str = "") -> list:
    lines = [f"{_key(k)} = {_value(v)}" for k, v in table.items() if not isinstance(v, dict)]
    for k, v in table.items():
        if isinstance(v, dict):
            name = f"{prefix}.{_key(k)}" if prefix else _key(k)
            lines += ["", f"[{name}]"] + _dump(v, name)
    return lines


def save_state(**values) -> None:
    try:
        data = read_toml(_CONFIG_PATH)
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        state.update(values)
        data["state"] = state
        text = "\n".join(_dump(data)).lstrip("\n") + "\n"
        with open(_CONFIG_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except (OSError, ConfigError):
        pass
