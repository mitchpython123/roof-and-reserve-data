"""Live ingestion from AEMO's NEMWEB 'Current' reports into the store.

Three feeds:
  * DispatchIS_Reports   one small zip per 5-minute interval: prices and regional demand.
                         Roughly the last two days are kept on the server.
  * Public_Prices        one zip per market day of final 5-minute prices (about 60 days kept,
                         each day appears shortly after 04:00 the next morning). Used to fill
                         gaps and to replace our live copy of a day with AEMO's final numbers.
  * ROOFTOP_PV/ACTUAL    30-minute estimates of rooftop solar output by region.

Every upsert is idempotent, so re-fetching a file is always safe.
"""

import sys
from datetime import datetime, timedelta

from .mms import extract_demand, extract_prices, extract_rooftop

BASE = "https://nemweb.com.au/Reports/Current/"
DISPATCH_DIR = BASE + "DispatchIS_Reports/"
PRICES_DIR = BASE + "Public_Prices/"
ROOFTOP_DIR = BASE + "ROOFTOP_PV/ACTUAL/"

DISPATCH_PATTERN = r"PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip$"
PRICES_PATTERN = r"PUBLIC_PRICES_(\d{8})0000_\d+\.zip$"
ROOFTOP_PATTERN = r"PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_(\d{14})_\d+\.zip$"

REFETCH_WINDOW = timedelta(minutes=30)  # re-read recent files in case one arrived late


class IngestError(RuntimeError):
    """Raised when the live feed cannot be read, so the published data is left untouched."""


def _log(message):
    print(message, file=sys.stderr)


def ingest_dispatch(store, fetcher, max_files=700):
    """Fetch new 5-minute dispatch files (prices and demand). Critical: raises on failure."""
    files = fetcher.list_files(DISPATCH_DIR, DISPATCH_PATTERN)
    if not files:
        raise IngestError("No dispatch files were listed. NEMWEB may be down or its layout changed.")
    parsed = sorted((datetime.strptime(groups[0], "%Y%m%d%H%M"), name) for name, groups in files)

    last = store.max_first("dispatch_price")
    cutoff = last - REFETCH_WINDOW if last else None
    wanted = [p for p in parsed if cutoff is None or p[0] > cutoff][-max_files:]
    blobs = fetcher.get_many([DISPATCH_DIR + name for _, name in wanted])

    price_rows, demand_rows = [], []
    for blob in blobs:
        if blob is None:
            continue
        price_rows += extract_prices(blob)
        demand_rows += extract_demand(blob)
    if wanted and not price_rows:
        raise IngestError("Dispatch files were downloaded but no prices could be read from them. "
                          "The file format may have changed.")
    store.upsert("dispatch_price", price_rows)
    store.upsert("dispatch_regionsum", demand_rows)
    return {"dispatch_files": len(wanted), "price_rows": len(price_rows), "demand_rows": len(demand_rows)}


def ingest_daily_prices(store, fetcher, now, lookback_days=14):
    """Fill or finalise recent market days from the daily Public_Prices files."""
    today = (now - timedelta(hours=4)).date()
    listed = {}
    for name, groups in fetcher.list_files(PRICES_DIR, PRICES_PATTERN):
        listed[datetime.strptime(groups[0], "%Y%m%d").date()] = name  # later names win

    state = store.load_state()
    done = set(state.get("daily_prices_finalised", []))
    fetched = 0
    for k in range(1, lookback_days + 1):
        day = today - timedelta(days=k)
        if day.isoformat() in done or day not in listed:
            continue
        blob = fetcher.get(PRICES_DIR + listed[day])
        rows = extract_prices(blob)
        if rows:
            store.upsert("dispatch_price", rows)
            done.add(day.isoformat())
            fetched += 1
    state["daily_prices_finalised"] = sorted(done)[-90:]
    store.save_state(state)
    return {"daily_files": fetched}


def ingest_rooftop(store, fetcher, max_files=96):
    """Fetch new rooftop solar estimates."""
    files = fetcher.list_files(ROOFTOP_DIR, ROOFTOP_PATTERN)
    parsed = sorted((datetime.strptime(groups[0], "%Y%m%d%H%M%S"), name) for name, groups in files)
    last = store.max_first("rooftop_pv")
    cutoff = last - timedelta(hours=2) if last else None
    wanted = [p for p in parsed if cutoff is None or p[0] > cutoff][-max_files:]
    rows = []
    for blob in fetcher.get_many([ROOFTOP_DIR + name for _, name in wanted]):
        if blob is not None:
            rows += extract_rooftop(blob)
    store.upsert("rooftop_pv", rows)
    return {"rooftop_files": len(wanted), "rooftop_rows": len(rows)}


def run_update(store, fetcher, now):
    """Run every feed. Only the dispatch feed is critical; the others warn and carry on."""
    summary = ingest_dispatch(store, fetcher)
    for label, step in (
        ("daily prices", lambda: ingest_daily_prices(store, fetcher, now)),
        ("rooftop solar", lambda: ingest_rooftop(store, fetcher)),
    ):
        try:
            summary.update(step())
        except Exception as exc:  # noqa: BLE001 - non-critical feeds must not stop the update
            _log(f"warning: {label} update failed: {exc}")
            summary[f"{label} error"] = str(exc)
    return summary
