import re
from core.module_base import MetricModule, ChartSpec

LOAD_RE = re.compile(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)")

UPTIME_PATTERNS = [
    (re.compile(r"up\s+(\d+)\s+days?,\s+(\d+):(\d+):(\d+)"),
     lambda m: int(m.group(1)) * 86400 + int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))),
    (re.compile(r"up\s+(\d+)\s+days?,\s+(\d+):(\d+)"),
     lambda m: int(m.group(1)) * 86400 + int(m.group(2)) * 3600 + int(m.group(3)) * 60),
    (re.compile(r"up\s+(\d+)\s+days?,\s+(\d+)\s+min"),
     lambda m: int(m.group(1)) * 86400 + int(m.group(2)) * 60),
    (re.compile(r"up\s+(\d+):(\d+):(\d+)"),
     lambda m: int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))),
    (re.compile(r"up\s+(\d+):(\d+)"),
     lambda m: int(m.group(1)) * 3600 + int(m.group(2)) * 60),
    (re.compile(r"up\s+(\d+)\s+min"),
     lambda m: int(m.group(1)) * 60),
]


def parse_uptime_seconds(text: str):
    for pattern, to_seconds in UPTIME_PATTERNS:
        m = pattern.search(text)
        if m:
            return to_seconds(m)
    return None


class UptimeModule(MetricModule):
    id = "uptime"
    name = "Uptime / Load Average"
    version = "1.0"

    db_schema = {"uptime_sec": "INTEGER", "load1": "REAL", "load5": "REAL", "load15": "REAL"}

    chart_specs = [
        ChartSpec(key="uptime_sec", title="System Uptime (s)", ylabel="seconds",
                  value_col="uptime_sec", ylim=None),
        ChartSpec(key="load1", title="Load Average (1 min)", ylabel="load",
                  value_col="load1", ylim=None),
        ChartSpec(key="load5", title="Load Average (5 min)", ylabel="load",
                  value_col="load5", ylim=None),
        ChartSpec(key="load15", title="Load Average (15 min)", ylabel="load",
                  value_col="load15", ylim=None),
    ]

    def matches(self, cmd: str) -> bool:
        return cmd.strip() == "uptime"

    def parse(self, body: str, ts):
        text = body.strip()
        if not text:
            return None

        uptime_sec = parse_uptime_seconds(text)
        load1 = load5 = load15 = None
        m = LOAD_RE.search(text)
        if m:
            try:
                load1, load5, load15 = float(m.group(1)), float(m.group(2)), float(m.group(3))
            except ValueError:
                pass

        if uptime_sec is None and load1 is None:
            return None

        return {"uptime_sec": uptime_sec, "load1": load1, "load5": load5, "load15": load15}

    def excel_rename(self) -> dict:
        return {"uptime_sec": "Uptime_Seconds", "load1": "Load_1min", "load5": "Load_5min", "load15": "Load_15min"}


MODULE = UptimeModule()
