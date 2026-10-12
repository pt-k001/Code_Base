import re
from core.module_base import MetricModule, ChartSpec

FIELD_MAP = {
    "MemTotal": "MemTotal", "MemFree": "MemFree", "MemAvailable": "MemAvailable",
    "Buffers": "Buffers", "Cached": "Cached", "SwapCached": "SwapCached",
    "Active": "Active", "Inactive": "Inactive",
    "Active(anon)": "Active_anon", "Inactive(anon)": "Inactive_anon",
    "Active(file)": "Active_file", "Inactive(file)": "Inactive_file",
    "Unevictable": "Unevictable", "Mlocked": "Mlocked",
    "SwapTotal": "SwapTotal", "SwapFree": "SwapFree",
    "Dirty": "Dirty", "Writeback": "Writeback",
    "AnonPages": "AnonPages", "Mapped": "Mapped", "Shmem": "Shmem",
    "Slab": "Slab", "SReclaimable": "SReclaimable", "SUnreclaim": "SUnreclaim",
    "KernelStack": "KernelStack", "PageTables": "PageTables",
    "NFS_Unstable": "NFS_Unstable", "Bounce": "Bounce", "WritebackTmp": "WritebackTmp",
    "CommitLimit": "CommitLimit", "Committed_AS": "Committed_AS",
    "VmallocTotal": "VmallocTotal", "VmallocUsed": "VmallocUsed",
    "VmallocChunk": "VmallocChunk",
}
LINE_RE = re.compile(r"^\s*([A-Za-z_()]+):\s+(\d+)\s*kB\s*$")


class MemInfoModule(MetricModule):
    id = "meminfo"
    name = "Memory Info (/proc/meminfo)"
    version = "1.0"

    db_schema = {"mem_pct": "REAL", **{c: "INTEGER" for c in FIELD_MAP.values()}}

    chart_specs = [
        ChartSpec(key="mem_pct", title="Memory Used (%)", ylabel="%",
                  value_col="mem_pct", ylim=(0, 100)),
    ]

    def matches(self, cmd: str) -> bool:
        return cmd == "cat /proc/meminfo"

    def parse(self, body: str, ts):
        mem = {}
        for line in body.splitlines():
            m = LINE_RE.match(line.strip())
            if not m:
                continue
            col = FIELD_MAP.get(m.group(1))
            if col is None:
                continue
            try:
                mem[col] = int(m.group(2))
            except ValueError:
                pass

        total = mem.get("MemTotal", 0)
        free = mem.get("MemFree")
        if total <= 0 or free is None:
            return None

        mem_pct = 100.0 * (1 - free / total)
        row = {"mem_pct": mem_pct}
        for col in FIELD_MAP.values():
            row[col] = mem.get(col)
        return row

    def excel_rename(self) -> dict:
        rename = {"mem_pct": "Memory_Percent"}
        rename.update({c: f"{c}_kB" for c in FIELD_MAP.values()})
        return rename


MODULE = MemInfoModule()
