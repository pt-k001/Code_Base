import importlib.util
import json
import os
import time
from dataclasses import dataclass

from .module_base import MetricModule


@dataclass
class LoadedModule:
    id: str
    instance: MetricModule
    filepath: str
    mtime: float
    enabled: bool
    load_error: str | None = None


class ModuleLoader:
    def __init__(self, modules_dir: str, state_path: str):
        self.modules_dir = modules_dir
        self.state_path = state_path
        self.modules: dict[str, LoadedModule] = {}
        self._saved_state = self._load_state()

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _save_state(self) -> None:
        state = {mid: lm.enabled for mid, lm in self.modules.items()}
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass

    def refresh(self) -> list[str]:
        changes = []
        if not os.path.isdir(self.modules_dir):
            return changes

        seen = set()
        for fname in sorted(os.listdir(self.modules_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            fpath = os.path.join(self.modules_dir, fname)
            mtime = os.path.getmtime(fpath)
            existing = next((lm for lm in self.modules.values() if lm.filepath == fpath), None)

            if existing is not None and existing.mtime == mtime:
                seen.add(existing.id)
                continue

            instance, err = self._import_module(fname, fpath)
            if instance is None:
                changes.append(f"Module load FAILED ({fname}): {err}")
                if existing is not None:
                    seen.add(existing.id)
                continue

            enabled = existing.enabled if existing else self._saved_state.get(instance.id, True)
            verb = "reloaded" if existing else "discovered"
            changes.append(f"Module {verb}: {instance.id} (v{instance.version})")
            self.modules[instance.id] = LoadedModule(
                id=instance.id, instance=instance, filepath=fpath, mtime=mtime, enabled=enabled)
            seen.add(instance.id)

        for mid, lm in self.modules.items():
            if mid not in seen and lm.load_error is None:
                lm.enabled = False
                lm.load_error = "module file removed"
                changes.append(f"Module file missing, disabled: {mid}")

        self._save_state()
        return changes

    @staticmethod
    def _import_module(fname: str, fpath: str):
        mod_name = f"modules.{os.path.splitext(fname)[0]}_{time.time_ns()}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, fpath)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            return None, f"import error: {exc}"

        candidate = getattr(module, "MODULE", None)
        if candidate is None:
            return None, "no module-level MODULE object found"
        if not isinstance(candidate, MetricModule):
            return None, "MODULE does not implement MetricModule"
        if not getattr(candidate, "id", None):
            return None, "MODULE.id is required"
        return candidate, None

    def set_enabled(self, mod_id: str, enabled: bool) -> None:
        if mod_id in self.modules:
            self.modules[mod_id].enabled = enabled
            self._save_state()

    def enabled_modules(self) -> list[MetricModule]:
        return [lm.instance for lm in self.modules.values() if lm.enabled and lm.load_error is None]

    def all_modules(self) -> list[LoadedModule]:
        return list(self.modules.values())
