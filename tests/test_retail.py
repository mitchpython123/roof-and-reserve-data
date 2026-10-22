import copy
import json
import os
import tempfile
import unittest
from datetime import date

from rr_nem.retail import RETAIL_DIR, RetailError, build_retail_feed, load_years, period_at, validate


def _year(fy, start, end):
    with open(os.path.join(RETAIL_DIR, "2026-27.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    doc.update(fy=fy, effective_from=start, effective_to=end)
    return doc


class TestRetailFiles(unittest.TestCase):
    def test_repository_files_are_valid(self):
        docs = load_years()
        self.assertTrue(docs)
        ids = {n["id"] for n in docs[0]["networks"]}
        self.assertEqual(len(ids), 13)

    def test_period_lookup(self):
        doc = load_years()[0]
        nets = {n["id"]: n for n in doc["networks"]}
        ausgrid_tou = nets["ausgrid"]["plans"][0]
        self.assertEqual(period_at(ausgrid_tou, 1, True, 17)["name"], "Peak")
        self.assertEqual(period_at(ausgrid_tou, 4, True, 17)["name"], "Off-peak")  # no peak in April
        tas = nets["tasnetworks"]["plans"][0]
        self.assertEqual(period_at(tas, 7, True, 8)["name"], "Peak")
        self.assertEqual(period_at(tas, 7, False, 8)["name"], "Off-peak")  # weekends are off-peak
        sso = nets["sapn"]["plans"][1]
        self.assertEqual(period_at(sso, 1, True, 13)["rate"], 0)

    def test_gap_in_a_plan_is_caught(self):
        doc = copy.deepcopy(load_years()[0])
        plan = doc["networks"][0]["plans"][0]
        plan["periods"] = [p for p in plan["periods"] if not p.get("rest")]
        with self.assertRaises(RetailError):
            validate(doc)

    def test_unknown_source_is_caught(self):
        doc = copy.deepcopy(load_years()[0])
        doc["networks"][0]["plans"][0]["source"] = "nope"
        with self.assertRaises(RetailError):
            validate(doc)


class TestRetailFeed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        for doc in (_year("2026-27", "2026-07-01", "2027-06-30"), _year("2027-28", "2027-07-01", "2028-06-30")):
            with open(os.path.join(self.dir, doc["fy"] + ".json"), "w", encoding="utf-8") as fh:
                json.dump(doc, fh)

    def test_current_and_next(self):
        feed = build_retail_feed(date(2027, 6, 30), self.dir)
        self.assertEqual((feed["current"]["fy"], feed["next"]["fy"], feed["stale"]), ("2026-27", "2027-28", False))
        feed = build_retail_feed(date(2027, 7, 1), self.dir)
        self.assertEqual((feed["current"]["fy"], feed["next"]), ("2027-28", None))

    def test_out_of_date_is_flagged(self):
        feed = build_retail_feed(date(2028, 7, 1), self.dir)
        self.assertEqual((feed["current"]["fy"], feed["stale"]), ("2027-28", True))

    def test_overlap_is_caught(self):
        with open(os.path.join(self.dir, "bad.json"), "w", encoding="utf-8") as fh:
            json.dump(_year("bad", "2027-01-01", "2027-12-31"), fh)
        with self.assertRaises(RetailError):
            build_retail_feed(date(2027, 1, 1), self.dir)


if __name__ == "__main__":
    unittest.main()
