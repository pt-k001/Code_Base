import os
from datetime import datetime

_START = "[record]"
_END = "[/record]"
_HEADER_KEYS = ("time", "host", "cmd", "tag")


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", ""))
    except ValueError:
        return None


def _unescape(line: str) -> str:
    if line.startswith("\\") and line[1:].strip() in (_START, _END):
        return line[1:]
    return line


def _build_record(lines):
    try:
        sep = next(i for i, line in enumerate(lines) if line.strip() == "---")
    except StopIteration:
        return None
    rec = {"time": None, "host": None, "cmd": None, "tag": None,
           "body": "\n".join(_unescape(line) for line in lines[sep + 1:])}
    for line in lines[:sep]:
        key, _, value = line.partition("=")
        if key.strip() in _HEADER_KEYS:
            rec[key.strip()] = value.strip()
    return rec if rec["time"] and rec["cmd"] else None


def extract_records(text: str):
    block = None
    for line in text.splitlines():
        stripped = line.strip()
        if block is None:
            if stripped == _START:
                block = []
        elif stripped == _END:
            rec = _build_record(block)
            if rec:
                yield rec
            block = None
        else:
            block.append(line)


def count_records(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() == _END)


class LogReader:
    def __init__(self):
        self.filepath = None
        self.offset = 0

    def set_filepath(self, path):
        self.filepath = path
        self.offset = 0

    def read_new(self) -> str:
        if not self.filepath:
            return ""
        try:
            if os.path.getsize(self.filepath) < self.offset:
                self.offset = 0
            lines = []
            end = self.offset
            with open(self.filepath, "r", encoding="utf-8", errors="ignore", newline="") as f:
                f.seek(self.offset)
                for line in iter(f.readline, ""):
                    lines.append(line)
                    if line.strip() == _END:
                        end = f.tell()
        except OSError:
            return ""
        self.offset = end
        return "".join(lines)


class DataFeed:
    def __init__(self):
        self.reader = LogReader()

    def set_filepath(self, path):
        self.reader.set_filepath(path)

    def poll_with_progress(self):
        text = self.reader.read_new()
        if not text:
            return
        total = count_records(text)
        yield ("count", total)
        index = 0
        for rec in extract_records(text):
            ts = parse_time(rec["time"])
            if ts is None:
                continue
            index += 1
            yield ("record", index, total, ts, rec["cmd"], rec["body"])
