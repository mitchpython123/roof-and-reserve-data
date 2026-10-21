"""A small file-based market-data store.

Each table is a folder of gzip-compressed CSV files, one per day for the raw 5-minute and
30-minute tables and one per month for the small summary tables:

    <data-dir>/store/<table>/<YYYY-MM-DD>.csv.gz     dispatch_price, dispatch_regionsum, rooftop_pv
    <data-dir>/store/<table>/<YYYY-MM>.csv.gz        daily_stats, profile30

Why this layout:
  * An update only reads and rewrites today's (and perhaps yesterday's) small file, not a
    whole month, so each run costs the same at the end of the month as at the start.
  * Raw days older than the retention period can be moved out whole (see `prune`).
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
from datetime import date, datetime, timedelta

TS_FMT = "%Y-%m-%d %H:%M:%S"

# column name, type. The first NKEY columns are the unique key; the first column
# decides which day or month file a row lives in.
COLUMNS = {
    "dispatch_price": [("settlement_ts", "ts"), ("region_id", "str"), ("intervention", "int"), ("rrp", "float")],
    "dispatch_regionsum": [("settlement_ts", "ts"), ("region_id", "str"), ("intervention", "int"), ("totaldemand", "float")],
    "rooftop_pv": [("interval_ts", "ts"), ("region_id", "str"), ("type", "str"), ("power_mw", "float")],
    "daily_stats": [
        ("market_day", "date"), ("region_id", "str"), ("n", "int"), ("avg_rrp", "float"),
        ("min_rrp", "float"), ("max_rrp", "float"), ("neg_n", "int"),
        ("midday_avg", "float"), ("evening_avg", "float"),
    ],
    # One row per region per half hour of each market day (slot 0 is 04:00-04:30). Built from
    # the raw tables, so long-run views never need the 5-minute history.
    "profile30": [
        ("market_day", "date"), ("region_id", "str"), ("slot", "int"), ("n", "int"), ("avg_rrp", "float"),
        ("min_rrp", "float"), ("max_rrp", "float"), ("neg_n", "int"), ("spike_n", "int"),
        ("demand_avg", "float"), ("rooftop_mw", "float"),
    ],
}
NKEY = {"dispatch_price": 3, "dispatch_regionsum": 3, "rooftop_pv": 3, "daily_stats": 2, "profile30": 3}

# How each table is split into files: by day (first 10 characters of the first column) or by
# month (first 7).
DAILY_TABLES = {"dispatch_price", "dispatch_regionsum", "rooftop_pv"}


def _part_len(table):
    return 10 if table in DAILY_TABLES else 7


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
        self._migrate()

    # ---- file helpers -------------------------------------------------------
    def _dir(self, table):
        return os.path.join(self.base, table)

    def path(self, table, part):
        return os.path.join(self._dir(table), f"{part}.csv.gz")

    def parts(self, table):
        """Sorted file keys for a table: 'YYYY-MM-DD' (daily tables) or 'YYYY-MM' (monthly)."""
        d = self._dir(table)
        if not os.path.isdir(d):
            return []
        size = _part_len(table)
        return sorted(f[:-7] for f in os.listdir(d) if f.endswith(".csv.gz") and len(f) == size + 7)

    months = parts  # older name

    def _migrate(self):
        """Split month files of the daily tables (the old layout) into day files. Runs once."""
        for table in DAILY_TABLES:
            d = self._dir(table)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not (name.endswith(".csv.gz") and len(name) == 7 + 7):
                    continue
                month = name[:7]
                by_day = {}
                for key, row in self._read_path(os.path.join(d, name), NKEY[table]).items():
                    by_day.setdefault(row[0][:10], {})[key] = row
                for day, rows in by_day.items():
                    existing = self._read_file(table, day)
                    existing.update(rows)
                    self._write_file(table, day, existing)
                os.remove(os.path.join(d, name))

    def _read_file(self, table, part):
        """Return {key_tuple: row_tuple_of_strings} for one file."""
        return self._read_path(self.path(table, part), NKEY[table])

    @staticmethod
    def _read_path(path, nkey):
        rows = {}
        if not os.path.exists(path):
            return rows
        with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            for row in reader:
                if row:
                    rows[tuple(row[:nkey])] = tuple(row)
        return rows

    def _write_file(self, table, part, rows):
        names = [c for c, _ in COLUMNS[table]]
        path = self.path(table, part)
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
        size = _part_len(table)
        by_part = {}
        for row in rows:
            if len(row) != width:
                raise ValueError(f"{table} rows need {width} columns, got {len(row)}")
            cells = tuple(_to_cell(v) for v in row)
            by_part.setdefault(cells[0][:size], {})[cells[:nkey]] = cells
        for part, new in by_part.items():
            existing = self._read_file(table, part)
            if all(existing.get(k) == v for k, v in new.items()):
                continue  # nothing new: leave the file (and its bytes) alone
            existing.update(new)
            self._write_file(table, part, existing)
        return sum(len(v) for v in by_part.values())

    def rows(self, table, since=None):
        """Yield typed row tuples, optionally only those whose first column >= since."""
        kinds = [k for _, k in COLUMNS[table]]
        since_part = None
        if since is not None:
            since_part = _to_cell(since)[:_part_len(table)]
        for part in self.parts(table):
            if since_part is not None and part < since_part:
                continue
            for row in self._read_file(table, part).values():
                typed = tuple(_from_cell(t, k) for t, k in zip(row, kinds))
                if since is None or typed[0] >= since:
                    yield typed

    def max_first(self, table):
        """Largest value of the first column (latest timestamp/day), or None if empty."""
        kind = COLUMNS[table][0][1]
        for part in reversed(self.parts(table)):
            keys = [r[0] for r in self._read_file(table, part).values()]
            if keys:
                return _from_cell(max(keys), kind)
        return None

    def prune(self, keep_days, today, archive_dir=None):
        """Move day files of the raw tables older than keep_days out of the store.

        With archive_dir they are moved to <archive_dir>/<table>/; without it they are
        deleted. The summary tables (daily_stats, profile30) are never pruned.
        Returns the number of files moved.
        """
        cutoff = (today - timedelta(days=keep_days)).isoformat()
        moved = 0
        for table in sorted(DAILY_TABLES):
            for part in self.parts(table):
                if part >= cutoff:
                    break
                src = self.path(table, part)
                if archive_dir:
                    dest = os.path.join(archive_dir, table, os.path.basename(src))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.replace(src, dest)
                else:
                    os.remove(src)
                moved += 1
        return moved

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
