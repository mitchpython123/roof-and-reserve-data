"""Builders for AEMO-format test files, and a fake downloader."""

import io
import zipfile
from datetime import datetime, timedelta

from rr_nem.common import REGIONS, market_day_start
from rr_nem.fetch import Fetcher
from rr_nem import ingest

C_LINE = 'C,"NEMP.WORLD","TEST","AEMO","PUBLIC","2026/10/16","07:10:00","0000000000000001","TEST","0000000000000001"'


def q(ts):
    return '"' + ts.strftime("%Y/%m/%d %H:%M:%S") + '"'


def mms_csv(blocks):
    """blocks: [(report, table, version, [columns], [[values]])] -> MMS CSV text."""
    lines = [C_LINE]
    for report, table, ver, cols, rows in blocks:
        lines.append(f"I,{report},{table},{ver}," + ",".join(cols))
        for row in rows:
            lines.append(f"D,{report},{table},{ver}," + ",".join(row))
    lines.append('C,"END OF REPORT",9')
    return "\n".join(lines) + "\n"


def make_zip(text, name="file.csv"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text)
    return buf.getvalue()


def band_price(settle):
    """Deterministic price: -10 midday (10-15), 200 evening (17-21), 50 otherwise."""
    hour = (settle - timedelta(minutes=5)).hour
    if 10 <= hour < 15:
        return -10.0
    if 17 <= hour < 21:
        return 200.0
    return 50.0


def dispatch_zip(settle, prices=None, demand=None, extra_intervention_price=None):
    """One live dispatch file. Includes an unrelated table and an unknown region."""
    prices = prices or {r: band_price(settle) for r in REGIONS}
    demand = demand or {r: 7000.0 for r in REGIONS}
    price_cols = ["SETTLEMENTDATE", "RUNNO", "REGIONID", "DISPATCHINTERVAL", "INTERVENTION", "RRP", "EEP", "ROP",
                  "APCFLAG", "MARKETSUSPENDEDFLAG", "LASTCHANGED"]
    price_rows = [[q(settle), "1", r, "2026101600001", "0", str(p), "0", str(p), "0", "0", q(settle)]
                  for r, p in prices.items()]
    price_rows.append([q(settle), "1", "XYZ1", "2026101600001", "0", "123", "0", "123", "0", "0", q(settle)])
    if extra_intervention_price is not None:
        price_rows.append([q(settle), "1", "NSW1", "2026101600001", "1", str(extra_intervention_price), "0",
                           "0", "0", "0", q(settle)])
    sum_cols = ["SETTLEMENTDATE", "RUNNO", "REGIONID", "DISPATCHINTERVAL", "INTERVENTION", "TOTALDEMAND",
                "AVAILABLEGENERATION"]
    sum_rows = [[q(settle), "1", r, "2026101600001", "0", str(d), "9999"] for r, d in demand.items()]
    return make_zip(mms_csv([
        ("DISPATCH", "CASE_SOLUTION", "2", ["SETTLEMENTDATE", "RUNNO", "INTERVENTION"], [[q(settle), "1", "0"]]),
        ("DISPATCH", "PRICE", "5", price_cols, price_rows),
        ("DISPATCH", "REGIONSUM", "5", sum_cols, sum_rows),
    ]))


def daily_prices_zip(day, price_fn=band_price):
    """A Public_Prices file: a market day of DREGION rows plus a 30-minute TRADING block to ignore."""
    start = market_day_start(day)
    cols = ["SETTLEMENTDATE", "RUNNO", "REGIONID", "INTERVENTION", "RRP", "EEP", "ROP", "APCFLAG",
            "MARKETSUSPENDEDFLAG", "LASTCHANGED"]
    rows = []
    for i in range(1, 289):
        settle = start + timedelta(minutes=5 * i)
        for r in REGIONS:
            p = price_fn(settle)
            rows.append([q(settle), "1", r, "0", str(p), "0", str(p), "0", "0", q(settle)])
    trading = [[q(start + timedelta(minutes=30)), "1", "NSW1", "1", "99999"]]
    return make_zip(mms_csv([
        ("DREGION", "", "2", cols, rows),
        ("TRADING", "PRICE", "3", ["SETTLEMENTDATE", "RUNNO", "REGIONID", "PERIODID", "RRP"], trading),
    ]))


def rooftop_zip(interval, power=None):
    power = power or {r: 1500.0 for r in REGIONS}
    cols = ["INTERVAL_DATETIME", "REGIONID", "POWER", "QI", "TYPE", "LASTCHANGED"]
    rows = [[q(interval), r, str(p), "1", "MEASUREMENT", q(interval)] for r, p in power.items()]
    rows.append([q(interval), "QLDC", "321", "1", "MEASUREMENT", q(interval)])  # sub-region, ignored
    return make_zip(mms_csv([("ROOFTOP", "ACTUAL", "2", cols, rows)]))


def dispatch_name(settle):
    return f"PUBLIC_DISPATCHIS_{settle:%Y%m%d%H%M}_0000000543127610.zip"


def daily_name(day):
    return f"PUBLIC_PRICES_{day:%Y%m%d}0000_{day + timedelta(days=1):%Y%m%d}040502.zip"


def rooftop_name(interval):
    return f"PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_{interval:%Y%m%d%H%M%S}_0000000540264525.zip"


class FakeFetcher(Fetcher):
    """Serves directory listings and zips from memory and records every download."""

    def __init__(self, files=None, fail_dirs=()):
        super().__init__(pause=0)
        self.files = dict(files or {})
        self.fail_dirs = set(fail_dirs)
        self.calls = []

    def add(self, directory, name, blob):
        self.files[directory + name] = blob

    def get(self, url):
        self.calls.append(url)
        if url.endswith("/"):
            if url in self.fail_dirs:
                raise OSError("listing unavailable")
            links = "".join(
                f'<A HREF="/{u.split(".au/", 1)[1]}">{u.rsplit("/", 1)[1]}</A><br>'
                for u in sorted(self.files) if u.startswith(url))
            return f"<html><body><pre>{links}</pre></body></html>".encode()
        if url not in self.files:
            raise KeyError(url)
        return self.files[url]

    def downloads(self, directory):
        return [c for c in self.calls if c.startswith(directory) and not c.endswith("/")]


DISPATCH_DIR = ingest.DISPATCH_DIR
PRICES_DIR = ingest.PRICES_DIR
ROOFTOP_DIR = ingest.ROOFTOP_DIR


def fill_store(store, first_day, last_day, until, qld_offset=10.0):
    """Write dispatch prices straight into the store for whole days, plus a partial last day.

    Full days first_day..last_day-1, then last_day from its start up to settlement time `until`.
    """
    rows = []
    day = first_day
    while day <= last_day:
        start = market_day_start(day)
        end = start + timedelta(days=1)
        i = 1
        while True:
            settle = start + timedelta(minutes=5 * i)
            if settle > end or (day == last_day and settle > until):
                break
            for r in REGIONS:
                p = band_price(settle) + (qld_offset if r == "QLD1" else 0.0)
                rows.append((settle, r, 0, p))
            i += 1
        day += timedelta(days=1)
    store.upsert("dispatch_price", rows)
    return rows
