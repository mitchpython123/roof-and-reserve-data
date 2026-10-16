#!/usr/bin/env python3
"""One-off history backfill into the store, using the NEMOSIS package.

Run this on your own computer (not in GitHub). It downloads AEMO's monthly archive files
through NEMOSIS and writes 5-minute prices, regional demand and rooftop solar into the
same store the live updater uses. Re-running is safe: rows are replaced, never duplicated.

    pip install nemosis pandas
    python3 scripts/backfill_nemosis.py --from 2021-01 --to 2026-09 --data-dir data

Then rebuild the daily statistics (this script does it at the end) and, if you want the
history on the website feed, copy the data/ folder contents onto the repository's "data"
branch (or just run the live updater, which carries the folder forward).

NOTE: written against NEMOSIS's documented table names (DISPATCHPRICE, DISPATCHREGIONSUM,
ROOFTOP_PV_ACTUAL) but NOT yet run against the real archive. If a column is missing it
stops and prints the columns it found, so the fix is quick.
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rr_nem.common import REGIONS  # noqa: E402
from rr_nem.derive import rebuild_daily  # noqa: E402
from rr_nem.store import Store  # noqa: E402


def month_pairs(first, last):
    y, m = first
    while (y, m) <= last:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def parse_month(text):
    y, m = text.split("-")
    return int(y), int(m)


def need(df, columns, table):
    missing = [c for c in columns if c not in df.columns]
    if missing:
        sys.exit(f"{table}: expected column(s) {missing} but found {list(df.columns)}. "
                 "Please send me this message and I'll adjust the script.")


def to_dt(value):
    import pandas as pd
    return pd.Timestamp(value).to_pydatetime()


def price_rows(df):
    need(df, ["SETTLEMENTDATE", "REGIONID", "INTERVENTION", "RRP"], "DISPATCHPRICE")
    df = df[df["REGIONID"].isin(REGIONS)]
    return [(to_dt(t), r, int(float(i)), float(p))
            for t, r, i, p in zip(df["SETTLEMENTDATE"], df["REGIONID"], df["INTERVENTION"], df["RRP"])]


def demand_rows(df):
    need(df, ["SETTLEMENTDATE", "REGIONID", "INTERVENTION", "TOTALDEMAND"], "DISPATCHREGIONSUM")
    df = df[df["REGIONID"].isin(REGIONS)]
    return [(to_dt(t), r, int(float(i)), float(d))
            for t, r, i, d in zip(df["SETTLEMENTDATE"], df["REGIONID"], df["INTERVENTION"], df["TOTALDEMAND"])]


def rooftop_rows(df):
    need(df, ["INTERVAL_DATETIME", "REGIONID", "POWER"], "ROOFTOP_PV_ACTUAL")
    df = df[df["REGIONID"].isin(REGIONS)]
    kinds = df["TYPE"] if "TYPE" in df.columns else ["MEASUREMENT"] * len(df)
    return [(to_dt(t), r, str(k), float(p))
            for t, r, k, p in zip(df["INTERVAL_DATETIME"], df["REGIONID"], kinds, df["POWER"])]


TABLES = [
    ("DISPATCHPRICE", "dispatch_price", price_rows),
    ("DISPATCHREGIONSUM", "dispatch_regionsum", demand_rows),
    ("ROOFTOP_PV_ACTUAL", "rooftop_pv", rooftop_rows),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="first", required=True, help="first month, YYYY-MM")
    ap.add_argument("--to", dest="last", required=True, help="last month, YYYY-MM")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--cache", default="nemosis_cache", help="folder NEMOSIS uses for downloads")
    ap.add_argument("--skip", nargs="*", default=[], choices=[t for t, _, _ in TABLES], help="tables to skip")
    args = ap.parse_args()

    try:
        import nemosis
    except ImportError:
        sys.exit("NEMOSIS is not installed. Run:  pip install nemosis pandas")

    os.makedirs(args.cache, exist_ok=True)
    store = Store(args.data_dir)
    for y, m in month_pairs(parse_month(args.first), parse_month(args.last)):
        start = f"{y}/{m:02d}/01 00:00:00"
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = f"{ny}/{nm:02d}/01 00:00:00"
        for table, store_table, convert in TABLES:
            if table in args.skip:
                continue
            df = nemosis.dynamic_data_compiler(start, end, table, args.cache)
            count = store.upsert(store_table, convert(df))
            print(f"{y}-{m:02d} {table}: {count} rows")
    print("rebuilding daily statistics ...")
    print(f"daily_stats rows: {rebuild_daily(store)}")


if __name__ == "__main__":
    main()
