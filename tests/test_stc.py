import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from rr_nem.fetch import Fetcher
from rr_nem.stc import FILE_NAME, SOURCE_URL, parse_stc_price, refresh_stc_price

NOW = datetime(2026, 10, 21, 9, 0, 0)  # NEM time


def ms(day):
    """Chart x value: midnight UTC on a date, in milliseconds, as the source page uses."""
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def series(name, points):
    pts = ",".join('{"x":%d,"y":%s}' % (ms(d), p) for d, p in points)
    return ('{\n type: "scatter",\n xValueType: "dateTime",\n dataPoints: [' + pts + '],\n'
            ' markerType: "circle",\n legendText: "%s",\n name: "%s",\n}' % (name, name))


def page(stc_points, last_price="39.65"):
    return ("<html><script>var chart = new CanvasJS.Chart('c', {data: [" +
            series("VEEC", [(date(2026, 10, 19), 82.0)]) + "," + series("STC", stc_points) + "," +
            series("LGC", [(date(2026, 10, 19), 7.5)]) + "]});</script>" +
            '<b>LAST PRICES</b><div class="canvas"> STC $' + last_price + ' </div></html>')


class FakePage(Fetcher):
    def __init__(self, html=None, fail=False):
        super().__init__(pause=0)
        self.html, self.fail, self.calls = html, fail, []

    def get(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("offline")
        return self.html.encode()


class TestParse(unittest.TestCase):
    def test_median_of_last_30_days_of_stc_trades_only(self):
        pts = [(date(2026, 8, 1), 30.0),            # older than the window
               (date(2026, 10, 1), 39.8), (date(2026, 10, 12), 39.9),
               (date(2026, 10, 19), 39.65), (date(2026, 10, 19), 39.9)]
        got = parse_stc_price(page(pts), NOW.date())
        self.assertEqual(got["as_of"], "2026-10-19")
        self.assertEqual(got["trades"], 4)
        self.assertAlmostEqual(got["price"], 39.85)

    def test_out_of_range_trades_are_ignored(self):
        got = parse_stc_price(page([(date(2026, 10, 19), 85.0), (date(2026, 10, 19), 39.7)]), NOW.date())
        self.assertAlmostEqual(got["price"], 39.7)

    def test_falls_back_to_last_price_box(self):
        html = '<b>LAST PRICES</b><div class="canvas"> STC $39.65 </div>'
        self.assertEqual(parse_stc_price(html, NOW.date()),
                         {"price": 39.65, "as_of": "2026-10-21", "trades": 0})

    def test_nothing_usable(self):
        self.assertIsNone(parse_stc_price("<html>maintenance</html>", NOW.date()))


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, FILE_NAME)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_writes_file_the_calculator_reads(self):
        f = FakePage(page([(date(2026, 10, 19), 39.65)]))
        refresh_stc_price(self.dir, f, NOW)
        data = self.read()
        self.assertEqual(f.calls, [SOURCE_URL])
        self.assertEqual((data["price"], data["as_of"]), (39.65, "2026-10-19"))
        self.assertTrue(data["fetched_at"].endswith("+10:00"))

    def test_skips_when_under_a_week_old_and_refreshes_after(self):
        f = FakePage(page([(date(2026, 10, 19), 39.65)]))
        refresh_stc_price(self.dir, f, NOW)
        self.assertEqual(refresh_stc_price(self.dir, f, NOW + timedelta(days=6)), "stc: up to date")
        self.assertEqual(len(f.calls), 1)
        refresh_stc_price(self.dir, f, NOW + timedelta(days=7, minutes=1))
        self.assertEqual(len(f.calls), 2)

    def test_keeps_existing_file_when_source_fails_or_is_unusable(self):
        refresh_stc_price(self.dir, FakePage(page([(date(2026, 10, 19), 39.65)])), NOW)
        later = NOW + timedelta(days=8)
        self.assertIn("kept existing", refresh_stc_price(self.dir, FakePage(fail=True), later))
        self.assertIn("kept existing", refresh_stc_price(self.dir, FakePage("<html></html>"), later))
        self.assertEqual(self.read()["price"], 39.65)


if __name__ == "__main__":
    unittest.main()
