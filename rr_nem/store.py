"""A small file-based market-data store.

Each table is a folder of monthly gzip-compressed CSV files:

    <data-dir>/store/<table>/<YYYY-MM>.csv.gz

Why this layout:
  * Only the current month's file changes between updates, so the repository stays small.
  * Files are written deterministically, so unchanged months produce identical bytes.
  * DuckDB, pandas, Excel and R all read CSV directly, for example in DuckDB:
        SELECT * FROM read_csv('data/store/dispatch_price/*.csv.gz');
    and converting to Parquet later is a one-line COPY.

Timestamps are NEM time (UTC+10, no daylight saving), written as 'YYYY-MM-DD HH:MM:SS'.
"""

import csv
import gzip
import io
import json
import os
from datetime import date, datetime

TS_FMT = "%Y-%m-%d %H:%M:%S"

# column name, type. The first NKEY columns are the unique key; the first column
# decides which month file a row lives in.
COLUMNS = {
    "dispatch_price": [("settlement_ts", "ts"), ("region_id", "str"), ("intervention", "int"), ("rrp", "float")],
    "dispatch_regionsum": [("settlement_ts", "ts"), ("region_id", "str"), ("intervention", "int"), ("totaldemand", "float")],
    "rooftop_pv": [("interval_ts", "ts"), ("region_id", "str"), ("type", "str"), ("power_mw", "float")],
    "daily_stats": [
        ("market_day", "date"), ("region_id", "str"), ("n", "int"), ("avg_rrp", "float"),
        ("min_rrp", "float"), ("max_rrp", "float"), ("neg_n", "int"),
        ("midday_avg", "float"), ("evening_avg", "float"),
    ],
}
NKEY = {"dispatch_price": 3, "dispatch_regionsum": 3, "rooftop_pv": 3, "daily_stats": 2}


def _to_cell(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime(TS_FMT)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return repr(round(value, 6))
    return str(value)


def _from_cell(text, kind):
    if text == "":
        return None
    if kind == "ts":
        return datetime.strptime(text, TS_FMT)
    if kind == "date":
        return date.fromisoformat(text)
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    return text


class Store:
    def __init__(self, root):
        self.root = root
        self.base = os.path.join(root, "store")
        os.makedirs(self.base, exist_ok=True)

    # ---- file helpers -------------------------------------------------------
    def _dir(self, table):
        return os.path.join(self.base, table)

    def path(self, table, month):
        return os.path.join(self._dir(table), f"{month}.csv.gz")

    def months(self, table):
        d = self._dir(table)
        if not os.path.isdir(d):
            return []
        return sorted(f[:-7] for f in os.listdir(d) if f.endswith(".csv.gz"))

    def _read_file(self, table, month):
        """Return {key_tuple: row_tuple_of_strings} for one month file."""
        path = self.path(table, month)
        rows = {}
        if not os.path.exists(path):
            return rows
        nkey = NKEY[table]
        with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            for row in reader:
                if row:
                    rows[tuple(row[:nkey])] = tuple(row)
        return rows

    def _write_file(self, table, month, rows):
        names = [c for c, _ in COLUMNS[table]]
        path = self.path(table, month)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as raw:
            # mtime=0 makes the gzip bytes depend only on the content
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as gz:
                text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
                writer = csv.writer(text, lineterminator="\n")
                writer.writerow(names)
                for key in sorted(rows):
                    writer.writerow(rows[key])
                text.flush()
                text.detach()
        os.replace(tmp, path)

    # ---- public API ---------------------------------------------------------
    def upsert(self, table, rows):
        """Insert rows (tuples in column order), replacing rows with the same key."""
        if table not in COLUMNS:
            raise KeyError(f"unknown table {table}")
        nkey = NKEY[table]
        width = len(COLUMNS[table])
        by_month = {}
        for row in rows:
            if len(row) != width:
                raise ValueError(f"{table} rows need {width} columns, got {len(row)}")
            cells = tuple(_to_cell(v) for v in row)
            by_month.setdefault(cells[0][:7], {})[cells[:nkey]] = cells
        for month, new in by_month.items():
            existing = self._read_file(table, month)
            existing.update(new)
            self._write_file(table, month, existing)
        return sum(len(v) for v in by_month.values())

    def rows(self, table, since=None):
        """Yield typed row tuples, optionally only those whose first column >= since."""
        kinds = [k for _, k in COLUMNS[table]]
        since_month = None
        if since is not None:
            since_month = _to_cell(since)[:7]
        for month in self.months(table):
            if since_month is not None and month < since_month:
                continue
            for row in self._read_file(table, month).values():
                typed = tuple(_from_cell(t, k) for t, k in zip(row, kinds))
                if since is None or typed[0] >= since:
                    yield typed

    def max_first(self, table):
        """Largest value of the first column (latest timestamp/day), or None if empty."""
        kind = COLUMNS[table][0][1]
        for month in reversed(self.months(table)):
            keys = [r[0] for r in self._read_file(table, month).values()]
            if keys:
                return _from_cell(max(keys), kind)
        return None

    # ---- small bits of pipeline state (e.g. which daily files were fetched) --
    def _state_path(self):
        return os.path.join(self.base, "_state.json")

    def load_state(self):
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def save_state(self, state):
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, self._state_path())

    # ---- small JSON documents kept beside the tables (e.g. the latest forecast) ---
    def _json_path(self, name):
        return os.path.join(self.base, f"{name}.json")

    def load_json(self, name):
        try:
            with open(self._json_path(name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def save_json(self, name, data):
        tmp = self._json_path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, self._json_path(name))
