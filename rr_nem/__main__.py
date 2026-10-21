"""Command line: python -m rr_nem <update | derive | rebuild-daily | rebuild-profile | backfill | prune | stc>"""

import argparse
import os
import sys
from datetime import date

from .common import now_nem
from .backfill import backfill
from .derive import DeriveError, derive, rebuild_daily, rebuild_profile, write_json
from .profile import build_profile_feed
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

    rp = sub.add_parser("rebuild-profile", help="recompute the half-hour profile from the raw tables")
    rp.add_argument("--data-dir", default="data")
    rp.add_argument("--since", help="only from this date, YYYY-MM-DD")

    bf = sub.add_parser("backfill", help="load history from AEMO's archive, then rebuild the summaries")
    bf.add_argument("--data-dir", default="data")
    bf.add_argument("--from", dest="first", required=True, help="first month, YYYY-MM")
    bf.add_argument("--to", dest="last", required=True, help="last month, YYYY-MM")
    bf.add_argument("--no-rooftop", action="store_true", help="skip rooftop solar")

    pr = sub.add_parser("prune", help="move raw day files older than --keep-days out of the store")
    pr.add_argument("--data-dir", default="data")
    pr.add_argument("--keep-days", type=int, default=400)
    pr.add_argument("--archive-dir", help="move them here instead of deleting them")

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
            feed = build_profile_feed(store)
            if feed is not None:
                path = os.path.join(os.path.dirname(os.path.abspath(out)), "nem-profile.json")
                write_json(path, feed)
                print(f"wrote {path} (to {feed['latest_complete_day']})")
        if args.cmd == "rebuild-daily":
            since = date.fromisoformat(args.since) if args.since else None
            print(f"rebuilt {rebuild_daily(store, since)} daily_stats rows")
        if args.cmd == "rebuild-profile":
            since = date.fromisoformat(args.since) if args.since else None
            print(f"rebuilt {rebuild_profile(store, since)} profile30 rows")
        if args.cmd == "backfill":
            print("backfill:", backfill(store, Fetcher(), args.first, args.last, rooftop=not args.no_rooftop))
        if args.cmd == "prune":
            moved = store.prune(args.keep_days, now_nem().date(), args.archive_dir)
            print(f"pruned {moved} raw day file(s) older than {args.keep_days} days")
    except (IngestError, DeriveError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
