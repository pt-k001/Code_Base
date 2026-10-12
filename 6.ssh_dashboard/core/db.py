import sqlite3
import threading


class ModuleDB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()

    @staticmethod
    def table_name(mod_id: str) -> str:
        safe = "".join(c if c.isalnum() or c == "_" else "_" for c in mod_id)
        return f"mod_{safe}"

    def ensure_schema(self, mod_id: str, db_schema: dict) -> list[str]:
        table = self.table_name(mod_id)
        warnings = []
        with self.lock:
            existing = {row[1] for row in self.conn.execute(f'PRAGMA table_info("{table}")')}
            if not existing:
                cols = "".join(f', "{c}" {t}' for c, t in db_schema.items())
                self.conn.execute(f'CREATE TABLE "{table}" (time TEXT NOT NULL{cols})')
                self.conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_time" ON "{table}"(time)')
            else:
                for col, coltype in db_schema.items():
                    if col not in existing:
                        self.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {coltype}')
                        warnings.append(f"{mod_id}: added new column '{col}'")
            self.conn.commit()
        return warnings

    def insert_row(self, mod_id: str, time_iso: str, row: dict, commit: bool = True) -> None:
        table = self.table_name(mod_id)
        cols = ["time", *row.keys()]
        col_sql = ", ".join(f'"{c}"' for c in cols)
        marks = ", ".join("?" for _ in cols)
        with self.lock:
            self.conn.execute(f'INSERT INTO "{table}" ({col_sql}) VALUES ({marks})',
                              [time_iso, *row.values()])
            if commit:
                self.conn.commit()

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    def clear_table(self, mod_id: str) -> None:
        with self.lock:
            try:
                self.conn.execute(f'DELETE FROM "{self.table_name(mod_id)}"')
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

    def _fetch(self, sql: str, params=()) -> list:
        with self.lock:
            try:
                return self.conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return []

    def query_window(self, mod_id: str, columns: list[str], cutoff_iso: str) -> list[tuple]:
        col_sql = ", ".join(f'"{c}"' for c in ["time", *columns])
        return self._fetch(
            f'SELECT {col_sql} FROM "{self.table_name(mod_id)}" WHERE "time" >= ? ORDER BY "time"',
            (cutoff_iso,))

    def query_window_grouped(self, mod_id: str, columns: list[str], cutoff_iso: str,
                             group_col: str, group_val) -> list[tuple]:
        col_sql = ", ".join(f'"{c}"' for c in ["time", *columns])
        return self._fetch(
            f'SELECT {col_sql} FROM "{self.table_name(mod_id)}" '
            f'WHERE "time" >= ? AND "{group_col}" = ? ORDER BY "time"',
            (cutoff_iso, group_val))

    def latest_time(self, mod_id: str):
        rows = self._fetch(f'SELECT MAX("time") FROM "{self.table_name(mod_id)}"')
        return rows[0][0] if rows and rows[0][0] else None

    def latest_time_for_value(self, mod_id: str, group_col: str, group_val):
        rows = self._fetch(
            f'SELECT MAX("time") FROM "{self.table_name(mod_id)}" WHERE "{group_col}" = ?',
            (group_val,))
        return rows[0][0] if rows and rows[0][0] else None

    def distinct_values(self, mod_id: str, group_col: str) -> list:
        rows = self._fetch(
            f'SELECT DISTINCT "{group_col}" FROM "{self.table_name(mod_id)}" '
            f'WHERE "{group_col}" IS NOT NULL ORDER BY "{group_col}"')
        return [r[0] for r in rows]

    def read_all_df(self, mod_id: str):
        import pandas as pd
        with self.lock:
            try:
                return pd.read_sql_query(
                    f'SELECT * FROM "{self.table_name(mod_id)}" ORDER BY "time"', self.conn)
            except Exception:
                return pd.DataFrame()

    def close(self) -> None:
        with self.lock:
            self.conn.commit()
            self.conn.close()
