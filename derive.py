"""Turn the store into the small JSON file the website widget reads.

Definitions (all in NEM time, AEST all year):
  * Market day   04:00 to 04:00, the way AEMO reports a "day".
  * Price        the 5-minute dispatch price (RRP, $/MWh) from the non-intervention run
                 (INTERVENTION = 0), which is the price the market settles on.
  * Midday       intervals starting 10:00-15:00.   Evening: intervals starting 17:00-21:00.
  * Last 7 days  the seven most recent COMPLETE market days before the current one
                 (a day needs at least 280 of its 288 intervals to count).
  * Gap          evening average minus midday average, over the last 7 days.
"""

import json
import os
from datetime import date, datetime, timedelta

from .common import (EVENING, INTERVAL, MIDDAY, MARKET_DAY_START_HOUR, REGIONS, iso_nem,
                     market_day_of, market_day_start, now_nem)

LOOKBACK_DAYS = 15
COMPLETE_MIN = 280
SLOTS = 48  # half-hour slots in a market day


class DeriveError(RuntimeError):
    pass


class _Acc:
    """Running totals for one region on one market day."""

    __slots__ = ("n", "s", "lo", "hi", "neg", "mid_n", "mid_s", "eve_n", "eve_s")

    def __init__(self):
        self.n = self.neg = self.mid_n = self.eve_n = 0
        self.s = self.mid_s = self.eve_s = 0.0
        self.lo = self.hi = None

    def add(self, rrp, hour):
        self.n += 1
        self.s += rrp
        self.lo = rrp if self.lo is None else min(self.lo, rrp)
        self.hi = rrp if self.hi is None else max(self.hi, rrp)
        if rrp < 0:
            self.neg += 1
        if MIDDAY[0] <= hour < MIDDAY[1]:
            self.mid_n += 1
            self.mid_s += rrp
        if EVENING[0] <= hour < EVENING[1]:
            self.eve_n += 1
            self.eve_s += rrp

    @property
    def avg(self):
        return self.s / self.n if self.n else None

    @property
    def midday_avg(self):
        return self.mid_s / self.mid_n if self.mid_n else None

    @property
    def evening_avg(self):
        return self.eve_s / self.eve_n if self.eve_n else None


def accumulate_daily(price_rows):
    """price_rows: iterable of (settlement_ts, region_id, rrp) -> {(region, day): _Acc}"""
    out = {}
    for ts, region, rrp in price_rows:
        key = (region, market_day_of(ts))
        acc = out.get(key)
        if acc is None:
            acc = out[key] = _Acc()
        acc.add(rrp, (ts - INTERVAL).hour)
    return out


def _daily_rows(daily):
    return [(day, region, a.n, a.avg, a.lo, a.hi, a.neg, a.midday_avg, a.evening_avg)
            for (region, day), a in sorted(daily.items())]


def _r(value, places=2):
    return None if value is None else round(value, places)


def _zero_intervention(store, since):
    for ts, region, intervention, rrp in store.rows("dispatch_price", since=since):
        if intervention == 0 and region in REGIONS:
            yield ts, region, rrp


def rebuild_daily(store, since=None):
    """Recompute the daily_stats table from all stored prices (or those since a date)."""
    since_ts = datetime(since.year, since.month, since.day) if since else None
    daily = accumulate_daily(_zero_intervention(store, since_ts))
    return store.upsert("daily_stats", _daily_rows(daily))


def _latest_by_region(rows, ts_index, value_index):
    best = {}
    for row in rows:
        region = row[1]
        if region in REGIONS and (region not in best or row[ts_index] >= best[region][ts_index]):
            best[region] = row
    return {region: (row[ts_index], row[value_index]) for region, row in best.items()}


def derive(store, now=None, sample=False, lookback_days=LOOKBACK_DAYS, persist_daily=True):
    """Build the widget JSON (as a dict) from the store, and refresh recent daily_stats."""
    latest = store.max_first("dispatch_price")
    if latest is None:
        raise DeriveError("The store has no dispatch prices yet. Run the update first.")
    now = now or now_nem()
    today = market_day_of(latest)
    since = market_day_start(today - timedelta(days=lookback_days))

    series = {region: [] for region in REGIONS}
    for ts, region, rrp in _zero_intervention(store, since):
        series[region].append((ts, rrp))
    for values in series.values():
        values.sort()

    daily = accumulate_daily((ts, region, rrp) for region, vals in series.items() for ts, rrp in vals)
    if persist_daily:
        store.upsert("daily_stats", _daily_rows(daily))

    # half-hour price shape for the current market day
    start_today = market_day_start(today)
    shape = {region: [[0.0, 0] for _ in range(SLOTS)] for region in REGIONS}
    for region, vals in series.items():
        for ts, rrp in vals:
            if market_day_of(ts) != today:
                continue
            slot = int(((ts - INTERVAL) - start_today).total_seconds() // 1800)
            if 0 <= slot < SLOTS:
                shape[region][slot][0] += rrp
                shape[region][slot][1] += 1

    demand = _latest_by_region(
        (r for r in store.rows("dispatch_regionsum", since=latest - timedelta(hours=2)) if r[2] == 0), 0, 3)
    rooftop = _latest_by_region(store.rows("rooftop_pv", since=latest - timedelta(hours=6)), 0, 3)

    regions = {}
    for region, name in REGIONS.items():
        vals = series[region]
        if not vals:
            continue
        ts_now, price_now = vals[-1]
        t = daily.get((region, today))

        complete = sorted(((d, a) for (rg, d), a in daily.items() if rg == region and d < today and a.n >= COMPLETE_MIN),
                          key=lambda item: item[0], reverse=True)[:7]
        last7 = None
        if complete:
            n = sum(a.n for _, a in complete)
            mid_n = sum(a.mid_n for _, a in complete)
            eve_n = sum(a.eve_n for _, a in complete)
            mid = sum(a.mid_s for _, a in complete) / mid_n if mid_n else None
            eve = sum(a.eve_s for _, a in complete) / eve_n if eve_n else None
            last7 = {
                "days": len(complete),
                "avg": _r(sum(a.s for _, a in complete) / n),
                "midday_avg": _r(mid),
                "evening_avg": _r(eve),
                "gap": _r(eve - mid) if mid is not None and eve is not None else None,
                "negative_intervals": sum(a.neg for _, a in complete),
                "min": _r(min(a.lo for _, a in complete)),
                "max": _r(max(a.hi for _, a in complete)),
                "by_day": [{"date": d.isoformat(), "avg": _r(a.avg), "midday_avg": _r(a.midday_avg),
                            "evening_avg": _r(a.evening_avg), "negative_intervals": a.neg}
                           for d, a in sorted(complete, key=lambda item: item[0])],
            }

        current = {"price": _r(price_now), "as_at": iso_nem(ts_now)}
        if region in demand:
            current["demand_mw"] = _r(demand[region][1], 1)
            current["demand_as_at"] = iso_nem(demand[region][0])
        if region in rooftop:
            current["rooftop_pv_mw"] = _r(rooftop[region][1], 1)
            current["rooftop_as_at"] = iso_nem(rooftop[region][0])

        regions[region] = {
            "name": name,
            "current": current,
            "today": {
                "intervals": t.n,
                "avg": _r(t.avg), "min": _r(t.lo), "max": _r(t.hi),
                "negative_intervals": t.neg,
                "shape30": [_r(s / n) if n else None for s, n in shape[region]],
            },
            "last7": last7,
        }

    return {
        "schema": 1,
        "sample": bool(sample),
        "source": "AEMO (public NEMWEB data)",
        "generated_at": iso_nem(now),
        "data_as_at": iso_nem(latest),
        "market_day": today.isoformat(),
        "market_day_start": iso_nem(start_today),
        "windows": {"midday": list(MIDDAY), "evening": list(EVENING)},
        "regions": regions,
    }


def write_json(path, data):
    """Write JSON atomically so a half-written file is never published."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)
