from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ChartSpec:
    key: str
    title: str
    ylabel: str
    value_col: str
    ylim: tuple | None = None
    group_by: str | None = None


class MetricModule(ABC):
    id: str
    name: str
    version: str = "1.0"
    db_schema: dict = {}
    chart_specs: list[ChartSpec] = []

    @abstractmethod
    def matches(self, cmd: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def parse(self, body: str, ts) -> dict | list[dict] | None:
        raise NotImplementedError

    def excel_rename(self) -> dict:
        return {}

    def log_line(self, row: dict) -> str | None:
        return None

    def reset(self) -> None:
        return None
