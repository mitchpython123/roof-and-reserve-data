"""History backfill from AEMO's NEMWEB archive, using only the standard library.

Sources (all public, no sign-in):
  * Reports/ARCHIVE/Public_Prices     one zip per month of daily final-price files (5-minute
                                      prices and regional demand, table DREGION)
  * Reports/Current/Public_Prices     the last ~60 days of the same daily files, for months
                                      not yet archived
  * Reports/ARCHIVE/ROOFTOP_PV/ACTUAL weekly zips of 30-minute rooftop solar estimates

Raw rows go into the store's day files, then daily_stats and profile30 are rebuilt for the
period. Re-running is safe: rows are replaced, never duplicated.

    python -m rr_nem backfill --from 2026-01 --to 2026-10 --data-dir data
"""

import sys
from datetime import date, datetime, timedelta

from .derive import rebuild_daily, rebuild_profile
from .ingest import PRICES_DIR, PRICES_PATTERN
from .mms import extract_demand, extract_prices, extract_rooftop

ARCHIVE = "https://nemweb.com.au/Reports/ARCHIVE/"
ARCHIVE_PRICES_DIR = ARCHIVE + "Public_Prices/"
ARCHIVE_PRICES_PATTERN = r"PUBLIC_PRICES_(\d{6})01\.zip$"
ARCHIVE_ROOFTOP_DIR = ARCHIVE + "ROOFTOP_PV/ACTUAL/"
ARCHIVE_ROOFTOP_PATTERN = r"PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_(\d{8})\.zip$"


def _log(message):
    print(message, file=sys.stderr, flush=True)


def _month_start(text):
    y, m = text.split("-")
    return date(int(y), int(m), 1)


def _next_month(d):
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def backfill(store, fetcher, first, last, rooftop=True):
    """Load history for calendar months first..last ('YYYY-MM'). Returns a summary dict."""
    start, end = _month_start(first), _next_month(_month_start(last))
    summary = {"price_rows": 0, "demand_rows": 0, "rooftop_rows": 0, "months_archived": [], "days_current": 0}

    def keep(rows):
        return [r for r in rows if start <= (r[0] - timedelta(hours=4, minutes=5)).date() < end]

    # 1. monthly archive bundles
    archived = {}
    for name, groups in fetcher.list_files(ARCHIVE_PRICES_DIR, ARCHIVE_PRICES_PATTERN):
        archived[datetime.strptime(groups[0], "%Y%m").date()] = name
    covered = set()
    month = start
    while month < end:
        if month in archived:
            fetcher.polite()
            blob = fetcher.get(ARCHIVE_PRICES_DIR + archived[month])
            prices, demand = keep(extract_prices(blob)), keep(extract_demand(blob))
            summary["price_rows"] += store.upsert("dispatch_price", prices)
            summary["demand_rows"] += store.upsert("dispatch_regionsum", demand)
            summary["months_archived"].append(month.strftime("%Y-%m"))
            covered |= {(r[0] - timedelta(hours=4, minutes=5)).date() for r in prices}
            _log(f"{month:%Y-%m}: {len(prices)} prices, {len(demand)} demand rows from the archive")
        month = _next_month(month)

    # 2. recent daily files for days the archive does not cover yet
    for name, groups in fetcher.list_files(PRICES_DIR, PRICES_PATTERN):
        day = datetime.strptime(groups[0], "%Y%m%d").date()
        if not (start <= day < end) or day in covered:
            continue
        fetcher.polite()
        blob = fetcher.get(PRICES_DIR + name)
        summary["price_rows"] += store.upsert("dispatch_price", extract_prices(blob))
        summary["demand_rows"] += store.upsert("dispatch_regionsum", extract_demand(blob))
        summary["days_current"] += 1
    _log(f"{summary['days_current']} recent daily files")

    # 3. rooftop solar, weekly archive bundles that overlap the period
    if rooftop:
        weeks = [(datetime.strptime(g[0], "%Y%m%d").date(), name)
                 for name, g in fetcher.list_files(ARCHIVE_ROOFTOP_DIR, ARCHIVE_ROOFTOP_PATTERN)]
        for week, name in sorted(weeks):
            if week + timedelta(days=7) < start or week >= end:
                continue
            fetcher.polite()
            rows = [r for r in extract_rooftop(fetcher.get(ARCHIVE_ROOFTOP_DIR + name))
                    if start <= r[0].date() <= end]
            summary["rooftop_rows"] += store.upsert("rooftop_pv", rows)
        _log(f"{summary['rooftop_rows']} rooftop rows")

    summary["daily_stats_rows"] = rebuild_daily(store, start)
    summary["profile_rows"] = rebuild_profile(store, since=start, until=end - timedelta(days=1))
    return summary
