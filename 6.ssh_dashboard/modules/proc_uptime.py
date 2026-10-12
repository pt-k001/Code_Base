import re
from core.module_base import MetricModule, ChartSpec

PROC_UPTIME_RE = re.compile(r"^\s*([\d.]+)\s+([\d.]+)\s*$")


class ProcUptimeModule(MetricModule):
    id = "proc_uptime"
    name = "Uptime (/proc/uptime)"
    version = "1.0"

    db_schema = {"uptime_sec": "REAL", "idle_sec": "REAL"}

    chart_specs = [
        ChartSpec(key="uptime_sec", title="System Uptime, /proc (s)", ylabel="seconds",
                  value_col="uptime_sec", ylim=None),
        ChartSpec(key="idle_sec", title="Cumulative Idle Time (s)", ylabel="seconds",
                  value_col="idle_sec", ylim=None),
    ]

    def matches(self, cmd: str) -> bool:
        return cmd.strip() == "cat /proc/uptime"

    def parse(self, body: str, ts):
        text = body.strip()
        if not text:
            return None

        m = PROC_UPTIME_RE.match(text)
        if not m:
            return None

        try:
            uptime_sec = float(m.group(1))
            idle_sec = float(m.group(2))
        except ValueError:
            return None

        return {"uptime_sec": uptime_sec, "idle_sec": idle_sec}

    def excel_rename(self) -> dict:
        return {"uptime_sec": "Uptime_Seconds", "idle_sec": "Idle_Seconds"}


MODULE = ProcUptimeModule()
