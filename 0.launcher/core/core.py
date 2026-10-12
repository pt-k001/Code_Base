import ast
import datetime
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass

APP_NAME = "launcher"
VERSION_FILE = "version.toml"
CHANGELOG_TOML = "changelog.toml"
CHANGELOG_MD = "CHANGELOG.md"
VERSIONS_PER_MINOR = 10

REQUIRED_KEYS = {
    "paths": ("apps_dir",),
    "settings": ("appearance", "quit_after_open", "launch_check_ms", "max_commits_shown"),
    "gui": ("title", "window_geometry", "min_width", "min_height"),
}
_APP_DIR_RE = re.compile(r"^(\d+)\.")
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_VERSION_RE = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.\d+)*\s*$")
_VERSION_LINE_RE = re.compile(r"^version\s*=.*$", re.MULTILINE)

_config_path = None


class ConfigError(RuntimeError):
    pass


@dataclass
class AppInfo:
    folder: str
    path: str
    version: str
    doc_line: str


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
        for key in keys:
            if key not in table:
                raise ConfigError(f"config: missing key '{key}' in [{section}]")
    cfg.setdefault("descriptions", {})
    return cfg


def _toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.match(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value).replace("\x7f", ""), ensure_ascii=False)


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def save_config(cfg: dict) -> None:
    if not _config_path:
        return
    blocks = []
    for section, table in cfg.items():
        lines = [f"[{section}]"] + [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table.items()]
        blocks.append("\n".join(lines))
    try:
        _atomic_write(_config_path, "\n\n".join(blocks) + "\n")
    except OSError:
        pass


def apps_dir(cfg: dict) -> str:
    configured = cfg["paths"]["apps_dir"].strip()
    return resolve_path(configured) if configured else os.path.dirname(repo_root())


def _doc_line(main_py: str) -> str:
    try:
        with open(main_py, "r", encoding="utf-8") as f:
            doc = ast.get_docstring(ast.parse(f.read()))
    except (OSError, SyntaxError, ValueError):
        return ""
    return doc.strip().splitlines()[0] if doc else ""


def scan_apps(cfg: dict) -> list[AppInfo]:
    root = apps_dir(cfg)
    apps = []
    try:
        names = os.listdir(root)
    except OSError:
        return apps
    for name in names:
        path = os.path.join(root, name)
        main_py = os.path.join(path, "main.py")
        if _APP_DIR_RE.match(name) and os.path.isfile(main_py):
            apps.append(AppInfo(name, path, read_version(os.path.join(path, VERSION_FILE)),
                                _doc_line(main_py)))
    return sorted(apps, key=lambda a: (int(_APP_DIR_RE.match(a.folder).group(1)), a.folder))


def is_self(app: AppInfo) -> bool:
    return os.path.normcase(os.path.abspath(app.path)) == os.path.normcase(repo_root())


# ── launching ─────────────────────────────────────────────────────────────────
def launch_app(app: AppInfo):
    exe = sys.executable
    flags = 0
    if os.name == "nt":
        windowed = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = windowed if os.path.exists(windowed) else exe
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        [exe, "main.py"], cwd=app.path, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=err, creationflags=flags, start_new_session=(os.name != "nt"))
    return proc, err


def launch_error(proc, err) -> str | None:
    code = proc.poll()
    if code is None or code == 0:
        err.close()
        return None
    err.seek(0)
    text = err.read().decode("utf-8", errors="replace").strip()
    err.close()
    return text[-1500:] or f"The app exited with code {code}."


# ── versions ──────────────────────────────────────────────────────────────────
def next_version(current: str) -> str:
    if not current:
        return "1.0"
    match = _VERSION_RE.match(current)
    if not match:
        raise ValueError(f"Cannot read version '{current}'.")
    major, minor = int(match.group(1)), int(match.group(2) or 0) + 1
    if minor >= VERSIONS_PER_MINOR:
        major, minor = major + 1, 0
    return f"{major}.{minor}"


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _quote(text: str) -> str:
    return json.dumps(text.replace("\x7f", ""), ensure_ascii=False)


def _toml_entry(entry: dict) -> str:
    return ("[[commit]]\n" + "".join(f"{k} = {_quote(entry[k])}\n"
                                      for k in ("time", "user", "from", "to", "note")))


def _md_entry(entry: dict) -> str:
    stamp = entry["time"].replace("T", " ")[:16]
    return f"## {entry['to']} - {stamp} ({entry['user']})\n\n{entry['note']}\n\n"


def read_commits(app_dir: str) -> list[dict]:
    path = os.path.join(app_dir, CHANGELOG_TOML)
    if not os.path.exists(path):
        return []
    items = read_toml(path).get("commit", [])
    return [{k: str(item.get(k, "")) for k in ("time", "user", "from", "to", "note")}
            for item in items if isinstance(item, dict)]


def all_commits(apps: list[AppInfo]) -> tuple[list[dict], list[str]]:
    commits, problems = [], []
    for app in apps:
        try:
            commits += [{**c, "app": app.folder} for c in read_commits(app.path)]
        except ConfigError as exc:
            problems.append(str(exc))
    commits.sort(key=lambda c: c["time"], reverse=True)
    return commits, problems


def _version_text(path: str, new: str) -> str:
    text = _read_text(path)
    line = f'version = "{new}"'
    if text is None:
        return line + "\n"
    if _VERSION_LINE_RE.search(text):
        return _VERSION_LINE_RE.sub(lambda _m: line, text, count=1)
    return text.rstrip("\n") + "\n" + line + "\n"


def _changelog_toml_text(path: str, entry: dict) -> str:
    text = _read_text(path) or ""
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + _toml_entry(entry)


def _changelog_md_text(app_dir: str, entry: dict) -> str:
    text = _read_text(os.path.join(app_dir, CHANGELOG_MD))
    block = _md_entry(entry)
    if text is None:
        history = sorted(read_commits(app_dir) + [entry], key=lambda c: c["time"], reverse=True)
        return "# Changelog\n\n" + "".join(_md_entry(c) for c in history)
    header = re.match(r"^# .*\n+", text)
    if header:
        return text[:header.end()] + block + text[header.end():]
    return block + text


def apply_bump(app: AppInfo, note: str) -> dict:
    note = note.strip()
    if not note:
        raise ValueError("A change log note is required.")
    new = next_version(app.version)
    entry = {"time": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
             "user": getpass.getuser(), "from": app.version, "to": new, "note": note}

    ver_path = os.path.join(app.path, VERSION_FILE)
    toml_path = os.path.join(app.path, CHANGELOG_TOML)
    md_path = os.path.join(app.path, CHANGELOG_MD)
    files = {ver_path: _version_text(ver_path, new),
             toml_path: _changelog_toml_text(toml_path, entry),
             md_path: _changelog_md_text(app.path, entry)}
    originals = {path: _read_text(path) for path in files}

    done = []
    try:
        for path, text in files.items():
            _atomic_write(path, text)
            done.append(path)
    except OSError as exc:
        for path in done:
            if originals[path] is None:
                os.remove(path)
            else:
                _atomic_write(path, originals[path])
        raise OSError(f"Bump rolled back: {exc}") from None
    return entry
