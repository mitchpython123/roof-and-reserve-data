"""Parsing AEMO MMS-format CSV files, the format NEMWEB publishes.

An MMS file is CSV with a row-type letter in the first column:
  C  comment / footer
  I  header row for a table:  I,<report>,<table>,<version>,<column names...>
  D  data row:                D,<report>,<table>,<version>,<values...>

Parsing is driven by the header row of each table, so extra or re-ordered columns
do not break it.
"""

import csv
import io
import zipfile
from datetime import datetime

from .common import REGIONS

# 5-minute dispatch prices appear as DISPATCH/PRICE in the live dispatch files and as
# DREGION in the daily Public_Prices files.
PRICE_TABLES = {("DREGION", ""), ("DISPATCH", "PRICE")}
DEMAND_TABLES = {("DISPATCH", "REGIONSUM")}
ROOFTOP_TABLES = {("ROOFTOP", "ACTUAL")}


def parse_ts(value):
    return datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S")


def parse_mms(text):
    """Yield (report, table, row_dict) for every data row in an MMS CSV string."""
    headers = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5:
            continue
        kind = row[0]
        key = (row[1], row[2])
        if kind == "I":
            headers[key] = [c.strip() for c in row[4:]]
        elif kind == "D":
            cols = headers.get(key)
            if cols is not None:
                yield key[0], key[1], dict(zip(cols, row[4:]))


def csv_texts(data, _depth=0):
    """Yield the text of every CSV inside a zip (looking inside nested zips too)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            low = name.lower()
            if low.endswith(".csv"):
                yield z.read(name).decode("utf-8", errors="replace")
            elif low.endswith(".zip") and _depth < 2:
                yield from csv_texts(z.read(name), _depth + 1)


def _rows(data, wanted):
    for text in csv_texts(data):
        for report, table, row in parse_mms(text):
            if (report, table) in wanted:
                yield row


def _intervention(row):
    return int(float(row.get("INTERVENTION") or 0))


def extract_prices(data):
    """[(settlement_ts, region_id, intervention, rrp $/MWh)] from a zip of price data."""
    out = []
    for row in _rows(data, PRICE_TABLES):
        region = row.get("REGIONID", "").strip()
        if region not in REGIONS:
            continue
        try:
            out.append((parse_ts(row["SETTLEMENTDATE"]), region, _intervention(row), float(row["RRP"])))
        except (KeyError, ValueError):
            continue
    return out


def extract_demand(data):
    """[(settlement_ts, region_id, intervention, total demand MW)] from a dispatch zip."""
    out = []
    for row in _rows(data, DEMAND_TABLES):
        region = row.get("REGIONID", "").strip()
        if region not in REGIONS:
            continue
        try:
            out.append((parse_ts(row["SETTLEMENTDATE"]), region, _intervention(row), float(row["TOTALDEMAND"])))
        except (KeyError, ValueError):
            continue
    return out


def extract_rooftop(data):
    """[(interval_ts, region_id, type, power MW)] from a rooftop PV actual zip."""
    out = []
    for row in _rows(data, ROOFTOP_TABLES):
        region = row.get("REGIONID", "").strip()
        if region not in REGIONS:
            continue
        try:
            out.append((parse_ts(row["INTERVAL_DATETIME"]), region, row.get("TYPE", "").strip(), float(row["POWER"])))
        except (KeyError, ValueError):
            continue
    return out
