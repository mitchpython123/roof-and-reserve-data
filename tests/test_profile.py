import gzip
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from rr_nem.derive import derive, profile_rows, rebuild_profile
from rr_nem.profile import build_profile_feed
from rr_nem.store import Store
from tests.helpers import fill_store

NOW = datetime(2026, 10, 16, 8, 0, 0)


class TmpStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.store = Store(self.root)


class TestLayout(TmpStore):
    def test_old_month_files_are_split_into_days(self):
        # write a month file in the old layout, then open the store again
        d = os.path.join(self.root, "store", "dispatch_price")
        with gzip.open(os.path.join(d if os.path.isdir(d) else self._mk(d), "2026-10.csv.gz"), "wt") as fh:
            fh.write("settlement_ts,region_id,intervention,rrp\n"
                     "2026-10-01 00:05:00,NSW1,0,20.0\n2026-10-02 00:05:00,NSW1,0,30.0\n")
        store = Store(self.root)
        self.assertEqual(store.parts("dispatch_price"), ["2026-10-01", "2026-10-02"])
        self.assertEqual(len(list(store.rows("dispatch_price"))), 2)
        self.assertFalse(os.path.exists(os.path.join(d, "2026-10.csv.gz")))

    def _mk(self, d):
        os.makedirs(d)
        return d

    def test_summary_tables_stay_monthly(self):
        self.store.upsert("daily_stats", [(date(2026, 10, 1), "NSW1", 288, 1.0, 0.0, 2.0, 0, 1.0, 1.0)])
        self.assertEqual(self.store.parts("daily_stats"), ["2026-10"])

    def test_prune_moves_old_raw_days_only(self):
        self.store.upsert("dispatch_price", [(datetime(2026, 1, 1, 0, 5), "NSW1", 0, 1.0),
                                             (datetime(2026, 10, 1, 0, 5), "NSW1", 0, 2.0)])
        self.store.upsert("daily_stats", [(date(2026, 1, 1), "NSW1", 288, 1.0, 0.0, 2.0, 0, 1.0, 1.0)])
        archive = os.path.join(self.root, "archive")
        self.assertEqual(self.store.prune(90, date(2026, 10, 22), archive), 1)
        self.assertEqual(self.store.parts("dispatch_price"), ["2026-10-01"])
        self.assertTrue(os.path.exists(os.path.join(archive, "dispatch_price", "2026-01-01.csv.gz")))
        self.assertEqual(self.store.parts("daily_stats"), ["2026-01"])


class TestProfile(TmpStore):
    def test_slots_follow_the_market_day(self):
        rows = profile_rows([(datetime(2026, 10, 16, 4, 5), "NSW1", 10.0),   # first interval of the day
                             (datetime(2026, 10, 16, 4, 30), "NSW1", -5.0),  # still slot 0
                             (datetime(2026, 10, 16, 4, 35), "NSW1", 400.0),  # slot 1, a spike
                             (datetime(2026, 10, 16, 4, 0), "NSW1", 1.0)],   # last of the 15th
                            [(datetime(2026, 10, 16, 4, 5), "NSW1", 7000.0)],
                            [(datetime(2026, 10, 16, 4, 30), "NSW1", "SATELLITE", 9.0),
                             (datetime(2026, 10, 16, 4, 30), "NSW1", "MEASUREMENT", 5.0)])
        by = {(r[0], r[2]): r for r in rows}
        s0 = by[(date(2026, 10, 16), 0)]
        self.assertEqual((s0[3], s0[4], s0[7], s0[9], s0[10]), (2, 2.5, 1, 7000.0, 5.0))
        self.assertEqual(by[(date(2026, 10, 16), 1)][8], 1)
        self.assertIn((date(2026, 10, 15), 47), by)

    def test_derive_refreshes_recent_profile_and_rebuild_matches(self):
        fill_store(self.store, date(2026, 10, 13), date(2026, 10, 16), until=NOW)
        derive(self.store, now=NOW)
        live = sorted(self.store.rows("profile30"))
        self.assertEqual({r[0] for r in live}, {date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)})
        rebuild_profile(self.store)
        self.assertEqual(sorted(self.store.rows("profile30")), live)

    def test_feed_uses_complete_days_and_clock_hours(self):
        fill_store(self.store, date(2026, 10, 13), date(2026, 10, 16), until=NOW)
        rebuild_profile(self.store)
        feed = build_profile_feed(self.store, now=NOW)
        self.assertEqual(feed["latest_complete_day"], "2026-10-15")
        oct_ = feed["regions"]["NSW1"]["months"][0]
        self.assertEqual((oct_["month"], oct_["days"], oct_["partial"]), ("2026-10", 3, True))
        # band prices: -10 at 10am-3pm, 200 at 5pm-9pm, 50 otherwise; hour 0 is midnight
        self.assertEqual(oct_["price"][0], 50.0)
        self.assertEqual(oct_["price"][12], -10.0)
        self.assertEqual(oct_["price"][18], 200.0)
        self.assertEqual(oct_["neg"][12], 100.0)
        self.assertEqual(oct_["gap"], 210.0)
        self.assertEqual(oct_["negative_hours"], 15.0)  # 5 hours a day for 3 days
        self.assertEqual(feed["regions"]["QLD1"]["months"][0]["price"][0], 60.0)

    def test_derive_does_not_overwrite_the_day_before_its_window(self):
        fill_store(self.store, date(2026, 9, 25), date(2026, 10, 16), until=NOW)
        rebuild_profile(self.store)
        from rr_nem.derive import rebuild_daily
        rebuild_daily(self.store)
        derive(self.store, now=NOW)
        counts = {r[0]: r[2] for r in self.store.rows("daily_stats") if r[1] == "NSW1"}
        self.assertTrue(all(n == 288 for d, n in counts.items() if d < date(2026, 10, 16)), counts)

    def test_empty_store_gives_no_feed(self):
        self.assertIsNone(build_profile_feed(self.store))


if __name__ == "__main__":
    unittest.main()
