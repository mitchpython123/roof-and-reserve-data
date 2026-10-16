import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from rr_nem import ingest
from rr_nem.__main__ import main
from rr_nem.common import REGIONS, market_day_of, market_day_start
from rr_nem.derive import DeriveError, derive, rebuild_daily
from rr_nem.mms import extract_demand, extract_prices, extract_rooftop, parse_mms
from rr_nem.store import Store
from tests.helpers import (DISPATCH_DIR, PRICES_DIR, ROOFTOP_DIR, FakeFetcher, daily_name, daily_prices_zip,
                           dispatch_name, dispatch_zip, fill_store, make_zip, rooftop_name, rooftop_zip)

NOW = datetime(2026, 10, 16, 8, 0, 0)  # NEM time


class TmpDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.store = Store(self.root)


class TestTimeHelpers(unittest.TestCase):
    def test_market_day_boundaries(self):
        self.assertEqual(market_day_of(datetime(2026, 10, 16, 4, 5)), date(2026, 10, 16))
        self.assertEqual(market_day_of(datetime(2026, 10, 17, 4, 0)), date(2026, 10, 16))
        self.assertEqual(market_day_of(datetime(2026, 10, 16, 4, 0)), date(2026, 10, 15))
        self.assertEqual(market_day_of(datetime(2026, 10, 16, 0, 5)), date(2026, 10, 15))


class TestParsing(unittest.TestCase):
    def test_parse_is_header_driven_and_ignores_unknown_tables(self):
        text = ('C,"hello"\n'
                'I,DISPATCH,PRICE,5,REGIONID,RRP,SETTLEMENTDATE\n'
                'D,DISPATCH,PRICE,5,NSW1,55.5,"2026/10/16 07:15:00"\n'
                'D,OTHER,THING,1,x,y,z\n')
        rows = list(parse_mms(text))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], {"REGIONID": "NSW1", "RRP": "55.5", "SETTLEMENTDATE": "2026/10/16 07:15:00"})

    def test_live_dispatch_file(self):
        blob = dispatch_zip(datetime(2026, 10, 16, 7, 15), extra_intervention_price=777)
        prices = extract_prices(blob)
        self.assertEqual(len([p for p in prices if p[2] == 0]), 5)  # unknown region XYZ1 dropped
        self.assertIn((datetime(2026, 10, 16, 7, 15), "NSW1", 1, 777.0), prices)
        demand = extract_demand(blob)
        self.assertEqual(len(demand), 5)
        self.assertEqual(demand[0][3], 7000.0)

    def test_daily_file_reads_dregion_only(self):
        prices = extract_prices(daily_prices_zip(date(2026, 10, 15)))
        self.assertEqual(len(prices), 288 * 5)
        self.assertNotIn(99999.0, [p[3] for p in prices])

    def test_rooftop_ignores_subregions(self):
        rows = extract_rooftop(rooftop_zip(datetime(2026, 10, 16, 7, 0)))
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r[1] in REGIONS for r in rows))

    def test_garbage_zip_gives_no_rows(self):
        self.assertEqual(extract_prices(make_zip("not an mms file at all\n")), [])


class TestStore(TmpDirTest):
    def test_upsert_replaces_same_key_and_splits_months(self):
        a = datetime(2026, 9, 30, 23, 55)
        b = datetime(2026, 10, 1, 0, 5)
        self.store.upsert("dispatch_price", [(a, "NSW1", 0, 10.0), (b, "NSW1", 0, 20.0)])
        self.store.upsert("dispatch_price", [(b, "NSW1", 0, 25.5), (b, "NSW1", 1, 99.0)])
        self.assertEqual(self.store.months("dispatch_price"), ["2026-09", "2026-10"])
        rows = sorted(self.store.rows("dispatch_price"))
        self.assertEqual(rows, [(a, "NSW1", 0, 10.0), (b, "NSW1", 0, 25.5), (b, "NSW1", 1, 99.0)])
        self.assertEqual(self.store.max_first("dispatch_price"), b)
        self.assertEqual(list(self.store.rows("dispatch_price", since=b))[0][0], b)

    def test_files_are_byte_identical_when_content_unchanged(self):
        rows = [(datetime(2026, 10, 1, 0, 5), "NSW1", 0, 20.0)]
        self.store.upsert("dispatch_price", rows)
        path = self.store.path("dispatch_price", "2026-10")
        with open(path, "rb") as fh:
            first = fh.read()
        self.store.upsert("dispatch_price", rows)
        with open(path, "rb") as fh:
            self.assertEqual(first, fh.read())

    def test_wrong_width_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.upsert("dispatch_price", [(datetime(2026, 10, 1), "NSW1", 0)])

    def test_empty_store(self):
        self.assertIsNone(self.store.max_first("dispatch_price"))
        self.assertEqual(list(self.store.rows("dispatch_price")), [])


class TestIngest(TmpDirTest):
    def dispatch_fetcher(self, start, count):
        f = FakeFetcher()
        for i in range(count):
            t = start + timedelta(minutes=5 * i)
            f.add(DISPATCH_DIR, dispatch_name(t), dispatch_zip(t))
        return f

    def test_incremental_dispatch_with_refetch_window(self):
        start = datetime(2026, 10, 16, 5, 5)
        f = self.dispatch_fetcher(start, 12)
        first = ingest.ingest_dispatch(self.store, f)
        self.assertEqual(first["dispatch_files"], 12)
        self.assertEqual(len(list(self.store.rows("dispatch_price"))), 12 * 5)
        self.assertEqual(len(list(self.store.rows("dispatch_regionsum"))), 12 * 5)

        for i in range(12, 14):
            t = start + timedelta(minutes=5 * i)
            f.add(DISPATCH_DIR, dispatch_name(t), dispatch_zip(t))
        f.calls.clear()
        second = ingest.ingest_dispatch(self.store, f)
        # last stored = 06:00; window re-reads files after 05:30 (6 old + 2 new)
        self.assertEqual(second["dispatch_files"], 8)
        self.assertEqual(len(f.downloads(DISPATCH_DIR)), 8)
        self.assertEqual(len(list(self.store.rows("dispatch_price"))), 14 * 5)  # no duplicates

    def test_no_files_listed_raises(self):
        with self.assertRaises(ingest.IngestError):
            ingest.ingest_dispatch(self.store, FakeFetcher())

    def test_unreadable_files_raise_so_old_data_is_kept(self):
        f = FakeFetcher()
        f.add(DISPATCH_DIR, dispatch_name(datetime(2026, 10, 16, 7, 15)), make_zip("garbage\n"))
        with self.assertRaises(ingest.IngestError):
            ingest.ingest_dispatch(self.store, f)

    def test_daily_prices_fill_gaps_once(self):
        f = FakeFetcher()
        for d in (date(2026, 10, 14), date(2026, 10, 15)):
            f.add(PRICES_DIR, daily_name(d), daily_prices_zip(d))
        out = ingest.ingest_daily_prices(self.store, f, NOW)
        self.assertEqual(out["daily_files"], 2)
        self.assertEqual(len(list(self.store.rows("dispatch_price"))), 2 * 288 * 5)
        f.calls.clear()
        out = ingest.ingest_daily_prices(self.store, f, NOW)
        self.assertEqual(out["daily_files"], 0)
        self.assertEqual(f.downloads(PRICES_DIR), [])

    def test_rooftop_incremental(self):
        f = FakeFetcher()
        for i in range(4):
            t = datetime(2026, 10, 16, 5, 0) + timedelta(minutes=30 * i)
            f.add(ROOFTOP_DIR, rooftop_name(t), rooftop_zip(t, {r: 100.0 * (i + 1) for r in REGIONS}))
        ingest.ingest_rooftop(self.store, f)
        self.assertEqual(len(list(self.store.rows("rooftop_pv"))), 20)
        self.assertEqual(self.store.max_first("rooftop_pv"), datetime(2026, 10, 16, 6, 30))

    def test_non_critical_feed_failure_does_not_stop_update(self):
        f = self.dispatch_fetcher(datetime(2026, 10, 16, 7, 5), 3)
        f.fail_dirs.add(ROOFTOP_DIR)
        summary = ingest.run_update(self.store, f, NOW)
        self.assertEqual(summary["dispatch_files"], 3)
        self.assertIn("rooftop solar error", summary)


class TestDerive(TmpDirTest):
    def test_empty_store_raises(self):
        with self.assertRaises(DeriveError):
            derive(self.store, now=NOW)

    def test_headline_numbers(self):
        # nine full market days (7-15 Oct) and the 16th until 08:00
        fill_store(self.store, date(2026, 10, 7), date(2026, 10, 16), until=datetime(2026, 10, 16, 8, 0))
        data = derive(self.store, now=NOW)
        self.assertEqual(data["data_as_at"], "2026-10-16T08:00:00+10:00")
        self.assertEqual(data["market_day"], "2026-10-16")
        self.assertFalse(data["sample"])
        nsw = data["regions"]["NSW1"]
        self.assertEqual(nsw["current"]["price"], 50.0)
        self.assertEqual(nsw["today"]["intervals"], 48)
        self.assertEqual(nsw["today"]["avg"], 50.0)
        self.assertEqual(nsw["today"]["negative_intervals"], 0)
        self.assertEqual(nsw["today"]["shape30"][:8], [50.0] * 8)
        self.assertEqual(nsw["today"]["shape30"][8:], [None] * 40)
        l7 = nsw["last7"]
        self.assertEqual(l7["days"], 7)
        # per day: 60 midday intervals at -10, 48 evening at 200, 180 others at 50
        self.assertEqual(l7["avg"], 62.5)
        self.assertEqual(l7["midday_avg"], -10.0)
        self.assertEqual(l7["evening_avg"], 200.0)
        self.assertEqual(l7["gap"], 210.0)
        self.assertEqual(l7["negative_intervals"], 7 * 60)
        self.assertEqual((l7["min"], l7["max"]), (-10.0, 200.0))
        self.assertEqual([d["date"] for d in l7["by_day"]][0], "2026-10-09")
        self.assertEqual(l7["by_day"][-1]["date"], "2026-10-15")
        qld = data["regions"]["QLD1"]
        self.assertEqual(qld["last7"]["midday_avg"], 0.0)
        self.assertEqual(qld["last7"]["gap"], 210.0)
        self.assertEqual(qld["current"]["price"], 60.0)

    def test_midday_today_counts_negative_time_and_shape(self):
        fill_store(self.store, date(2026, 10, 14), date(2026, 10, 16), until=datetime(2026, 10, 16, 12, 0))
        today = derive(self.store, now=NOW)["regions"]["NSW1"]["today"]
        self.assertEqual(today["intervals"], 96)
        self.assertEqual(today["negative_intervals"], 24)  # intervals starting 10:00-11:55
        self.assertEqual(today["min"], -10.0)
        self.assertEqual(today["shape30"][12], -10.0)  # 10:00 slot = (10-4)*2
        self.assertEqual(today["shape30"][11], 50.0)  # 09:30 slot

    def test_incomplete_days_are_not_counted(self):
        fill_store(self.store, date(2026, 10, 13), date(2026, 10, 16), until=datetime(2026, 10, 16, 8, 0))
        # damage the 15th: keep only its first 100 intervals
        month = self.store._read_file("dispatch_price", "2026-10")
        first_kept_end = datetime(2026, 10, 15, 4, 0) + timedelta(minutes=5 * 100)
        day_end = datetime(2026, 10, 16, 4, 0)
        keep = {k: v for k, v in month.items()
                if not (first_kept_end < datetime.strptime(k[0], "%Y-%m-%d %H:%M:%S") <= day_end)}
        self.store._write_file("dispatch_price", "2026-10", keep)
        days = [d["date"] for d in derive(self.store, now=NOW)["regions"]["NSW1"]["last7"]["by_day"]]
        self.assertEqual(days, ["2026-10-13", "2026-10-14"])

    def test_intervention_rows_do_not_change_prices(self):
        fill_store(self.store, date(2026, 10, 15), date(2026, 10, 16), until=datetime(2026, 10, 16, 8, 0))
        self.store.upsert("dispatch_price", [(datetime(2026, 10, 16, 8, 0), "NSW1", 1, 99999.0)])
        nsw = derive(self.store, now=NOW)["regions"]["NSW1"]
        self.assertEqual(nsw["current"]["price"], 50.0)
        self.assertEqual(nsw["today"]["max"], 50.0)

    def test_demand_rooftop_and_daily_stats(self):
        fill_store(self.store, date(2026, 10, 14), date(2026, 10, 16), until=datetime(2026, 10, 16, 8, 0))
        self.store.upsert("dispatch_regionsum", [
            (datetime(2026, 10, 16, 7, 55), "NSW1", 0, 7000.0), (datetime(2026, 10, 16, 8, 0), "NSW1", 0, 7250.5)])
        self.store.upsert("rooftop_pv", [(datetime(2026, 10, 16, 7, 30), "NSW1", "MEASUREMENT", 1234.5)])
        cur = derive(self.store, now=NOW)["regions"]["NSW1"]["current"]
        self.assertEqual(cur["demand_mw"], 7250.5)
        self.assertEqual(cur["demand_as_at"], "2026-10-16T08:00:00+10:00")
        self.assertEqual(cur["rooftop_pv_mw"], 1234.5)
        self.assertEqual(cur["rooftop_as_at"], "2026-10-16T07:30:00+10:00")
        stats = {(d, r): rest for d, r, *rest in self.store.rows("daily_stats")}
        n, avg, lo, hi, neg, mid, eve = stats[(date(2026, 10, 15), "NSW1")]
        self.assertEqual((n, avg, lo, hi, neg, mid, eve), (288, 62.5, -10.0, 200.0, 60, -10.0, 200.0))

    def test_rebuild_daily_matches(self):
        fill_store(self.store, date(2026, 10, 14), date(2026, 10, 15), until=datetime(2026, 10, 16, 4, 0))
        count = rebuild_daily(self.store)
        self.assertEqual(count, 2 * 5)
        self.assertEqual(len(list(self.store.rows("daily_stats"))), 10)

    def test_json_is_valid_and_has_all_regions(self):
        fill_store(self.store, date(2026, 10, 14), date(2026, 10, 16), until=datetime(2026, 10, 16, 8, 0))
        out = os.path.join(self.root, "out.json")
        self.assertEqual(main(["derive", "--data-dir", self.root, "--out", out, "--sample"]), 0)
        with open(out) as fh:
            data = json.load(fh)
        self.assertTrue(data["sample"])
        self.assertEqual(sorted(data["regions"]), sorted(REGIONS))
        self.assertEqual(len(data["regions"]["SA1"]["today"]["shape30"]), 48)

    def test_cli_reports_error_on_empty_store(self):
        self.assertEqual(main(["derive", "--data-dir", self.root, "--out", os.path.join(self.root, "x.json")]), 1)


if __name__ == "__main__":
    unittest.main()
