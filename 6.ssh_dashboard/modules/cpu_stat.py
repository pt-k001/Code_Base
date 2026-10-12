from core.module_base import MetricModule, ChartSpec

FIELDS = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]


class CpuStatModule(MetricModule):
    id = "cpu_stat"
    name = "CPU Stat (/proc/stat)"
    version = "1.0"

    db_schema = {"cpu_pct": "REAL", **{f: "INTEGER" for f in FIELDS}}

    chart_specs = [
        ChartSpec(key="cpu_pct", title="CPU Load (%)", ylabel="%",
                  value_col="cpu_pct", ylim=(0, 100)),
    ]

    def __init__(self):
        self._last_raw = None

    def matches(self, cmd: str) -> bool:
        return cmd == "cat /proc/stat"

    def parse(self, body: str, ts):
        raw = None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("cpu "):
                vals = line.split()[1:]
                raw = {}
                for i, f in enumerate(FIELDS):
                    try:
                        raw[f] = int(vals[i]) if i < len(vals) else 0
                    except (ValueError, IndexError):
                        raw[f] = 0
                break
        if raw is None:
            return None

        cpu_pct = None
        if self._last_raw is not None:
            deltas = {f: raw[f] - self._last_raw[f] for f in FIELDS}
            total = sum(deltas.values())
            if total > 0:
                cpu_pct = 100.0 * (1 - (deltas["idle"] / total))
        self._last_raw = raw

        return {"cpu_pct": cpu_pct, **raw}

    def excel_rename(self) -> dict:
        rename = {"cpu_pct": "CPU_Percent"}
        rename.update({f: f"{f}_jiffies" for f in FIELDS})
        return rename

    def reset(self):
        self._last_raw = None


MODULE = CpuStatModule()
