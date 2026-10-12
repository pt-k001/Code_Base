import re
from core.module_base import MetricModule, ChartSpec

STATION_RE = re.compile(r"^Station\s+([0-9a-fA-F:]{17})\s+\(on\s+([\w.-]+)\)")
CMD_RE = re.compile(r"^iw dev \S+ station dump$")

MIN_SAMPLE_INTERVAL_SEC = 1.0
SANITY_MAX_MBPS = 2000.0


def _first_int(text):
    m = re.search(r"-?\d+", text)
    return int(m.group(0)) if m else None


class WifiStationModule(MetricModule):
    id = "wifi_station"
    name = "Wi-Fi Stations (iw station dump)"
    version = "1.0"

    db_schema = {
        "mac": "TEXT", "iface": "TEXT",
        "rx_bytes": "INTEGER", "tx_bytes": "INTEGER",
        "rx_mbps": "REAL", "tx_mbps": "REAL",
        "signal_dbm": "INTEGER", "signal_avg_dbm": "INTEGER",
        "connected_sec": "INTEGER", "inactive_ms": "INTEGER",
        "tx_retries": "INTEGER", "tx_failed": "INTEGER", "rx_drop_misc": "INTEGER",
    }

    chart_specs = [
        ChartSpec(key="rx_mbps", title="{group} - RX Speed", ylabel="Mbps",
                  value_col="rx_mbps", ylim=None, group_by="mac"),
        ChartSpec(key="tx_mbps", title="{group} - TX Speed", ylabel="Mbps",
                  value_col="tx_mbps", ylim=None, group_by="mac"),
        ChartSpec(key="signal_avg_dbm", title="{group} - Signal Avg", ylabel="dBm",
                  value_col="signal_avg_dbm", ylim=(-100, 0), group_by="mac"),
        ChartSpec(key="connected_sec", title="{group} - Connected Duration", ylabel="seconds",
                  value_col="connected_sec", ylim=None, group_by="mac"),
    ]

    def __init__(self):
        self._last_sample = {}
        self._clamp_msgs = {}

    def matches(self, cmd: str) -> bool:
        return bool(CMD_RE.match(cmd.strip()))

    def parse(self, body: str, ts):
        stations = self._parse_dump(body)
        rows = []
        for st in stations:
            mac = st["mac"]
            rx_mbps = tx_mbps = None
            prev = self._last_sample.get(mac)
            counter_reset = False

            if (prev is not None and st["rx_bytes"] is not None and st["tx_bytes"] is not None
                    and prev.get("rx_bytes") is not None and prev.get("tx_bytes") is not None):
                dt_sec = (ts - prev["time"]).total_seconds()
                d_rx = st["rx_bytes"] - prev["rx_bytes"]
                d_tx = st["tx_bytes"] - prev["tx_bytes"]

                if d_rx < 0 or d_tx < 0:
                    counter_reset = True
                elif dt_sec >= MIN_SAMPLE_INTERVAL_SEC:
                    rx_mbps = (d_rx * 8) / dt_sec / 1_000_000.0
                    tx_mbps = (d_tx * 8) / dt_sec / 1_000_000.0
                    if rx_mbps > SANITY_MAX_MBPS:
                        self._clamp_msgs[mac] = f"Wi-Fi {mac} RX rate clamped to {SANITY_MAX_MBPS:.0f} Mbps"
                        rx_mbps = SANITY_MAX_MBPS
                    if tx_mbps > SANITY_MAX_MBPS:
                        self._clamp_msgs[mac] = f"Wi-Fi {mac} TX rate clamped to {SANITY_MAX_MBPS:.0f} Mbps"
                        tx_mbps = SANITY_MAX_MBPS

            if counter_reset:
                if st["rx_bytes"] is not None and st["tx_bytes"] is not None:
                    self._last_sample[mac] = {"time": ts, "rx_bytes": st["rx_bytes"], "tx_bytes": st["tx_bytes"]}
                else:
                    self._last_sample.pop(mac, None)
            elif rx_mbps is not None or tx_mbps is not None or prev is None:
                if st["rx_bytes"] is not None and st["tx_bytes"] is not None:
                    self._last_sample[mac] = {"time": ts, "rx_bytes": st["rx_bytes"], "tx_bytes": st["tx_bytes"]}

            rows.append({
                "mac": mac, "iface": st["iface"],
                "rx_bytes": st["rx_bytes"], "tx_bytes": st["tx_bytes"],
                "rx_mbps": rx_mbps, "tx_mbps": tx_mbps,
                "signal_dbm": st["signal_dbm"], "signal_avg_dbm": st["signal_avg_dbm"],
                "connected_sec": st["connected_sec"], "inactive_ms": st["inactive_ms"],
                "tx_retries": st["tx_retries"], "tx_failed": st["tx_failed"],
                "rx_drop_misc": st["rx_drop_misc"],
            })
        return rows

    def _parse_dump(self, body: str):
        stations = []
        current = None
        for raw_line in body.splitlines():
            line = raw_line.strip()
            m = STATION_RE.match(line)
            if m:
                if current:
                    stations.append(current)
                current = {
                    "mac": m.group(1), "iface": m.group(2),
                    "rx_bytes": None, "tx_bytes": None,
                    "signal_dbm": None, "signal_avg_dbm": None,
                    "connected_sec": None, "inactive_ms": None,
                    "tx_retries": None, "tx_failed": None, "rx_drop_misc": None,
                }
                continue
            if current is None or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            try:
                if key == "rx bytes":
                    current["rx_bytes"] = int(val.split()[0])
                elif key == "tx bytes":
                    current["tx_bytes"] = int(val.split()[0])
                elif key == "signal":
                    current["signal_dbm"] = _first_int(val)
                elif key == "signal avg":
                    current["signal_avg_dbm"] = _first_int(val)
                elif key == "connected time":
                    current["connected_sec"] = _first_int(val)
                elif key == "inactive time":
                    current["inactive_ms"] = _first_int(val)
                elif key == "tx retries":
                    current["tx_retries"] = _first_int(val)
                elif key == "tx failed":
                    current["tx_failed"] = _first_int(val)
                elif key == "rx drop misc":
                    current["rx_drop_misc"] = _first_int(val)
            except (ValueError, IndexError):
                continue
        if current:
            stations.append(current)
        return stations

    def excel_rename(self) -> dict:
        return {
            "mac": "MAC", "iface": "Interface",
            "rx_bytes": "RX_Bytes", "tx_bytes": "TX_Bytes",
            "rx_mbps": "RX_Mbps", "tx_mbps": "TX_Mbps",
            "signal_dbm": "Signal_dBm", "signal_avg_dbm": "Signal_Avg_dBm",
            "connected_sec": "Connected_Seconds", "inactive_ms": "Inactive_ms",
            "tx_retries": "TX_Retries", "tx_failed": "TX_Failed", "rx_drop_misc": "RX_Drop_Misc",
        }

    def log_line(self, row: dict):
        mac = row.get("mac")
        return self._clamp_msgs.pop(mac, None) if mac else None

    def reset(self):
        self._last_sample = {}
        self._clamp_msgs = {}


MODULE = WifiStationModule()
