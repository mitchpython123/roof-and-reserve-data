"""Turn the store into the small JSON file the website widget reads.

Definitions (all in NEM time, AEST all year):
  * Market day   04:00 to 04:00, the way AEMO reports a "day".
  * Price        the 5-minute dispatch price (RRP, $/MWh) from the non-intervention run
                 (INTERVENTION = 0), which is the price the market settles on.
  * Midday       intervals starting 10:00-15:00.   Evening: intervals starting 17:00-21:00.
  * Last 7 days  the seven most recent COMPLETE market days before the current one
                 (a day needs at least 280 of its 288 intervals to count).
  * Gap          evening average minus midday average, over the last 7 days.
  * Last hour    the most recent complete clock hour in the store (for example 09:00-10:00),
                 averaged over its twelve 5-minute prices. Needs at least 6 of the 12.
  * Best spread  for the most recent complete market day, from 4am to midnight only (the
                 hours after midnight are left out, so the windows match a household's day):
                 the cheapest 3-hour block of half-hour prices, and the dearest 3-hour block
                 that starts after it ends, chosen to make the difference as large as possible. 3 hours is the time the
                 site's 13.5 kWh / 5 kW reference battery needs to fill or empty (2.7 hours),
                 rounded up to whole half hours. A price spread only: no losses or fees.
  * Outlook      from AEMO's latest 30-minute pre-dispatch (P30) run: the average forecast
                 price over the next 2 hours (the next four half-hour periods that start
                 after the latest dispatch interval) against the last hour's average. "Steady" means
                 within $10/MWh or 10% of the last hour's average, whichever is larger.
                 Demand change is the forecast demand at the end of those 2 hours against
                 the latest 5-minute demand. Ignored if the forecast run is over 2 hours old.
"""

import json
import os
from datetime import date, datetime, timedelta

from .common import (EVENING, INTERVAL, MIDDAY, MARKET_DAY_START_HOUR, REGIONS, iso_nem,
                     market_day_of, market_day_start, now_nem)

LOOKBACK_DAYS = 15
COMPLETE_MIN = 280
SLOTS = 48  # half-hour slots in a market day
HOUR_MIN_INTERVALS = 6  # of 12, before an hourly average is published
SPREAD_SLOTS = 6  # half hours in a spread window (3 hours, see "Best spread" above)
SPREAD_END_SLOT = 40  # half hours from 4am to midnight
SLOT_MIN_INTERVALS = 4  # of 6, before a half-hour average is used for the spread
OUTLOOK_PERIODS = 4  # half-hour P30 periods in the outlook (2 hours)
STEADY_MIN = 10.0  # $/MWh
STEADY_FRACTION = 0.10
FORECAST_MAX_AGE = timedelta(hours=2)
HALF_HOUR = timedelta(minutes=30)


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


# ---- half-hour profile (store table profile30) ---------------------------------------------
SPIKE = 300.0  # $/MWh: a 5-minute price above this counts as a spike


def _slot_of(ts, length):
    """(market_day, slot) of an interval stamped with its END time and `length` long."""
    start = ts - length
    day = (start - timedelta(hours=MARKET_DAY_START_HOUR)).date()
    slot = int((start - market_day_start(day)).total_seconds() // 1800)
    return day, slot


def profile_rows(prices, demand=(), rooftop=()):
    """Build profile30 rows from raw data.

    prices:  (settlement_ts, region, rrp) for the non-intervention run
    demand:  (settlement_ts, region, totaldemand) for the non-intervention run
    rooftop: (interval_ts, region, type, power_mw); MEASUREMENT is preferred over other types
    """
    acc = {}
    for ts, region, rrp in prices:
        day, slot = _slot_of(ts, INTERVAL)
        a = acc.get((day, region, slot))
        if a is None:
            a = acc[(day, region, slot)] = [0, 0.0, rrp, rrp, 0, 0, 0.0, 0, None, None]
        a[0] += 1
        a[1] += rrp
        a[2] = min(a[2], rrp)
        a[3] = max(a[3], rrp)
        a[4] += rrp < 0
        a[5] += rrp > SPIKE
    for ts, region, mw in demand:
        day, slot = _slot_of(ts, INTERVAL)
        a = acc.get((day, region, slot))
        if a is not None:
            a[6] += mw
            a[7] += 1
    for ts, region, kind, mw in rooftop:
        day, slot = _slot_of(ts, HALF_HOUR)
        a = acc.get((day, region, slot))
        if a is None:
            continue
        rank = 0 if kind == "MEASUREMENT" else 1
        if a[9] is None or rank < a[9]:
            a[8], a[9] = mw, rank
    return [(day, region, slot, a[0], a[1] / a[0], a[2], a[3], a[4], a[5],
             a[6] / a[7] if a[7] else None, a[8])
            for (day, region, slot), a in sorted(acc.items())]


def rebuild_profile(store, since=None, until=None):
    """Recompute profile30 from the raw tables, a month at a time. Returns rows written."""
    total = 0
    months = sorted(set(p[:7] for p in store.parts("dispatch_price")))
    for month in months:
        first = date.fromisoformat(month + "-01")
        nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        if (since and nxt <= since) or (until and first > until):
            continue
        start = market_day_start(max(first, since) if since else first)
        end = market_day_start(min(nxt, until + timedelta(days=1)) if until else nxt)
        rows = profile_rows(
            ((ts, rg, rrp) for ts, rg, iv, rrp in store.rows("dispatch_price", since=start)
             if iv == 0 and rg in REGIONS and ts <= end),
            ((ts, rg, mw) for ts, rg, iv, mw in store.rows("dispatch_regionsum", since=start)
             if iv == 0 and ts <= end),
            (r for r in store.rows("rooftop_pv", since=start) if r[0] <= end + HALF_HOUR))
        rows = [r for r in rows if market_day_start(r[0]) >= start and market_day_start(r[0]) < end]
        total += store.upsert("profile30", rows)
    return total


def _latest_by_region(rows, ts_index, value_index):
    best = {}
    for row in rows:
        region = row[1]
        if region in REGIONS and (region not in best or row[ts_index] >= best[region][ts_index]):
            best[region] = row
    return {region: (row[ts_index], row[value_index]) for region, row in best.items()}


def last_full_hour(latest):
    """(start, end) of the most recent clock hour fully covered by data up to `latest`.

    Intervals are stamped with their END time, so the hour 09:00-10:00 is the intervals
    stamped 09:05 to 10:00 inclusive.
    """
    end = latest.replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=1), end


def hour_average(values, start, end):
    """values: sorted [(ts, rrp)] -> {avg, start, end, intervals} or None if too thin."""
    prices = [rrp for ts, rrp in values if start < ts <= end]
    if len(prices) < HOUR_MIN_INTERVALS:
        return None
    return {"avg": _r(sum(prices) / len(prices)), "start": iso_nem(start), "end": iso_nem(end),
            "intervals": len(prices)}


def best_spread(slots, width=SPREAD_SLOTS):
    """slots: half-hour average prices for one day (None where too thin).

    Returns (low_start, high_start, low_avg, high_avg) for the cheapest `width`-slot block
    and the dearest block starting at or after its end, maximising high minus low. Ties go
    to the earliest blocks. None if no pair of complete blocks exists.
    """
    blocks = []
    for i in range(len(slots) - width + 1):
        window = slots[i:i + width]
        blocks.append(None if any(v is None for v in window) else sum(window) / width)
    best = None
    for i, low in enumerate(blocks):
        if low is None:
            continue
        for j in range(i + width, len(blocks)):
            high = blocks[j]
            if high is not None and (best is None or high - low > best[3] - best[2]):
                best = (i, j, low, high)
    return best


def _spread_summary(day, slots, width=SPREAD_SLOTS):
    found = best_spread(slots, width)
    if found is None:
        return None
    i, j, low, high = found
    start = market_day_start(day)
    window = lambda k, avg: {"start": iso_nem(start + k * HALF_HOUR),
                             "end": iso_nem(start + (k + width) * HALF_HOUR), "avg": _r(avg)}
    return {"date": day.isoformat(), "hours": width / 2, "window": "04:00-24:00", "spread": _r(high - low),
            "low": window(i, low), "high": window(j, high)}


def _forecast_by_region(store, latest):
    """{region: [(period_end, rrp, demand)]} for P30 periods starting at or after `latest`
    (the half hour already under way is left out), plus the run's creation time."""
    held = store.load_json("predispatch_latest")
    if not held or not held.get("periods"):
        return {}, None
    created = datetime.strptime(held["created"][:19], "%Y-%m-%dT%H:%M:%S")
    if created < latest - FORECAST_MAX_AGE:
        return {}, None
    out = {}
    for end, region, rrp, dem in held["periods"]:
        end_ts = datetime.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")
        if end_ts - HALF_HOUR >= latest and region in REGIONS:  # periods not yet started
            out.setdefault(region, []).append((end_ts, rrp, dem))
    for periods in out.values():
        periods.sort()
    return out, created


def outlook(periods, base, demand_now, today, created):
    """Summarise the P30 forecast for one region. base: last hour's average price."""
    ahead = periods[:OUTLOOK_PERIODS]
    if len(ahead) < 2 or base is None:
        return None
    avg = sum(rrp for _, rrp, _ in ahead) / len(ahead)
    change = avg - base
    if abs(change) < max(STEADY_MIN, STEADY_FRACTION * abs(base)):
        direction = "steady"
    else:
        direction = "rising" if change > 0 else "falling"
    end_ts, _, dem_end = ahead[-1]
    demand_change = None
    if dem_end is not None and demand_now:
        demand_change = _r((dem_end - demand_now) / demand_now * 100, 1)
    start_today = market_day_start(today)
    forecast30 = [None] * SLOTS
    peak = None
    for p_end, rrp, _ in periods:
        slot = int(((p_end - HALF_HOUR) - start_today).total_seconds() // 1800)
        if 0 <= slot < SLOTS:
            forecast30[slot] = _r(rrp)
            if peak is None or rrp > peak[1]:
                peak = (p_end, rrp)
    return {
        "source": "AEMO 30-minute pre-dispatch (P30)",
        "run_created": iso_nem(created),
        "horizon_end": iso_nem(end_ts),
        "periods": len(ahead),
        "forecast_avg": _r(avg),
        "base": _r(base),
        "change": _r(change),
        "direction": direction,
        "demand_now": _r(demand_now, 1),
        "demand_at_horizon": _r(dem_end, 1),
        "demand_change_pct": demand_change,
        "peak_today": None if peak is None else {"start": iso_nem(peak[0] - HALF_HOUR), "end": iso_nem(peak[0]),
                                                  "price": _r(peak[1])},
        "forecast30": forecast30,
    }


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
        # The window starts at 04:00, so the day before it still picks up the one interval
        # stamped exactly 04:00. Only save days that lie wholly inside the window, or that
        # day's statistics would be overwritten with a single interval.
        first_full = today - timedelta(days=lookback_days)
        store.upsert("daily_stats", [r for r in _daily_rows(daily) if r[0] >= first_full])
        # refresh the half-hour profile for the last few market days (AEMO's final prices for
        # a day arrive the next morning, and rooftop estimates can lag a few hours)
        if not store.parts("profile30"):
            rebuild_profile(store)  # first run after the profile table was added
        recent = today - timedelta(days=3)
        store.upsert("profile30", [row for row in profile_rows(
            ((ts, region, rrp) for region, vals in series.items() for ts, rrp in vals),
            ((ts, rg, mw) for ts, rg, iv, mw in store.rows("dispatch_regionsum", since=market_day_start(recent))
             if iv == 0),
            store.rows("rooftop_pv", since=market_day_start(recent))) if row[0] >= recent])

    # half-hour price shapes, by market day (today's feeds the chart, earlier ones the spread)
    start_today = market_day_start(today)
    shapes = {}
    for region, vals in series.items():
        for ts, rrp in vals:
            day = market_day_of(ts)
            slot = int(((ts - INTERVAL) - market_day_start(day)).total_seconds() // 1800)
            if 0 <= slot < SLOTS:
                acc = shapes.setdefault((region, day), [[0.0, 0] for _ in range(SLOTS)])[slot]
                acc[0] += rrp
                acc[1] += 1
    empty = [[0.0, 0] for _ in range(SLOTS)]

    demand = _latest_by_region(
        (r for r in store.rows("dispatch_regionsum", since=latest - timedelta(hours=2)) if r[2] == 0), 0, 3)
    rooftop = _latest_by_region(store.rows("rooftop_pv", since=latest - timedelta(hours=6)), 0, 3)

    hour_start, hour_end = last_full_hour(latest)
    forecast, forecast_created = _forecast_by_region(store, latest)

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

        last_hour = hour_average(vals, hour_start, hour_end)
        spread = None
        if complete:
            day = complete[0][0]
            slots = [sm / n if n >= SLOT_MIN_INTERVALS else None
                     for sm, n in shapes.get((region, day), empty)[:SPREAD_END_SLOT]]
            spread = _spread_summary(day, slots)
        base = last_hour["avg"] if last_hour else price_now
        region_outlook = None
        if region in forecast:
            region_outlook = outlook(forecast[region], base, demand.get(region, (None, None))[1],
                                     today, forecast_created)

        regions[region] = {
            "name": name,
            "current": current,
            "last_hour": last_hour,
            "today": {
                "intervals": t.n,
                "avg": _r(t.avg), "min": _r(t.lo), "max": _r(t.hi),
                "negative_intervals": t.neg,
                "shape30": [_r(sm / n) if n else None for sm, n in shapes.get((region, today), empty)],
            },
            "last7": last7,
            "best_spread_last_day": spread,
            "outlook": region_outlook,
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
