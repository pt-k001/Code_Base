import os
import tomllib


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

