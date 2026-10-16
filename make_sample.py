#!/usr/bin/env python3
"""Generate SAMPLE data for previewing the widget. The numbers are invented, not AEMO data.

It builds a throwaway store of made-up (but realistic-looking) 5-minute prices for the
last 10 market days, then runs the real derive step on it, so the sample file has exactly
the same shape as the live one. The output is marked "sample": true and the widget shows a
banner for sample data, so it can't be mistaken for the real thing.

    python3 scripts/make_sample.py            # writes widget/sample-nem-latest.json
"""

import os
import random
import sys
import tempfile
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rr_nem.common import INTERVAL, REGIONS, market_day_of, market_day_start, now_nem  # noqa: E402
from rr_nem.derive import derive, write_json  # noqa: E402
from rr_nem.store import Store  # noqa: E402

# (hour of day, $/MWh) anchor points of a typical spring weekday, per region
SHAPES = {
    "NSW1": [(0, 95), (4, 85), (6, 140), (8, 110), (10, 20), (12, -25), (14, -15), (16, 70), (17.5, 180), (19, 300), (21, 200), (23, 120), (24, 95)],
    "QLD1": [(0, 85), (4, 75), (6, 120), (8, 90), (10, 5), (12, -15), (14, -5), (16, 60), (17.5, 170), (19, 260), (21, 170), (23, 105), (24, 85)],
    "VIC1": [(0, 80), (4, 70), (6, 130), (8, 95), (10, 10), (12, -30), (14, -20), (16, 55), (17.5, 190), (19, 290), (21, 190), (23, 100), (24, 80)],
    "SA1": [(0, 70), (4, 60), (6, 120), (8, 80), (10, -10), (12, -60), (14, -50), (16, 40), (17.5, 200), (19, 320), (21, 180), (23, 90), (24, 70)],
    "TAS1": [(0, 90), (4, 80), (6, 115), (8, 100), (10, 70), (12, 60), (14, 60), (16, 75), (17.5, 120), (19, 170), (21, 130), (23, 100), (24, 90)],
}
DEMAND = {"NSW1": 7800, "QLD1": 6200, "VIC1": 5300, "SA1": 1400, "TAS1": 1050}
ROOFTOP_MIDDAY = {"NSW1": 4900, "QLD1": 4300, "VIC1": 2900, "SA1": 1100, "TAS1": 120}


def interpolate(points, hour):
    for (h0, p0), (h1, p1) in zip(points, points[1:]):
        if h0 <= hour <= h1:
            return p0 + (p1 - p0) * (hour - h0) / (h1 - h0)
    return points[-1][1]


def main():
    out = os.path.join(ROOT, "widget", "sample-nem-latest.json")
    now = now_nem()
    until = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
    last_day = market_day_of(until)
    rng = random.Random(2026)

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(tmp)
        price_rows, demand_rows = [], []
        for back in range(10, -1, -1):
            day = last_day - timedelta(days=back)
            start = market_day_start(day)
            factor = {r: rng.uniform(0.8, 1.25) for r in REGIONS}
            spike_at = {r: (start + timedelta(hours=rng.uniform(13.5, 16))) if rng.random() < 0.3 else None for r in REGIONS}
            for i in range(1, 289):
                settle = start + INTERVAL * i
                if settle > until:
                    break
                hour = ((settle - INTERVAL).hour * 60 + (settle - INTERVAL).minute) / 60
                for region in REGIONS:
                    price = interpolate(SHAPES[region], hour) * factor[region] + rng.gauss(0, 8)
                    spike = spike_at[region]
                    if spike and spike <= settle - INTERVAL < spike + 3 * INTERVAL:
                        price = rng.uniform(900, 2500)
                    price_rows.append((settle, region, 0, round(price, 2)))
                    if back == 0 and settle > until - timedelta(hours=2):
                        demand_rows.append((settle, region, 0, round(DEMAND[region] * (0.9 + 0.1 * rng.random()), 1)))
        store.upsert("dispatch_price", price_rows)
        store.upsert("dispatch_regionsum", demand_rows)

        # rooftop solar roughly follows a bell curve peaking at 12:30
        hour = until.hour + until.minute / 60
        sun = max(0.0, 1 - abs(hour - 12.5) / 6.5) if 6 <= hour <= 19 else 0.0
        half_hour = until.replace(minute=0 if until.minute < 30 else 30)
        store.upsert("rooftop_pv", [(half_hour, r, "MEASUREMENT", round(ROOFTOP_MIDDAY[r] * sun, 1)) for r in REGIONS])

        data = derive(store, now=until, sample=True, persist_daily=False)
    write_json(out, data)
    print(f"wrote {out} (sample data as at {data['data_as_at']})")


if __name__ == "__main__":
    main()
