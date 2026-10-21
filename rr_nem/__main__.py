"""Command line: python -m rr_nem <update | derive | rebuild-daily | stc> [options]"""

import argparse
import os
import sys
from datetime import date

from .common import now_nem
from .derive import DeriveError, derive, rebuild_daily, write_json
from .fetch import Fetcher
from .ingest import IngestError, run_update
from .stc import refresh_stc_price
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rr_nem", description="Roof & Reserve NEM data store")
    sub = parser.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("update", help="download new AEMO data, then rebuild the widget JSON")
    up.add_argument("--data-dir", default="data")
    up.add_argument("--out", help="JSON output path (default: <data-dir>/nem-latest.json)")

    dv = sub.add_parser("derive", help="rebuild the widget JSON from the store without downloading")
    dv.add_argument("--data-dir", default="data")
    dv.add_argument("--out")
    dv.add_argument("--sample", action="store_true", help="mark the output as sample data")

    rb = sub.add_parser("rebuild-daily", help="recompute daily_stats from all stored prices")
    rb.add_argument("--data-dir", default="data")
    rb.add_argument("--since", help="only from this date, YYYY-MM-DD")

    st = sub.add_parser("stc", help="refresh the market STC price for the rebate calculator (weekly)")
    st.add_argument("--data-dir", default="data")
    st.add_argument("--force", action="store_true", help="refresh even if the file is under a week old")

    args = parser.parse_args(argv)
    if args.cmd == "stc":
        print(refresh_stc_price(args.data_dir, Fetcher(), now_nem(), force=args.force))
        return 0
    store = Store(args.data_dir)
    try:
        if args.cmd == "update":
            summary = run_update(store, Fetcher(), now_nem())
            print("update:", summary)
        if args.cmd in ("update", "derive"):
            data = derive(store, sample=getattr(args, "sample", False))
            out = args.out or os.path.join(args.data_dir, "nem-latest.json")
            write_json(out, data)
            print(f"wrote {out} (data as at {data['data_as_at']}, {len(data['regions'])} regions)")
        if args.cmd == "rebuild-daily":
            since = date.fromisoformat(args.since) if args.since else None
            print(f"rebuilt {rebuild_daily(store, since)} daily_stats rows")
    except (IngestError, DeriveError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
